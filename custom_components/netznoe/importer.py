"""Historical data importer for Netz NO Smartmeter."""

import calendar
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from operator import itemgetter
from typing import TypeVar
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api.errors import SmartmeterConnectionError, SmartmeterLoginError
from .AsyncSmartmeter import AsyncSmartmeter
from .const import DOMAIN, STAT_SUFFIX_GRID, STAT_SUFFIX_SELF_COVERAGE

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# The Netz NO API returns interval timestamps without a UTC offset. They are
# always in the grid's own local time (Austria), regardless of the Home
# Assistant instance's configured time zone.
NETZNOE_TIMEZONE = ZoneInfo("Europe/Vienna")


class Importer:
    """Import historical consumption data into Home Assistant statistics."""

    def __init__(  # noqa: PLR0913 - the importer is configured from several sources
        self,
        hass: HomeAssistant,
        async_smartmeter: AsyncSmartmeter,
        metering_point_id: str,
        unit_of_measurement: str,
        has_ftm_meter_data: bool = True,
        energy_community: bool = False,
    ):
        """Initialize the importer.

        Args:
            hass: Home Assistant instance
            async_smartmeter: Async smartmeter client
            metering_point_id: Metering point ID
            unit_of_measurement: Unit of measurement for statistics
            has_ftm_meter_data: True for 15-min interval meters, False for daily meters
            energy_community: True to also import the energy community split

        """
        self.id = f"{DOMAIN}:{metering_point_id.lower()}"
        self.id_self_coverage = f"{self.id}_{STAT_SUFFIX_SELF_COVERAGE}"
        self.id_grid = f"{self.id}_{STAT_SUFFIX_GRID}"
        self.metering_point_id = metering_point_id
        self.unit_of_measurement = unit_of_measurement
        self.hass = hass
        self.async_smartmeter = async_smartmeter
        self.has_ftm_meter_data = has_ftm_meter_data
        # The energy community split is only published for interval meters.
        self.energy_community = energy_community and has_ftm_meter_data
        # Running totals of the most recent import, keyed by statistic id.
        self.last_totals: dict[str, Decimal] = {}

    def statistic_ids(self) -> list[str]:
        """Return every statistic id this importer maintains."""
        if not self.energy_community:
            return [self.id]
        return [self.id, self.id_self_coverage, self.id_grid]

    def is_last_inserted_stat_valid(self, last_inserted_stat: dict) -> bool:
        """Check if last inserted statistics are valid."""
        return (
            len(last_inserted_stat) == 1
            and len(last_inserted_stat.get(self.id, [])) == 1
            and "sum" in last_inserted_stat[self.id][0]
            and "end" in last_inserted_stat[self.id][0]
        )

    def prepare_start_off_point(
        self, last_inserted_stat: dict
    ) -> tuple[datetime, Decimal] | None:
        """Prepare starting point for incremental import."""
        _sum = Decimal(last_inserted_stat[self.id][0]["sum"])
        start = last_inserted_stat[self.id][0]["end"]

        # Handle different types returned by HA core
        if isinstance(start, (int, float)):
            start = dt_util.utc_from_timestamp(start)
        if isinstance(start, str):
            start = dt_util.parse_datetime(start)

        if not isinstance(start, datetime):
            _LOGGER.error(
                "Unexpected type for statistics end: %s (type: %s)",
                last_inserted_stat,
                type(last_inserted_stat[self.id][0]["end"]),
            )
            return None

        # Don't query if less than 1h since last update
        min_wait = timedelta(hours=1)
        delta_t = datetime.now(UTC) - start.replace(microsecond=0)
        if delta_t <= min_wait:
            _LOGGER.debug(
                "Skipping API query - last update is recent. Next update in %s",
                min_wait - delta_t,
            )
            return None

        return start, _sum

    async def _last_statistics(self, statistic_id: str) -> dict:
        """Return the most recent statistic written for one series."""
        return await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,
            {"sum", "state"},
        )

    async def _series_without_statistics(self) -> list[str]:
        """Return the maintained series that hold no statistics yet."""
        written = {
            statistic_id: (await self._last_statistics(statistic_id)).get(statistic_id)
            for statistic_id in self.statistic_ids()
        }
        return [statistic_id for statistic_id, rows in written.items() if not rows]

    async def async_import(self) -> Decimal | None:
        """Import historical data.

        Returns:
            The cumulative total usage after import, or None if import was skipped.

        """
        # Query last statistics
        last_inserted_stat = await self._last_statistics(self.id)
        _LOGGER.debug("Last inserted stat: %s", last_inserted_stat)

        try:
            if not self.is_last_inserted_stat_valid(last_inserted_stat):
                # Initial import - last 3 years
                _LOGGER.warning("Starting initial import. This may take some time.")
                return await self._initial_import_statistics()

            # A series can be added to a meter that already has history, which
            # is what happens when the energy community option is switched on
            # later. Reading only the recent window would leave it starting
            # from nothing, so it is filled in over the whole history first.
            missing = await self._series_without_statistics()
            if missing:
                _LOGGER.warning(
                    "No statistics yet for %s, importing the full history so the "
                    "series covers the same period as the total",
                    ", ".join(missing),
                )
                await self._import_statistics(series=missing)

            # Incremental import
            start_off_point = self.prepare_start_off_point(last_inserted_stat)
            if start_off_point is None:
                # Return existing sum if no new import needed
                return Decimal(last_inserted_stat[self.id][0]["sum"])
            start, _sum = start_off_point
            totals = {self.id: _sum}

            if self.energy_community:
                # The community split for a day arrives after that day was
                # already imported, so the previous day is read again and its
                # statistics rewritten from the totals that preceded it.
                start = min(start, self.previous_day_start(datetime.now(UTC)))
                totals = await self._running_sums_before(start)

            return await self._incremental_import_statistics(start, totals)

        except TimeoutError as e:
            _LOGGER.warning("Timeout during import: %s", e)
            return None
        except Exception as e:
            _LOGGER.exception("Error during import: %s", e)
            return None

    def get_statistics_metadata(
        self, statistic_id: str | None = None
    ) -> StatisticMetaData:
        """Get statistics metadata for one of the maintained series."""
        statistic_id = statistic_id or self.id
        name = f"Netz NO {self.metering_point_id}"
        if statistic_id == self.id_self_coverage:
            name = f"{name} Eigendeckung"
        elif statistic_id == self.id_grid:
            name = f"{name} Restnetzbezug"

        return StatisticMetaData(
            source=DOMAIN,
            statistic_id=statistic_id,
            name=name,
            unit_of_measurement=self.unit_of_measurement,
            has_mean=False,
            has_sum=True,
        )

    async def _fetch_with_relogin(
        self, fetch: Callable[[], Awaitable[_T]], description: str
    ) -> _T | None:
        """Fetch one period, re-authenticating once if the session was lost.

        A long import can outlive its session. Every later call then fails
        without even reaching the API, so without logging back in the rest of
        the run would be quietly dropped. Returns None if the period could not
        be read, which the callers count so a truncated import is reported.
        """
        try:
            return await fetch()
        except (SmartmeterConnectionError, SmartmeterLoginError) as err:
            _LOGGER.debug(
                "Session lost while reading %s (%s), logging in again",
                description,
                err,
            )
        except Exception as err:
            _LOGGER.debug("Could not fetch data for %s: %s", description, err)
            return None

        try:
            await self.async_smartmeter.ensure_logged_in()
            return await fetch()
        except Exception as err:
            _LOGGER.warning(
                "Could not fetch data for %s even after logging in again: %s",
                description,
                err,
            )
            return None

    @staticmethod
    def previous_day_start(now: datetime) -> datetime:
        """Return the start of the previous day, in UTC.

        Days are counted in the grid's own time zone, since that is how the
        API groups the readings and publishes the energy community split.
        """
        yesterday = now.astimezone(NETZNOE_TIMEZONE) - timedelta(days=1)
        return dt_util.as_utc(
            yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        )

    async def _running_sums_before(self, moment: datetime) -> dict[str, Decimal]:
        """Return each series' cumulative sum as of just before a moment.

        Re-importing a stretch of history means continuing its running totals,
        so the last statistic written before that stretch is the anchor.
        """
        statistic_ids = set(self.statistic_ids())
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            moment - timedelta(days=7),
            moment,
            statistic_ids,
            "hour",
            None,
            {"sum"},
        )

        sums: dict[str, Decimal] = {}
        for statistic_id in statistic_ids:
            entries = rows.get(statistic_id) or []
            last_sum = entries[-1].get("sum") if entries else None
            sums[statistic_id] = (
                Decimal(str(last_sum)) if last_sum is not None else Decimal(0)
            )
        return sums

    async def _initial_import_statistics(self) -> Decimal:
        """Perform initial import of statistics."""
        return await self._import_statistics()

    async def _incremental_import_statistics(
        self, start: datetime, totals: dict[str, Decimal]
    ) -> Decimal:
        """Perform incremental import of statistics."""
        return await self._import_statistics(start=start, totals=totals)

    async def _import_statistics(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        totals: dict[str, Decimal] | None = None,
        series: list[str] | None = None,
    ) -> Decimal:
        """Import statistics from Netz NO API.

        Dispatches to the appropriate import method based on meter type.
        `series` limits which of the maintained series are written, so one can
        be filled in without rewriting the others.
        """
        now = datetime.now(UTC)
        series = series or self.statistic_ids()
        totals = dict(totals or {})
        for statistic_id in series:
            totals.setdefault(statistic_id, Decimal(0))

        if start is None:
            # Default: 3 years of history
            start = now - timedelta(days=365 * 3)
        if end is None:
            end = now

        if start.tzinfo is None:
            raise ValueError("start datetime must be timezone-aware!")

        _LOGGER.debug(
            "Importing data from %s to %s (FTM: %s, energy community: %s)",
            start,
            end,
            self.has_ftm_meter_data,
            self.energy_community,
        )
        if start > end:
            _LOGGER.warning("Start date is after end date, skipping")
            return totals.get(self.id, Decimal(0))

        if self.has_ftm_meter_data:
            return await self._import_ftm_statistics(start, end, totals, series)
        return await self._import_daily_statistics(
            start, end, totals.get(self.id, Decimal(0))
        )

    async def _import_ftm_statistics(
        self,
        start: datetime,
        end: datetime,
        totals: dict[str, Decimal],
        series: list[str] | None = None,
    ) -> Decimal:
        """Import FTM (15-minute interval) statistics.

        Each day returns individual readings (e.g., 96 values for 15-min intervals)
        together with the corresponding timestamps. The API reports the *end* of
        each interval (e.g., 23:00 for the 22:45-23:00 interval). We aggregate to
        hourly statistics (HA requires timestamps at top of hour).

        For energy community members the total is additionally split into the
        share covered by the community and the remainder taken from the grid.
        """
        series = series or self.statistic_ids()
        hourly_readings: dict[str, defaultdict[datetime, Decimal]] = {
            statistic_id: defaultdict(Decimal) for statistic_id in series
        }
        current_date = start.date()
        end_date = end.date()
        unreadable_days = 0

        while current_date <= end_date:
            consumption = await self._fetch_with_relogin(
                partial(
                    self.async_smartmeter.get_consumption_day_series,
                    current_date,
                    self.metering_point_id,
                ),
                str(current_date),
            )
            if consumption is None:
                unreadable_days += 1
                current_date += timedelta(days=1)
                continue

            try:
                self._aggregate_day(consumption, current_date, start, hourly_readings)
            except Exception as e:
                _LOGGER.debug("Could not process data for %s: %s", current_date, e)

            current_date += timedelta(days=1)

        if unreadable_days:
            _LOGGER.warning(
                "Could not read %d of the days between %s and %s for %s, "
                "so this import is incomplete",
                unreadable_days,
                start.date(),
                end_date,
                self.metering_point_id,
            )

        for statistic_id in series:
            totals[statistic_id] = self._write_statistics(
                statistic_id, hourly_readings[statistic_id], totals[statistic_id]
            )

        self.last_totals.update(totals)
        return totals.get(self.id, Decimal(0))

    def _aggregate_day(
        self,
        consumption,
        day: date,
        start: datetime,
        hourly_readings: dict[str, defaultdict[datetime, Decimal]],
    ) -> None:
        """Add one day's readings to the hourly buckets of each series."""
        times = consumption.times
        values = consumption.metered
        if not values:
            return

        if len(times) < len(values):
            _LOGGER.warning(
                "Netz NO returned %d values but only %d timestamps for %s, "
                "skipping the surplus readings",
                len(values),
                len(times),
                day,
            )

        # Timestamps arrive in chronological order. When the autumn DST change
        # repeats 02:00-03:00 local time, a timestamp stops advancing:
        # everything from there on belongs to the second pass, which must be
        # marked so both passes do not collapse into the same UTC hour.
        fold = 0
        previous_reading_time = None

        for i, value in enumerate(values):
            if i >= len(times):
                break

            reading_time = dt_util.parse_datetime(times[i])
            if reading_time is None:
                continue
            if reading_time.tzinfo is None:
                # The API reports naive timestamps in Austria local time, not
                # UTC - localize before converting so DST offsets (CET/CEST)
                # are applied correctly.
                if (
                    previous_reading_time is not None
                    and reading_time <= previous_reading_time
                ):
                    fold = 1
                previous_reading_time = reading_time
                reading_time = reading_time.replace(tzinfo=NETZNOE_TIMEZONE, fold=fold)
            if value is None:
                continue
            reading_time = dt_util.as_utc(reading_time)

            # API time is the end of the interval; subtract 1 minute before
            # flooring to the hour so the reading is attributed to the hour it
            # actually occurred in.
            hour_start = (reading_time - timedelta(minutes=1)).replace(
                minute=0, second=0, microsecond=0
            )
            # Skip hours already imported (start = end of last stat)
            if hour_start < start:
                continue

            usage = Decimal(str(value))
            if self.id in hourly_readings:
                hourly_readings[self.id][hour_start] += usage

            if self.energy_community:
                self_covered, from_grid = self._split_energy_community(
                    usage,
                    consumption.self_coverage_at(i),
                    consumption.grid_leftover_at(i),
                )
                if self.id_self_coverage in hourly_readings:
                    hourly_readings[self.id_self_coverage][hour_start] += self_covered
                if self.id_grid in hourly_readings:
                    hourly_readings[self.id_grid][hour_start] += from_grid

    @staticmethod
    def _split_energy_community(
        usage: Decimal,
        self_coverage: float | None,
        grid_leftover: float | None,
    ) -> tuple[Decimal, Decimal]:
        """Split one reading into the community share and the grid share.

        The split is published later than the consumption itself. Until it
        arrives the whole reading counts as taken from the grid, which keeps
        the grid series equal to the total; the values are corrected on a later
        run once the API fills them in.
        """
        if self_coverage is None:
            return Decimal(0), usage

        self_covered = Decimal(str(self_coverage))
        if grid_leftover is not None:
            return self_covered, Decimal(str(grid_leftover))
        return self_covered, usage - self_covered

    def _write_statistics(
        self,
        statistic_id: str,
        hourly_readings: dict[datetime, Decimal],
        total: Decimal,
    ) -> Decimal:
        """Write one series' hourly buckets and return its new running total."""
        statistics = []
        for ts, usage in sorted(hourly_readings.items(), key=itemgetter(0)):
            total += usage
            statistics.append(
                StatisticData(start=ts, sum=float(total), state=float(usage))
            )

        if statistics:
            _LOGGER.debug(
                "Importing %d FTM statistics entries for %s from %s to %s",
                len(statistics),
                statistic_id,
                statistics[0]["start"],
                statistics[-1]["start"],
            )
            async_add_external_statistics(
                self.hass, self.get_statistics_metadata(statistic_id), statistics
            )

        return total

    async def _import_daily_statistics(
        self,
        start: datetime,
        end: datetime,
        total_usage: Decimal,
    ) -> Decimal:
        """Import daily meter statistics using the Month endpoint.

        Each day's single consumption value becomes one statistics entry
        assigned to midnight UTC of that day.
        """
        daily_readings: dict[datetime, Decimal] = {}
        start_date = start.date()
        end_date = end.date()

        # Iterate month by month
        current_year = start_date.year
        current_month = start_date.month
        unreadable_months = 0

        while date(current_year, current_month, 1) <= end_date:
            month = await self._fetch_with_relogin(
                partial(
                    self.async_smartmeter.get_consumption_month,
                    current_year,
                    current_month,
                    self.metering_point_id,
                ),
                f"{current_year}-{current_month:02d}",
            )
            if month is None:
                unreadable_months += 1
                if current_month == 12:
                    current_year += 1
                    current_month = 1
                else:
                    current_month += 1
                continue

            try:
                _times, values = month

                if values:
                    days_in_month = calendar.monthrange(current_year, current_month)[1]
                    for day_index, value in enumerate(values):
                        if value is None:
                            continue
                        day_num = day_index + 1  # 1-based day of month
                        if day_num > days_in_month:
                            break
                        day_date = date(current_year, current_month, day_num)
                        # Skip days outside our requested range.
                        # Use <= for start_date because incremental imports set start
                        # to the END of the last stat (midnight + 1hr), so start_date
                        # equals the last already-imported day — skip it to avoid
                        # re-adding its value to the cumulative sum.
                        if day_date <= start_date or day_date > end_date:
                            continue
                        day_midnight = datetime.combine(
                            day_date, datetime.min.time(), tzinfo=UTC
                        )
                        daily_readings[day_midnight] = Decimal(str(value))

            except Exception as e:
                _LOGGER.debug(
                    "Could not process monthly data for %d-%02d: %s",
                    current_year,
                    current_month,
                    e,
                )

            # Advance to next month
            if current_month == 12:
                current_year += 1
                current_month = 1
            else:
                current_month += 1

        if unreadable_months:
            _LOGGER.warning(
                "Could not read %d of the months between %s and %s for %s, "
                "so this import is incomplete",
                unreadable_months,
                start_date,
                end_date,
                self.metering_point_id,
            )

        # Build statistics with daily resolution
        statistics = []
        metadata = self.get_statistics_metadata()

        for ts, usage in sorted(daily_readings.items(), key=itemgetter(0)):
            total_usage += usage
            statistics.append(
                StatisticData(start=ts, sum=float(total_usage), state=float(usage))
            )

        if statistics:
            _LOGGER.debug(
                "Importing %d daily statistics entries from %s to %s",
                len(statistics),
                statistics[0]["start"],
                statistics[-1]["start"],
            )
            async_add_external_statistics(self.hass, metadata, statistics)

        return total_usage
