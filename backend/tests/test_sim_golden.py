"""Golden digests for the committed ``nominal_day`` scenario (hardening plan §3).

The digests pin the full simulator output (telemetry, events, truth) for seed 42:
``GOLDEN`` for the scenario as committed, ``GOLDEN_ALL_OFF`` with every E3.3
world toggle off (the E3.2 behaviour, which later E3.3 work must not change).
A behaviour-neutral change must leave it unchanged. A deliberate behaviour change
updates ``GOLDEN`` and says why in the commit message.
"""

from pathlib import Path

from aerolink.network import Network
from aerolink.sim import load_scenario, run_simulation
from aerolink.sim.scenarios import DEFAULT_NETWORK
from aerolink.sim.world_config import WorldToggles

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "nominal_day.yaml"

# History (newest last):
#   8e2a4dc4... E3.2 baseline (T0 split, world toggles not wired)
#   d2d39f89... hardening C: range rules (destinations, diversions, destination
#               changes and medical dispatch must fit the battery; findings H2/H3/M3)
#   2968f68d... hardening C9: stands (aircraft clear the pad 60 s after touchdown;
#               finding M5). With every stand count 0 the output is still d2d39f89
#               (tests/test_sim_stands.py).
#   f0495770... network fix: corridor polylines no longer overshoot their end
#               waypoint and double back (airspace.build_network.remove_backtracks;
#               5 edges, up to 6.8 km shorter). Stand count 0 gives 2d0a47b1.
#   3fe1a5f4... E3.3 T1: separation switched on in nominal_day.yaml. All
#               toggles off is still f0495770 (GOLDEN_ALL_OFF).
#   1f291935... E3.3 T3/T4: pad booking and arrival metering switched on in
#               nominal_day.yaml. All toggles off is still f0495770.
GOLDEN = "855ce8ea2f6818086276be1c1fa16ede5322ccf3e9325c04fac74f597ea5c4dd"
GOLDEN_ALL_OFF = "9521a524c9430cd18879c3dc40b5dba4d122061e3b8d8998f181684214788d2d"


def test_nominal_day_golden_digest():
    scenario = load_scenario(SCENARIO)
    result = run_simulation(Network.from_geojson(DEFAULT_NETWORK), scenario, seed=42)
    assert result.digest() == GOLDEN


def test_nominal_day_all_toggles_off_golden_digest():
    scenario = load_scenario(SCENARIO).with_overrides(world=WorldToggles())
    result = run_simulation(Network.from_geojson(DEFAULT_NETWORK), scenario, seed=42)
    assert result.digest() == GOLDEN_ALL_OFF
