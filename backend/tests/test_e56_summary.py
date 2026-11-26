"""E5.6: route summaries never show a number that isn't in the route data."""

import json
from pathlib import Path

from aerolink.summary.evaluate import main
from aerolink.summary.llm import check_numbers, facts_from_response, summarize, template_summary

REPO = Path(__file__).resolve().parents[2]
RESPONSE = json.loads((REPO / "contracts/examples/route-response/valid/ok-two-options.json").read_text())


def test_facts_allow_route_numbers_and_differences():
    facts = facts_from_response(RESPONSE)
    assert {"19", "15:19", "3"} <= facts.allowed  # 1140 s = 19 min; arrival 15:19; 22 - 19 = 3 min
    assert check_numbers("Arrive 15:19, about 19 min, 3 min faster.", facts) == []
    assert check_numbers("Arrive 15:45 after 25 min.", facts) == ["15:45", "25"]


def test_good_llm_text_is_used():
    s = summarize(RESPONSE, llm=lambda facts: "Fastest: 19 min, arriving 15:19. The other option is 3 min slower.")
    assert s.source == "llm"


def test_wrong_number_falls_back_to_template():
    s = summarize(RESPONSE, llm=lambda facts: "Fastest route takes 12 min.")
    assert s.source == "template" and s.bad_numbers == ("12",)
    assert check_numbers(s.text, facts_from_response(RESPONSE)) == []


def test_api_error_falls_back_to_template():
    def broken(facts):
        raise TimeoutError("no network")

    s = summarize(RESPONSE, llm=broken)
    assert s.source == "template" and s.text == template_summary(RESPONSE)


def test_template_only_run(tmp_path: Path):
    out = tmp_path / "e56.json"
    assert main(["--template-only", "--cases", "5", "--out", str(out)]) == 0
    r = json.loads(out.read_text())
    assert r["cases"] == 5 and r["templates_pass_number_check"] is True
