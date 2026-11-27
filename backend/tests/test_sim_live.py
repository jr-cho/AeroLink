"""Simulator live mode (PLAN E6.4): pacing and re-stamping keep the offline behaviour."""

from datetime import UTC, datetime

from aerolink.sim.live import restamp
from aerolink.sim.runner import run_simulation
from aerolink.sim.scenarios import Scenario


def test_restamp_keeps_offsets_and_order(fl_network):
    result = run_simulation(fl_network, Scenario(duration_s=600, fleet={"passenger": 2, "medical": 1}), seed=1)
    start = datetime(2030, 1, 1, tzinfo=UTC)
    plan = restamp(result.telemetry, start)
    assert len(plan) == len(result.telemetry) > 0
    offsets = [o for o, _ in plan]
    assert offsets == sorted(offsets) and offsets[0] == 0.0
    first = datetime.fromisoformat(plan[0][1]["timestamp"].replace("Z", "+00:00"))
    assert first == start
    fast = restamp(result.telemetry, start, speed=10)
    assert fast[-1][0] == offsets[-1] / 10  # compressed in wall time
    assert len({r["message_id"] for _, r in plan}) == len(plan)  # fresh ids: no dedup collisions across loops
    # Everything except time and id is the offline record, unchanged.
    by_key = {(r["aircraft_id"], r["position"]["lat"]): r for r in result.telemetry}
    for _, r in plan[:50]:
        orig = by_key[(r["aircraft_id"], r["position"]["lat"])]
        assert {k: v for k, v in r.items() if k not in ("timestamp", "message_id")} == {
            k: v for k, v in orig.items() if k not in ("timestamp", "message_id")
        }
