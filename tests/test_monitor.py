from datetime import datetime
from zoneinfo import ZoneInfo

import monitor


def test_dates_are_tomorrow_through_august_6():
    dates = monitor.get_monitor_dates(datetime(2026, 7, 30, 12, tzinfo=ZoneInfo("America/New_York")))
    assert dates[0].isoformat() == "2026-07-31"
    assert dates[-1].isoformat() == "2026-08-06"
    assert len(dates) == 7


def test_strict_context():
    assert monitor.is_qualifying_context("AMC Lincoln Square 13 — IMAX 70mm — 7:00 PM Tickets")
    assert not monitor.is_qualifying_context("AMC Lincoln Square 13 — 70mm — 7:00 PM")
    assert not monitor.is_qualifying_context("AMC Empire 25 — IMAX 70mm")
    assert not monitor.is_qualifying_context("AMC Lincoln Square 13 — IMAX with Laser")


def test_available_seat_filter():
    assert monitor.is_standard_available_seat("Seat H12 available")
    assert not monitor.is_standard_available_seat("Wheelchair seat H12 available")
    assert not monitor.is_standard_available_seat("Companion seat H12")
    assert not monitor.is_standard_available_seat("Seat H12 occupied")
    assert not monitor.is_standard_available_seat("Seat H12", disabled=True)
