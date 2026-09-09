"""Tests for the Netz NO statistics importer.

The Netz NO API reports the *end* of each 15 minute interval as a timestamp
without a UTC offset, in Austrian local time. Getting that wrong shifts every
reading into the wrong hour of the energy dashboard, so the mapping from an
API timestamp to a Home Assistant hourly bucket is what these tests pin down.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from custom_components.netznoe.importer import Importer


class FakeSmartmeter:
    """Return canned consumption data for a set of days."""

    def __init__(self, days: dict) -> None:
        """Store the per-day (times, values) responses."""
        self.days = days

    async def get_consumption_day(self, day, meter_id):  # noqa: ARG002
        """Return the canned response for a day, or nothing."""
        return self.days.get(day, ([], []))


def utc(year, month, day, hour):
    """Build a UTC timestamp at the top of an hour."""
    return datetime(year, month, day, hour, tzinfo=UTC)


async def run_import(days, start, end):
    """Run the FTM import and return the resulting {bucket: usage} mapping."""
    importer = Importer(MagicMock(), FakeSmartmeter(days), "AT001", "kWh")

    target = "custom_components.netznoe.importer.async_add_external_statistics"
    with patch(target) as add_statistics:
        await importer._import_ftm_statistics(start, end, Decimal(0))

    if not add_statistics.call_args:
        return {}
    return {entry["start"]: entry["state"] for entry in add_statistics.call_args[0][2]}


async def single_reading(day, timestamp, value=1.0):
    """Import one reading and return the bucket it landed in."""
    buckets = await run_import(
        {day: ([timestamp], [value])},
        start=datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(days=1),
        end=datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1),
    )
    assert len(buckets) == 1
    return next(iter(buckets))


async def test_summer_timestamp_is_read_as_local_time():
    """A CEST reading lands two hours earlier in UTC, not at face value."""
    bucket = await single_reading(datetime(2026, 7, 1).date(), "2026-07-01T12:15:00")

    # 12:15 CEST is 10:15 UTC, so the reading belongs to the 10:00 UTC hour.
    # Treating the naive timestamp as UTC would have put it at 12:00.
    assert bucket == utc(2026, 7, 1, 10)


async def test_winter_timestamp_uses_the_standard_time_offset():
    """The same wall clock time in CET is only one hour ahead of UTC."""
    bucket = await single_reading(datetime(2026, 1, 15).date(), "2026-01-15T12:15:00")

    assert bucket == utc(2026, 1, 15, 11)


async def test_interval_end_is_attributed_to_the_hour_it_covers():
    """A timestamp on the hour closes the previous interval, not the next one."""
    bucket = await single_reading(datetime(2026, 7, 1).date(), "2026-07-01T23:00:00")

    # 23:00 local ends the 22:45-23:00 interval, which belongs to the 22:00
    # local hour, i.e. 20:00 UTC.
    assert bucket == utc(2026, 7, 1, 20)


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
        {day: (times, [0.1, 0.2, 0.3, 0.4])},
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
    )

    # All four cover 10:00-11:00 local time, which is the 08:00 UTC hour.
    assert buckets == {utc(2026, 7, 1, 8): pytest.approx(1.0)}


async def test_autumn_dst_change_keeps_the_repeated_hour_apart():
    """The hour that occurs twice must not collapse into one bucket."""
    day = datetime(2026, 10, 25).date()
    # On 2026-10-25 the clock goes back from 03:00 CEST to 02:00 CET, so the
    # local times 02:15 through 03:00 are reported twice.
    times = [
        "2026-10-25T02:15:00",
        "2026-10-25T02:30:00",
        "2026-10-25T02:45:00",
        "2026-10-25T03:00:00",
        "2026-10-25T02:15:00",
        "2026-10-25T02:30:00",
        "2026-10-25T02:45:00",
        "2026-10-25T03:00:00",
    ]
    values = [0.1, 0.1, 0.1, 0.1, 0.2, 0.2, 0.2, 0.2]
    buckets = await run_import(
        {day: (times, values)},
        start=utc(2026, 10, 24, 0),
        end=utc(2026, 10, 26, 0),
    )

    # The first pass is CEST (UTC+2) and the second is CET (UTC+1), so they
    # occupy different UTC hours. Without disambiguating the repeated hour both
    # passes would land in 00:00 UTC and 01:00 UTC would be missing entirely.
    assert utc(2026, 10, 25, 0) in buckets
    assert utc(2026, 10, 25, 1) in buckets
    assert sum(buckets.values()) == pytest.approx(sum(values))


async def test_readings_before_the_start_are_skipped():
    """Hours already imported are not written a second time."""
    day = datetime(2026, 7, 1).date()
    times = ["2026-07-01T10:15:00", "2026-07-01T14:15:00"]
    buckets = await run_import(
        {day: (times, [0.5, 0.7])},
        start=utc(2026, 7, 1, 11),
        end=utc(2026, 7, 2, 0),
    )

    # 10:15 local is 08:00 UTC, before the start; 14:15 local is the 12:00 hour.
    assert buckets == {utc(2026, 7, 1, 12): pytest.approx(0.7)}


async def test_surplus_values_without_timestamps_are_ignored():
    """A short timestamp list must not crash or invent readings."""
    day = datetime(2026, 7, 1).date()
    buckets = await run_import(
        {day: (["2026-07-01T10:15:00"], [0.5, 0.7, 0.9])},
        start=utc(2026, 6, 30, 0),
        end=utc(2026, 7, 2, 0),
    )

    assert buckets == {utc(2026, 7, 1, 8): pytest.approx(0.5)}


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
