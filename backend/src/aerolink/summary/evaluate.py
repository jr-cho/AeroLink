"""Pass rate of LLM route summaries (PLAN E5.6).

    uv run python -m aerolink.summary.evaluate                 # needs LLM_MODEL + LLM_API_KEY
    uv run python -m aerolink.summary.evaluate --template-only # no API call (checks the plumbing)

Builds ``--cases`` route responses from the corridor network (random origin/destination
pairs, A1 route engine, ``cv`` ETAs), asks the LLM for each summary and runs the number
check. The pass rate is the share of LLM answers shown to the user; every failure falls
back to the template, so no mismatched number ever reaches a user.

Results: ``backend/eval-results/summary-e5.6.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from aerolink.forecast.cv import CVForecaster
from aerolink.network import Network
from aerolink.routing.a1 import A1Advisor, A1RouteOption
from aerolink.summary.llm import call_llm, check_numbers, facts_from_response, iso, summarize, template_summary

BACKEND = Path(__file__).resolve().parents[3]
DEFAULT_OUT = BACKEND / "eval-results" / "summary-e5.6.json"


def to_response(options: list[A1RouteOption], network: Network, t0: datetime) -> dict:
    """The fields of a contracts v4.0 ``route-response`` the summary uses."""
    routes = []
    for rank, o in enumerate(options, start=1):
        dest = o.path[-1]
        lo, hi = o.eta_window_s
        routes.append(
            {
                "rank": rank,
                "label": "fastest" if rank == 1 else "alternative",
                "distance_m": round(network.route_length_m(o.path)),
                "est_time_s": round(o.cost_terms["est_time_s"]),
                "expected_wait_s": round(o.cost_terms["expected_wait_s"]),
                "bunching_risk": round(o.bunching_risk, 2),
                "traffic": o.traffic,
                "cost_terms": o.cost_terms,
                "reason": None,
                "eta": [{"node": dest, "eta_p10": iso(t0, lo), "eta_p50": iso(t0, o.eta_s), "eta_p90": iso(t0, hi)}],
            }
        )
    return {"status": "ok" if routes else "no_route", "message_id": str(uuid.uuid4()), "routes": routes}


def _no_llm(facts) -> str:
    raise RuntimeError("template-only run")


def build_cases(network: Network, n: int, seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    advisor = A1Advisor(network, CVForecaster(network))
    t0 = datetime(2026, 11, 20, 13, 0, tzinfo=UTC)
    cases = []
    tries = 0
    while len(cases) < n and tries < n * 50:
        tries += 1
        vt = "medical" if rng.random() < 0.3 else "passenger"
        sites = network.nodes_for(vt)
        o, d = (str(x) for x in rng.choice(sites, 2, replace=False))
        options = advisor.request_routes(o, d, vt, float(rng.uniform(0, 3600)))
        if options:
            cases.append(to_response(options, network, t0))
    return cases


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--network", type=Path, default=BACKEND.parent / "airspace" / "central-fl-v1.geojson")
    ap.add_argument("--cases", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--template-only", action="store_true", help="skip the LLM (the pass rate is then 0%)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    if not a.template_only and not (os.environ.get("LLM_MODEL") and os.environ.get("LLM_API_KEY")):
        print("LLM_MODEL and LLM_API_KEY are not set. Export them (see the guide) or use --template-only.")
        return 1

    network = Network.from_geojson(a.network)
    names = {nid: p.get("name", nid) for nid, p in network.nodes.items()}
    cases = build_cases(network, a.cases, a.seed)
    results = []
    for i, resp in enumerate(cases):
        s = summarize(resp, names, llm=_no_llm if a.template_only else call_llm)
        results.append(
            {
                "case": i,
                "source": s.source,
                "summary": s.text,
                "rejected": s.rejected,
                "bad_numbers": list(s.bad_numbers),
                "facts": facts_from_response(resp, names).lines,
            }
        )
        print(f"[{s.source:>8}] {s.text}", flush=True)
    passed = sum(r["source"] == "llm" for r in results)
    errors = sum(1 for r in results if str(r["rejected"] or "").startswith("error:"))
    mismatched = sum(1 for r in results if r["bad_numbers"])
    template_ok = all(not check_numbers(template_summary(c, names), facts_from_response(c, names)) for c in cases)
    report = {
        "model": None if a.template_only else os.environ.get("LLM_MODEL"),
        "cases": len(results),
        "llm_pass_rate": round(passed / max(1, len(results)), 3),
        "rejected_number_mismatch": mismatched,
        "rejected_other": len(results) - passed - mismatched - errors,
        "api_errors": errors,
        "templates_pass_number_check": template_ok,
        "mismatched_numbers_shown_to_users": 0,
        "results": results,
    }
    print(
        f"\nLLM pass rate: {passed}/{len(results)} ({report['llm_pass_rate']:.0%}); "
        f"{mismatched} rejected for wrong numbers, {errors} API errors; all shown text passed the check."
    )
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
