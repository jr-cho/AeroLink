import json
import math
from datetime import UTC, datetime
from uuid import UUID

import numpy as np
import pytest
from conftest import validator

from aerolink.contracts import Telemetry
from aerolink.sim.state import AircraftState, FlightPlan, SimPosition
from aerolink.sim.telemetry import M_PER_DEG_LAT, TelemetryNoise, seeded_uuid, telemetry_record

TS = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def must(*args, **kwargs) -> dict:
    """``telemetry_record`` for cases that must produce a record."""
    rec = telemetry_record(*args, **kwargs)
    assert rec is not None
    return rec


def ac(status="cruise", seed=0, **kw) -> AircraftState:
    a = AircraftState(
        aircraft_id="sim-p00",
        vehicle_type="passenger",
        home_node="ev-orl",
        status=status,
        current_node=None,
        position=SimPosition(28.5, -81.3, 800.0),
        rng=np.random.default_rng(seed),
        telemetry_rng=np.random.default_rng(seed + 1000),
        velocity_mps=60.0,
        heading_deg=45.0,
    )
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_record_validates_against_model_and_schema():
    a = ac()
    a.active_flight = FlightPlan(seeded_uuid(a.rng), seeded_uuid(a.rng), seeded_uuid(a.rng), ["x", "y"], "y", 0.0, {})
    rec = telemetry_record(a, TS)
    assert rec is not None
    Telemetry.model_validate_json(json.dumps(rec))
    validator("telemetry").validate(rec)
    assert rec["timestamp"] == "2026-11-20T12:00:00Z"
    assert rec["active_flight_id"] == str(a.active_flight.flight_id)


@pytest.mark.parametrize("status", ["parked", "turnaround"])
def test_no_record_on_pad(status):
    assert telemetry_record(ac(status), TS) is None


@pytest.mark.parametrize("status", ["climbing", "cruise", "descending", "holding", "landing"])
def test_record_when_airborne(status):
    assert telemetry_record(ac(status), TS) is not None


def test_zero_noise_is_exact():
    rec = must(ac(vertical_speed_mps=-3.0), TS, TelemetryNoise(0, 0, 0))
    assert rec["position"] == {"lat": 28.5, "lon": -81.3, "alt_msl_m": 800.0}
    assert rec["motion"] == {"ground_speed_mps": 60.0, "course_deg": 45.0, "vertical_speed_mps": -3.0}


def test_noise_has_configured_scale_in_metres():
    noise = TelemetryNoise(position_m=10.0, speed_mps=1.0)
    a = ac()
    dn, de, dv = [], [], []
    for _ in range(4000):
        r = must(a, TS, noise)
        dn.append((r["position"]["lat"] - 28.5) * M_PER_DEG_LAT)
        de.append((r["position"]["lon"] + 81.3) * M_PER_DEG_LAT * math.cos(math.radians(28.5)))
        dv.append(r["motion"]["ground_speed_mps"] - 60.0)
    assert np.std(dn) == pytest.approx(10, rel=0.05)
    assert np.std(de) == pytest.approx(10, rel=0.05)  # regression: lon scaling used to be wrong
    assert np.std(dv) == pytest.approx(1, rel=0.05)
    assert abs(np.mean(dn)) < 0.5 and abs(np.mean(de)) < 0.5


def test_dropout_rate_is_respected():
    a = ac()
    recs = [telemetry_record(a, TS, TelemetryNoise(dropout_rate=0.3)) for _ in range(5000)]
    assert sum(r is None for r in recs) / 5000 == pytest.approx(0.3, abs=0.03)
    assert all(r is None for r in (telemetry_record(a, TS, TelemetryNoise(dropout_rate=1.0)) for _ in range(20)))


def test_dropout_does_not_shift_noise_sequence():
    """Records that survive dropout carry the same noise as with no dropout."""
    keep = [telemetry_record(ac(seed=5), TS, TelemetryNoise(dropout_rate=0.0)) for _ in range(1)]
    a1, a2 = ac(seed=5), ac(seed=5)
    full = [telemetry_record(a1, TS, TelemetryNoise()) for _ in range(200)]
    dropped = [telemetry_record(a2, TS, TelemetryNoise(dropout_rate=0.5)) for _ in range(200)]
    assert keep
    for f, d in zip(full, dropped, strict=True):
        assert f is not None
        if d is not None:
            assert d["position"] == f["position"] and d["message_id"] == f["message_id"]


def test_speed_clamped_to_contract_range():
    for _ in range(100):
        r = must(ac(velocity_mps=0.0), TS, TelemetryNoise(speed_mps=50))
        assert 0 <= r["motion"]["ground_speed_mps"] <= 150
    r = must(ac(velocity_mps=200.0), TS, TelemetryNoise(0, 0))
    assert r["motion"]["ground_speed_mps"] == 150


def test_course_and_vertical_speed_normalised():
    assert must(ac(heading_deg=360.0), TS)["motion"]["course_deg"] == 0.0
    assert must(ac(heading_deg=-1e-17), TS)["motion"]["course_deg"] < 360
    assert must(ac(vertical_speed_mps=99), TS)["motion"]["vertical_speed_mps"] == 50


def test_seeded_uuid_is_deterministic_v4():
    u1 = seeded_uuid(np.random.default_rng(9))
    u2 = seeded_uuid(np.random.default_rng(9))
    assert u1 == u2 and isinstance(u1, UUID) and u1.version == 4


def test_message_ids_unique():
    a = ac()
    ids = {must(a, TS)["message_id"] for _ in range(1000)}
    assert len(ids) == 1000


def test_telemetry_does_not_touch_behaviour_rng():
    a = ac()
    state = a.rng.bit_generator.state
    telemetry_record(a, TS)
    assert a.rng.bit_generator.state == state


@pytest.mark.parametrize("kw", [{"position_m": -1}, {"speed_mps": -1}, {"dropout_rate": 1.1}, {"dropout_rate": -0.1}])
def test_invalid_noise(kw):
    with pytest.raises(ValueError):
        TelemetryNoise(**kw)
