"""E5.7: the AeroLink engine sees live traffic, configs switch features, the table is written."""

from pathlib import Path

from aerolink.eval.ablations import END, START, markdown, run_one, summarize, update_docs
from aerolink.forecast.cv import CVForecaster
from aerolink.network import Network
from aerolink.routing.aerolink import ABLATIONS, AeroLinkAdvisor, AeroLinkRouteAdvisorWrapper
from aerolink.sim import load_scenario, run_simulation

BACKEND = Path(__file__).resolve().parents[1]


def test_configs_switch_features():
    assert not ABLATIONS["A2"].demand and ABLATIONS["A3"].demand
    assert ABLATIONS["A4"].off_route and not ABLATIONS["A4"].en_route
    assert ABLATIONS["A5"].risk_ranking and ABLATIONS["A5"].en_route


def test_risk_only_counts_in_a5(fl_network: Network):
    a4 = AeroLinkAdvisor(fl_network, CVForecaster(fl_network), ABLATIONS["A4"])
    a5 = AeroLinkAdvisor(fl_network, CVForecaster(fl_network), ABLATIONS["A5"])
    assert a4.cfg.mu_risk_s == 0.0 and a5.cfg.mu_risk_s == 600.0


def test_engine_is_bound_to_the_world(fl_network: Network):
    adv = AeroLinkAdvisor(fl_network, CVForecaster(fl_network), ABLATIONS["A4"])
    result = run_simulation(fl_network, load_scenario(BACKEND / "scenarios" / "nominal_day.yaml"),
                            advisor=AeroLinkRouteAdvisorWrapper(adv))  # fmt: skip
    assert adv.world is result.final_world
    assert sum(e["kind"] == "arrival" for e in result.events) > 0


def test_run_one_and_table(fl_network: Network, tmp_path: Path):
    row = run_one(fl_network, BACKEND / "scenarios" / "nominal_day.yaml", "A1", 42, gbm=None, demand=None)
    assert row["config"] == "A1" and row["trips"] > 0
    table = markdown(summarize([row]), None, 1)
    assert "| A1 |" in table
    doc = tmp_path / "ARCH.md"
    doc.write_text(f"before\n{START}\nstale table\n{END}\nafter\n")
    assert update_docs(table, doc)
    assert "stale table" not in doc.read_text() and "| A1 |" in doc.read_text() and doc.read_text().endswith("after\n")
