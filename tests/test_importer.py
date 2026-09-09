"""Tests for the Netz NO statistics importer.

The Netz NO API reports the *end* of each 15 minute interval as a timestamp
without a UTC offset. Those timestamps are UTC: the reading the portal lists
under 05.09.2026 00:15 local arrives as 2026-09-04T22:15:00. Getting that
wrong shifts every reading into the wrong hour of the energy dashboard, so the
mapping from an API timestamp to a Home Assistant hourly bucket is what these
tests pin down.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from custom_components.netznoe.api import ConsumptionDay
from custom_components.netznoe.api.errors import SmartmeterConnectionError
from custom_components.netznoe.importer import Importer


class FakeSmartmeter:
    """Return canned consumption data for a set of days."""

    def __init__(self, days: dict, fail_until_relogin: bool = False) -> None:
        """Store the per-day ConsumptionDay responses."""
        self.days = days
        self.logged_in = not fail_until_relogin
        self.logins = 0
        self.requests = 0

    async def ensure_logged_in(self):
        """Restore the session the way the real client would."""
        self.logins += 1
        self.logged_in = True

    async def get_consumption_day_series(self, day, meter_id):  # noqa: ARG002
        """Return the canned response, or fail while logged out."""
        self.requests += 1
        if not self.logged_in:
            raise SmartmeterConnectionError("Not authenticated. Call login() first.")
        return self.days.get(day, ConsumptionDay())


def utc(year, month, day, hour):
    """Build a UTC timestamp at the top of an hour."""
    return datetime(year, month, day, hour, tzinfo=UTC)


def day_data(times, metered, grid_leftover=None, self_coverage=None):
    """Build a ConsumptionDay the way the API would return one."""
    return ConsumptionDay(
        times=times,
        metered=metered,
        grid_leftover=grid_leftover or [],
        self_coverage=self_coverage or [],
    )


async def run_series_import(days, start, end, *, energy_community=False, series=None):
    """Run the FTM import and return {statistic_id: {bucket: usage}}."""
    importer = Importer(
        MagicMock(),
        FakeSmartmeter(days),
        "AT001",
        "kWh",
        energy_community=energy_community,
    )

    target = "custom_components.netznoe.importer.async_add_external_statistics"
    with patch(target) as add_statistics:
        totals = dict.fromkeys(series or importer.statistic_ids(), Decimal(0))
        await importer._import_ftm_statistics(start, end, totals, series)

    written = {}
    for call in add_statistics.call_args_list:
        metadata, entries = call[0][1], call[0][2]
        written[metadata["statistic_id"]] = {
            entry["start"]: entry["state"] for entry in entries
        }
    return written


async def run_import(days, start, end):
    """Run the import and return the main series' {bucket: usage} mapping."""
    written = await run_series_import(days, start, end)
    return next(iter(written.values()), {})


async def single_reading(day, timestamp, value=1.0):
    """Import one reading and return the bucket it landed in."""
    buckets = await run_import(
        {day: day_data([timestamp], [value])},
        start=datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(days=1),
        end=datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1),
    )
    assert len(buckets) == 1
    return next(iter(buckets))


async def test_timestamps_are_taken_as_utc():
    """An offset free timestamp is the UTC instant, not a local wall clock."""
    bucket = await single_reading(datetime(2026, 7, 1).date(), "2026-07-01T12:15:00")

    assert bucket == utc(2026, 7, 1, 12)


async def test_the_reading_the_portal_shows_at_local_midnight():
    """Pin the mapping that was checked against the portal.

    The portal lists this reading under 05.09.2026 00:15 local time, and the
    API returns it as 2026-09-04T22:15:00, so it belongs to the 22:00 UTC hour
    of the previous day.
    """
    bucket = await single_reading(datetime(2026, 9, 4).date(), "2026-09-04T22:15:00")

    assert bucket == utc(2026, 9, 4, 22)


async def test_no_seasonal_shift_is_applied():
    """A winter timestamp is read exactly like a summer one."""
    bucket = await single_reading(datetime(2026, 1, 15).date(), "2026-01-15T12:15:00")

    assert bucket == utc(2026, 1, 15, 12)


async def test_interval_end_is_attributed_to_the_hour_it_covers():
    """A timestamp on the hour closes the previous interval, not the next one."""
    bucket = await single_reading(datetime(2026, 7, 1).date(), "2026-07-01T23:00:00")

    # 23:00 ends the 22:45-23:00 interval, so it belongs to the 22:00 hour.
    assert bucket == utc(2026, 7, 1, 22)


async def test_quarter_hours_are_summed_into_one_bucket():
    """The four readings of an hour aggregate into a single statistic."""
    day = datetime(2026, 7, 1).date()
    times = [
        "2026-07-01T10:15:00",
        "2026-07-01T10:30:00",
        "2026-07-01T10:45:00",
        "2026-07-01T11:00:00",
    ]
    buckets = await run_import(
        {day: day_data(times, [0.1, 0.2, 0.3, 0.4])},
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
    )

    # All four cover the 10:00-11:00 UTC hour.
    assert buckets == {utc(2026, 7, 1, 10): pytest.approx(1.0)}


async def test_the_day_of_a_dst_change_is_not_shifted():
    """A clock change in Austria does not move the readings.

    The timestamps are UTC, which has no daylight saving time, so the day the
    local clock goes back is imported exactly like any other. Each hour arrives
    once, and nothing needs disambiguating.
    """
    day = datetime(2026, 10, 25).date()
    times = [
        "2026-10-25T00:15:00",
        "2026-10-25T00:30:00",
        "2026-10-25T00:45:00",
        "2026-10-25T01:00:00",
        "2026-10-25T01:15:00",
    ]
    values = [0.1, 0.1, 0.1, 0.1, 0.2]
    buckets = await run_import(
        {day: day_data(times, values)},
        start=utc(2026, 10, 24, 0),
        end=utc(2026, 10, 26, 0),
    )

    assert buckets == {
        utc(2026, 10, 25, 0): pytest.approx(0.4),
        utc(2026, 10, 25, 1): pytest.approx(0.2),
    }
    assert sum(buckets.values()) == pytest.approx(sum(values))


async def test_readings_before_the_start_are_skipped():
    """Hours already imported are not written a second time."""
    day = datetime(2026, 7, 1).date()
    times = ["2026-07-01T10:15:00", "2026-07-01T14:15:00"]
    buckets = await run_import(
        {day: day_data(times, [0.5, 0.7])},
        start=utc(2026, 7, 1, 11),
        end=utc(2026, 7, 2, 0),
    )

    # 10:15 is before the start; 14:15 belongs to the 14:00 hour.
    assert buckets == {utc(2026, 7, 1, 14): pytest.approx(0.7)}


async def test_surplus_values_without_timestamps_are_ignored():
    """A short timestamp list must not crash or invent readings."""
    day = datetime(2026, 7, 1).date()
    buckets = await run_import(
        {day: day_data(["2026-07-01T10:15:00"], [0.5, 0.7, 0.9])},
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
    )

    assert buckets == {utc(2026, 7, 1, 10): pytest.approx(0.5)}


async def test_energy_community_split_is_written_to_separate_series():
    """The community share and the grid share become their own statistics."""
    day = datetime(2026, 7, 1).date()
    times = ["2026-07-01T10:15:00", "2026-07-01T10:30:00"]
    written = await run_series_import(
        {
            day: day_data(
                times,
                metered=[0.053, 0.049],
                grid_leftover=[0.051667, 0.047619],
                self_coverage=[0.001333, 0.001381],
            )
        },
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
        energy_community=True,
    )

    bucket = utc(2026, 7, 1, 10)
    assert written["netznoe:at001"][bucket] == pytest.approx(0.102)
    assert written["netznoe:at001_eigendeckung"][bucket] == pytest.approx(0.002714)
    assert written["netznoe:at001_restnetzbezug"][bucket] == pytest.approx(0.099286)


async def test_grid_series_carries_the_full_usage_until_the_split_arrives():
    """Before the community values are published the grid takes everything."""
    day = datetime(2026, 7, 1).date()
    written = await run_series_import(
        {day: day_data(["2026-07-01T10:15:00"], metered=[0.4])},
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
        energy_community=True,
    )

    bucket = utc(2026, 7, 1, 10)
    assert written["netznoe:at001"][bucket] == pytest.approx(0.4)
    assert written["netznoe:at001_restnetzbezug"][bucket] == pytest.approx(0.4)
    assert written["netznoe:at001_eigendeckung"][bucket] == pytest.approx(0.0)


async def test_missing_grid_value_is_derived_from_the_community_share():
    """A published community share is enough to work out the grid remainder."""
    day = datetime(2026, 7, 1).date()
    written = await run_series_import(
        {
            day: day_data(
                ["2026-07-01T10:15:00"],
                metered=[0.5],
                self_coverage=[0.2],
            )
        },
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
        energy_community=True,
    )

    bucket = utc(2026, 7, 1, 10)
    assert written["netznoe:at001_eigendeckung"][bucket] == pytest.approx(0.2)
    assert written["netznoe:at001_restnetzbezug"][bucket] == pytest.approx(0.3)


async def test_no_extra_series_when_energy_community_is_disabled():
    """Without the option only the total consumption is recorded."""
    day = datetime(2026, 7, 1).date()
    written = await run_series_import(
        {
            day: day_data(
                ["2026-07-01T10:15:00"],
                metered=[0.5],
                grid_leftover=[0.3],
                self_coverage=[0.2],
            )
        },
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
    )

    assert list(written) == ["netznoe:at001"]


async def test_a_lost_session_is_restored_and_the_day_is_read_again():
    """Losing the session mid-import logs back in instead of dropping data."""
    day = datetime(2026, 7, 1).date()
    smartmeter = FakeSmartmeter(
        {day: day_data(["2026-07-01T10:15:00"], [0.5])},
        fail_until_relogin=True,
    )
    importer = Importer(MagicMock(), smartmeter, "AT001", "kWh")

    target = "custom_components.netznoe.importer.async_add_external_statistics"
    with patch(target) as add_statistics:
        await importer._import_ftm_statistics(
            utc(2026, 7, 1, 0), utc(2026, 7, 1, 23), {importer.id: Decimal(0)}
        )

    # The first attempt failed while logged out, so the client logged in again
    # and the reading still made it into the statistics.
    assert smartmeter.logins == 1
    buckets = {e["start"]: e["state"] for e in add_statistics.call_args[0][2]}
    assert buckets == {utc(2026, 7, 1, 10): pytest.approx(0.5)}


async def test_a_day_that_stays_unreadable_is_reported(caplog):
    """An import that silently loses days says so instead of looking clean."""

    class AlwaysLoggedOut(FakeSmartmeter):
        async def ensure_logged_in(self):
            """Fail to recover, the way an outage would."""

    day = datetime(2026, 7, 1).date()
    smartmeter = AlwaysLoggedOut(
        {day: day_data(["2026-07-01T10:15:00"], [0.5])},
        fail_until_relogin=True,
    )
    importer = Importer(MagicMock(), smartmeter, "AT001", "kWh")

    target = "custom_components.netznoe.importer.async_add_external_statistics"
    with patch(target) as add_statistics:
        await importer._import_ftm_statistics(
            utc(2026, 7, 1, 0), utc(2026, 7, 1, 23), {importer.id: Decimal(0)}
        )

    assert not add_statistics.called
    assert "this import is incomplete" in caplog.text


def test_late_update_reaches_back_to_the_start_of_the_previous_day():
    """The re-read window is the previous UTC day, matching the API's days."""
    assert Importer.previous_day_start(utc(2026, 9, 9, 8)) == utc(2026, 9, 8, 0)
    assert Importer.previous_day_start(utc(2026, 1, 15, 8)) == utc(2026, 1, 14, 0)


async def test_a_series_can_be_filled_in_without_rewriting_the_others():
    """Switching the split on later backfills it, leaving the total alone."""
    day = datetime(2026, 7, 1).date()
    written = await run_series_import(
        {
            day: day_data(
                ["2026-07-01T10:15:00"],
                metered=[0.5],
                grid_leftover=[0.3],
                self_coverage=[0.2],
            )
        },
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
        energy_community=True,
        series=["netznoe:at001_eigendeckung", "netznoe:at001_restnetzbezug"],
    )

    # The total already had history, so only the two new series are written.
    assert set(written) == {"netznoe:at001_eigendeckung", "netznoe:at001_restnetzbezug"}
    bucket = utc(2026, 7, 1, 10)
    assert written["netznoe:at001_eigendeckung"][bucket] == pytest.approx(0.2)
    assert written["netznoe:at001_restnetzbezug"][bucket] == pytest.approx(0.3)


def test_energy_community_is_ignored_for_daily_meters():
    """Meters without interval data have no community split to import."""
    importer = Importer(
        MagicMock(),
        MagicMock(),
        "AT001",
        "kWh",
        has_ftm_meter_data=False,
        energy_community=True,
    )

    assert importer.energy_community is False
    assert importer.statistic_ids() == ["netznoe:at001"]


def test_incremental_import_waits_an_hour_between_queries():
    """A fresh statistic suppresses the next query, an old one allows it."""
    importer = Importer(MagicMock(), MagicMock(), "AT001", "kWh")
    now = datetime.now(UTC)

    recent = {
        importer.id: [{"sum": 1.0, "end": (now - timedelta(minutes=30)).timestamp()}]
    }
    assert importer.prepare_start_off_point(recent) is None

    stale = {importer.id: [{"sum": 1.0, "end": (now - timedelta(hours=2)).timestamp()}]}
    start_off_point = importer.prepare_start_off_point(stale)
    assert start_off_point is not None
    assert start_off_point[1] == Decimal(1)
