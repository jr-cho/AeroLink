from datetime import UTC, datetime, timedelta

import pytest

from aerolink.sim.clock import SimClock

START = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_starts_at_zero():
    c = SimClock(START, 1.0)
    assert c.t == 0.0 and c.step == 0 and c.now() == START


def test_tick_is_immutable_and_advances():
    c = SimClock(START, 0.5)
    c2 = c.tick()
    assert c.step == 0 and c2.step == 1 and c2.t == 0.5


def test_no_float_drift_over_long_runs():
    c = SimClock(START, 0.1)
    for _ in range(36_000):
        c = c.tick()
    assert c.t == pytest.approx(3600.0, abs=1e-9)
    assert c.now() == START + timedelta(hours=1)


@pytest.mark.parametrize("dt", [0.0, -1.0])
def test_rejects_non_positive_dt(dt):
    with pytest.raises(ValueError):
        SimClock(START, dt)


def test_rejects_naive_start_and_negative_step():
    with pytest.raises(ValueError):
        SimClock(datetime(2026, 1, 1), 1.0)
    with pytest.raises(ValueError):
        SimClock(START, 1.0, step=-1)


def test_from_iso_keeps_offset_and_reports_utc_now():
    c = SimClock.from_iso("2026-11-20T07:00:00-05:00", 1.0)
    assert c.local_hour() == pytest.approx(7.0)
    assert c.now() == datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert c.now().tzinfo == UTC


def test_from_iso_naive_is_utc():
    c = SimClock.from_iso("2026-11-20T07:00:00", 1.0)
    assert c.now() == datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


def test_local_hour_wraps_past_midnight():
    c = SimClock.from_iso("2026-11-20T23:30:00-05:00", 60.0)
    for _ in range(60):
        c = c.tick()
    assert c.local_hour() == pytest.approx(0.5)
