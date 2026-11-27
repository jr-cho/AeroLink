"""R1 checkpoint 1: the overview's facts, bunches and template bullets, from hand-built routes and forecasts.

Pure functions only: no app, no LLM, no clock. ``NOW`` is 2026-11-28 18:40 New York time (EST).
"""

from __future__ import annotations

from datetime import UTC, datetime

from aerolink.service import ops_overview as ov
from aerolink.summary.llm import check_numbers

NOW = datetime(2026, 11, 28, 23, 40, tzinfo=UTC).timestamp()  # 6:40 PM in New York
STEPS = list(range(0, 50, 5))
CALM = ["low"] * 10


def at(minutes: float) -> float:
    return NOW + minutes * 60


def route(
    callsign: str,
    dest: str,
    p10: float,
    p50: float,
    p90: float,
    *,
    status: str = "planned",
    medical: bool = False,
    path: list[str] | None = None,
    atc: dict | None = None,
    flight_id: str | None = None,
) -> dict:
    return {
        "flight_id": flight_id or f"fid-{callsign}",
        "callsign": callsign,
        "status": status,
        "destination": dest,
        "medical": medical,
        "path": path or [dest],
        "eta_p10": p10,
        "eta_p50": p50,
        "eta_p90": p90,
        "nodes": [{"node": dest, "eta_p10": p10, "eta_p50": p50, "eta_p90": p90}],
        "atc": atc or {"requirement": "none", "status": "not_needed", "legs": []},
    }


def site(node: str, name: str, pads: int, levels: list[str] | None = None) -> dict:
    return {"node": node, "name": name, "pads": pads, "levels": levels or CALM}


def congestion(sites: list[dict], edges: list[dict] | None = None) -> dict:
    return {"steps_min": STEPS, "sites": sites, "edges": edges or []}


def busy_from(minute_from: int, level: str = "moderate", upto: int = 45) -> list[str]:
    """Levels for the 10 slots: ``level`` from ``minute_from`` up to ``upto`` minutes, calm elsewhere."""
    return [level if minute_from <= m < upto else "low" for m in STEPS]


def reroute(rid: str, callsign: str, trigger: str = "faster", gain_s: float | None = 240.0) -> dict:
    return {"id": rid, "callsign": callsign, "trigger": trigger, "gain_s": gain_s, "status": "proposed"}


def facts(routes=(), cong=None, reroutes=(), highway=None, node_names=None):
    return ov.build_facts(
        NOW, list(routes), cong or congestion([]), list(reroutes), highway=highway, node_names=node_names
    )


# --- clock and sentences -----------------------------------------------------------------------------------
def test_clock_is_h_mm_am_pm_in_new_york():
    assert ov.clock(at(0)) == "6:40 PM"
    assert ov.clock(datetime(2026, 11, 29, 5, 5, tzinfo=UTC).timestamp()) == "12:05 AM"  # midnight in New York
    assert ov.clock(datetime(2026, 11, 28, 17, 0, tzinfo=UTC).timestamp()) == "12:00 PM"


# --- quiet -------------------------------------------------------------------------------------------------
def test_quiet_case_is_one_template_bullet():
    f = facts(
        [route("AL-01", "ev-orl", at(5), at(10), at(15), status="active")],
        congestion([site("ev-orl", "Orlando Exec", 2)]),
    )
    assert f == [{"kind": "quiet"}]
    assert ov.template_bullets(f) == ["Traffic is light everywhere for the next 30 min."]


# --- bunches -----------------------------------------------------------------------------------------------
def test_two_overlapping_arrivals_are_a_bunch_without_a_pad_wait():
    rs = [
        route("AL-02", "ev-orl", at(10), at(12), at(14)),
        route("AL-07", "ev-orl", at(11), at(14), at(17)),
    ]
    (b,) = [f for f in facts(rs, congestion([site("ev-orl", "Orlando Exec", 2)])) if f["kind"] == "bunch"]
    assert b["callsigns"] == ["AL-02", "AL-07"] and b["within_min"] == 2 and b["pads"] == 2
    assert b["from_s"] == at(10) and b["to_s"] == at(17)
    text = ov.sentence(b)
    assert text == "Orlando Exec: AL-02 and AL-07 arrive within 2 min."
    assert "pad wait" not in text


def test_a_pad_wait_is_mentioned_only_when_the_bunch_is_bigger_than_the_pads():
    rs = [route(f"AL-0{i}", "ev-orl", at(10), at(12 + i), at(14)) for i in range(3)]
    cong = congestion([site("ev-orl", "Orlando Exec", 2)])
    (b,) = [f for f in facts(rs, cong) if f["kind"] == "bunch"]
    assert "expect a pad wait (3 arrivals, 2 pads)" in ov.sentence(b)
    rs2 = rs[:2]
    (b2,) = [f for f in facts(rs2, cong) if f["kind"] == "bunch"]
    assert "pad wait" not in ov.sentence(b2)


def test_overlap_is_transitive():
    rs = [
        route("A", "ev-lal", at(5), at(6), at(8)),
        route("B", "ev-lal", at(7), at(9), at(12)),
        route("C", "ev-lal", at(11), at(13), at(15)),  # overlaps B, not A
    ]
    (b,) = [f for f in facts(rs, congestion([site("ev-lal", "Lakeland", 2)])) if f["kind"] == "bunch"]
    assert b["callsigns"] == ["A", "B", "C"] and b["within_min"] == 7


def test_separated_arrivals_make_no_bunch():
    rs = [route("A", "ev-lal", at(5), at(6), at(8)), route("B", "ev-lal", at(20), at(21), at(22))]
    assert [f for f in facts(rs, congestion([site("ev-lal", "Lakeland", 2)])) if f["kind"] == "bunch"] == []


def test_a_bunch_at_another_site_is_not_mixed_in():
    rs = [route("A", "ev-lal", at(5), at(6), at(8)), route("B", "ev-suntrax", at(5), at(6), at(8))]
    assert [f for f in facts(rs, congestion([site("ev-lal", "Lakeland", 2)])) if f["kind"] == "bunch"] == []


def test_arrivals_outside_the_horizon_or_finished_are_ignored():
    rs = [
        route("A", "ev-lal", at(40), at(41), at(42)),  # beyond +30 min
        route("B", "ev-lal", at(40), at(41), at(42)),
        route("C", "ev-lal", at(-30), at(-20), at(-10), status="active"),  # already past
    ]
    assert [f for f in facts(rs, congestion([site("ev-lal", "Lakeland", 2)])) if f["kind"] == "bunch"] == []


# --- busy sites and heavy corridors -----------------------------------------------------------------------
def test_busy_site_window_comes_from_the_slots_inside_the_horizon():
    cong = congestion([site("ev-orl", "Orlando Exec", 2, busy_from(10, "heavy", upto=20))])
    rs = [route("AL-02", "ev-orl", at(12), at(12), at(13))]
    (f,) = [x for x in facts(rs, cong) if x["kind"] == "busy_site"]
    # heavy in the 5-min slots starting at +10 and +15; the last slot ends at +20
    assert f["level"] == "heavy" and f["from_s"] == at(10) and f["to_s"] == at(20)
    assert f["arrivals"] == 1 and f["pads"] == 2
    assert ov.sentence(f) == "Orlando Exec: heavy traffic forecast 6:50 to 7:00 PM (1 arrival due)."


def test_busy_slots_beyond_the_horizon_do_not_count():
    cong = congestion([site("ev-orl", "Orlando Exec", 2, busy_from(35, "moderate"))])
    assert [x for x in facts([], cong) if x["kind"] == "busy_site"] == []


def test_heavy_corridor_names_the_flights_crossing_it():
    edge = {"edge": ["wp-a", "wp-b"], "levels": busy_from(0, "heavy", upto=15), "peak": "heavy"}
    rs = [
        route("N999AL", "ev-lal", at(3), at(9), at(12), path=["ev-x", "wp-a", "wp-b", "ev-lal"]),
        route("N111AL", "ev-lal", at(3), at(9), at(12), path=["ev-y", "wp-b", "wp-a", "ev-lal"]),
        route("N222AL", "ev-lal", at(3), at(9), at(12), path=["ev-z", "wp-c", "ev-lal"]),
    ]
    rs[0]["nodes"] += [{"node": "wp-a", "eta_p10": at(2), "eta_p50": at(4), "eta_p90": at(6)}]
    rs[1]["nodes"] += [{"node": "wp-b", "eta_p10": at(2), "eta_p50": at(4), "eta_p90": at(6)}]
    cong = congestion([site("ev-lal", "Lakeland", 2)], [edge])
    (f,) = [
        x
        for x in facts(
            rs, cong, highway=lambda u, v: "I-4", node_names={"wp-a": "Celebration", "wp-b": "Kissimmee (I-4)"}
        )
        if x["kind"] == "heavy_corridor"
    ]
    assert f["name"] == "I-4 between Celebration and Kissimmee" and f["edge"] == "wp-a|wp-b"
    assert f["flights"] == ["N999AL", "N111AL"]  # N222AL does not use the edge
    assert (
        ov.sentence(f)
        == "I-4 between Celebration and Kissimmee is heavy 6:40 PM to 6:55 PM with N999AL and N111AL on it."
    )


def test_heavy_corridor_default_name_uses_site_names():
    edge = {"edge": ["ev-lal", "wp-x"], "levels": busy_from(0, "heavy", upto=10), "peak": "heavy"}
    cong = congestion([site("ev-lal", "Lakeland", 2)], [edge])
    (f,) = [x for x in facts([], cong) if x["kind"] == "heavy_corridor"]
    assert f["name"] == "Lakeland to wp-x" and f["flights"] == []


# --- reroutes and clearances ---------------------------------------------------------------------------------
def test_open_reroute_becomes_a_fact_with_minutes_saved():
    (f,) = facts(reroutes=[reroute("rr_1", "N999AL", gain_s=250.0)])
    assert f["kind"] == "reroute" and f["reroute_id"] == "rr_1" and f["gain_s"] == 250.0
    assert ov.sentence(f) == "A reroute for N999AL waits for approval, saves 4 min."


def test_pending_clearance_due_within_the_horizon_is_a_fact():
    atc = {"requirement": "clearance", "status": "pending", "legs": [["wp-a", "wp-b"]]}
    r = route("N123AL", "ev-lal", at(20), at(25), at(28), path=["ev-x", "wp-a", "wp-b", "ev-lal"], atc=atc)
    r["nodes"].append({"node": "wp-a", "eta_p10": at(15), "eta_p50": at(16), "eta_p90": at(17)})
    (f,) = [x for x in facts([r]) if x["kind"] == "clearance"]
    assert (
        f["count"] == 1
        and f["earliest_due_s"] == at(16)
        and f["flight_ids"] == ["fid-N123AL"]
        and f["callsigns"] == ["N123AL"]
    )
    assert ov.sentence(f) == "N123AL needs ATC clearance by 6:56 PM."


def test_clearance_that_is_done_or_not_needed_is_not_a_fact():
    done = {"requirement": "contact_tower", "status": "done", "legs": [["a", "b"]]}
    none = {"requirement": "none", "status": "not_needed", "legs": []}
    rs = [route("A", "ev-lal", at(5), at(6), at(8), atc=done), route("B", "ev-lal", at(5), at(6), at(8), atc=none)]
    assert [x for x in facts(rs) if x["kind"] == "clearance"] == []


# --- ordering, medical first -------------------------------------------------------------------------------
def test_medical_facts_come_first_then_the_kind_order():
    cong = congestion([site("ev-orl", "Orlando Exec", 2, busy_from(0)), site("ev-lal", "Lakeland", 2, busy_from(0))])
    rs = [
        route("AL-02", "ev-orl", at(10), at(12), at(14)),
        route("AL-07", "ev-orl", at(11), at(14), at(17)),
        route("MED-1", "ev-lal", at(4), at(5), at(6), medical=True),
    ]
    out = facts(rs, cong, [reroute("rr_1", "N999AL"), reroute("rr_m", "MED-1", trigger="medical")])
    # medical facts lead (the Lakeland busy_site, whose destination has a medical arrival, then the medical
    # reroute); then the rest in kind order: bunch, busy_site, reroute
    assert [(f["kind"], f.get("medical")) for f in out] == [
        ("busy_site", True),
        ("reroute", True),
        ("bunch", False),
        ("busy_site", False),
        ("reroute", False),
    ]


def test_a_flight_whose_window_only_touches_the_horizon_counts():
    rs = [route("A", "ev-lal", at(28), at(31), at(40)), route("B", "ev-lal", at(29), at(33), at(41))]
    (b,) = [f for f in facts(rs, congestion([site("ev-lal", "Lakeland", 2)])) if f["kind"] == "bunch"]
    assert b["callsigns"] == ["A", "B"]


def test_bunch_comes_before_busy_site_for_the_same_priority_class():
    cong = congestion([site("ev-orl", "Orlando Exec", 2, busy_from(0))])
    rs = [route("A", "ev-orl", at(10), at(12), at(14)), route("B", "ev-orl", at(11), at(14), at(17))]
    out = facts(rs, cong)
    assert [f["kind"] for f in out] == ["bunch", "busy_site"]


# --- bullets and the number check -----------------------------------------------------------------------
def test_template_bullets_pass_the_number_check_for_every_fact_kind():
    edge = {"edge": ["wp-a", "wp-b"], "levels": busy_from(0, "heavy", upto=15), "peak": "heavy"}
    cong = congestion([site("ev-orl", "Orlando Exec", 2, busy_from(0)), site("ev-lal", "Lakeland (I-4)", 1)], [edge])
    atc = {"requirement": "contact_tower", "status": "pending", "legs": [["wp-a", "wp-b"]]}
    rs = [
        route("AL-02", "ev-orl", at(10), at(12), at(14), path=["ev-x", "wp-a", "wp-b", "ev-orl"]),
        route("AL-07", "ev-orl", at(11), at(14), at(17), path=["ev-x", "wp-a", "wp-b", "ev-orl"], medical=True),
        route("AL-09", "ev-lal", at(15), at(18), at(20), atc=atc, path=["ev-x", "wp-a", "wp-b", "ev-lal"]),
    ]
    rs[0]["nodes"].append({"node": "wp-a", "eta_p10": at(4), "eta_p50": at(5), "eta_p90": at(6)})
    out = facts(rs, cong, [reroute("rr_1", "N999AL")], highway=lambda u, v: "I-4")
    kinds = {f["kind"] for f in out}
    assert {"bunch", "busy_site", "heavy_corridor", "reroute", "clearance"} <= kinds
    lines = [ov.sentence(f) for f in out]
    llm_facts = ov.facts_for_llm(out)
    bullets = ov.template_bullets(out)
    assert len(bullets) <= ov.MAX_BULLETS
    for b in bullets:
        assert len(b) <= ov.MAX_CHARS and "\n" not in b
        assert check_numbers(b, llm_facts) == [], b
    assert all(line in llm_facts.lines for line in lines)


def test_a_number_outside_the_facts_fails_the_check():
    out = facts(
        [route("AL-02", "ev-orl", at(10), at(12), at(14)), route("AL-07", "ev-orl", at(11), at(14), at(17))],
        congestion([site("ev-orl", "Orlando Exec", 2)]),
    )  # noqa: E501
    llm_facts = ov.facts_for_llm(out)
    assert check_numbers("Orlando Exec: 3 arrivals within 2 min.", llm_facts) == ["3"]
    assert check_numbers("Orlando Exec: AL-02 and AL-07 within 2 min.", llm_facts) == []


def test_bullets_are_capped_at_four_and_long_text_is_cut_at_a_word():
    out = [
        {
            "kind": "reroute",
            "reroute_id": str(i),
            "callsign": f"N{i}AL",
            "gain_s": 60.0,
            "status": "proposed",
            "medical": False,
        }
        for i in range(6)
    ]
    assert len(ov.template_bullets(out)) == 4
    long_text = "word " * 60
    cut = ov.fit(long_text)
    assert len(cut) <= ov.MAX_CHARS and cut.endswith("word.") and "..." not in cut


# --- orchestrator follow-ups (checkpoint 1 accepted with these) ------------------------------------------
def _heavy_edge(u: str, v: str, minutes: int = 15) -> dict:
    return {"edge": [u, v], "levels": busy_from(0, "heavy", upto=minutes), "peak": "heavy"}


def test_a_long_highway_heavy_stretch_is_one_fact_named_by_its_ends():
    edges = [_heavy_edge("n1", "n2"), _heavy_edge("n2", "n3"), _heavy_edge("n3", "n4")]
    cong = congestion([], edges)
    names = {"n1": "Celebration", "n2": "Mid", "n3": "Lake Buena Vista", "n4": "Kissimmee (I-4)"}
    out = [f for f in facts([], cong, highway=lambda u, v: "I-4", node_names=names) if f["kind"] == "heavy_corridor"]
    assert len(out) == 1
    assert out[0]["name"] == "I-4 between Celebration and Kissimmee" and len(out[0]["edges"]) == 3


def test_separate_heavy_stretches_of_one_highway_are_separate_facts():
    edges = [_heavy_edge("n1", "n2"), _heavy_edge("n8", "n9")]  # not touching: two spans
    cong = congestion([], edges)
    names = {"n1": "A", "n2": "B", "n8": "C", "n9": "D"}
    out = [f for f in facts([], cong, highway=lambda u, v: "I-4", node_names=names) if f["kind"] == "heavy_corridor"]
    assert [f["name"] for f in out] == ["I-4 between A and B", "I-4 between C and D"]


def test_a_connector_without_a_highway_is_named_a_to_b():
    edges = [_heavy_edge("ev-lal", "wp-x")]
    out = [
        f for f in facts([], congestion([], edges), node_names={"wp-x": "Auburndale"}) if f["kind"] == "heavy_corridor"
    ]
    assert out[0]["name"] == "ev-lal to Auburndale"


def test_an_off_route_flight_still_counts_in_a_bunch_and_a_clearance():
    atc = {"requirement": "clearance", "status": "pending", "legs": [["wp-a", "wp-b"]]}
    a = route("AL-02", "ev-lal", at(10), at(12), at(14), status="off_route")
    b = route("AL-07", "ev-lal", at(11), at(14), at(17), status="off_route", atc=atc)
    out = facts([a, b], congestion([site("ev-lal", "Lakeland", 2)]))
    bunch = next(f for f in out if f["kind"] == "bunch")
    assert bunch["callsigns"] == ["AL-02", "AL-07"]
    clear = next(f for f in out if f["kind"] == "clearance")
    assert clear["callsigns"] == ["AL-07"]


def test_clearances_with_the_same_requirement_and_minute_are_one_bullet():
    atc = {"requirement": "clearance", "status": "pending", "legs": [["wp-a", "wp-b"]]}
    rs = []
    for cs in ["AL-01", "AL-02"]:
        r = route(cs, "ev-lal", at(20), at(25), at(28), path=["wp-a", "wp-b", "ev-lal"], atc=atc)
        r["nodes"].append({"node": "wp-a", "eta_p10": at(15), "eta_p50": at(16), "eta_p90": at(17)})
        rs.append(r)
    out = [f for f in facts(rs) if f["kind"] == "clearance"]
    assert len(out) == 1 and out[0]["callsigns"] == ["AL-01", "AL-02"]
    assert ov.template_bullets(out) == ["2 flights need ATC clearance; earliest due 6:56 PM (AL-01)."]


def test_more_than_two_clearances_in_one_group_say_and_n_more():
    atc = {"requirement": "clearance", "status": "pending", "legs": [["wp-a", "wp-b"]]}
    rs = []
    for cs in ["AL-01", "AL-02", "AL-03", "AL-04"]:
        r = route(cs, "ev-lal", at(20), at(25), at(28), path=["wp-a", "wp-b", "ev-lal"], atc=atc)
        r["nodes"].append({"node": "wp-a", "eta_p10": at(15), "eta_p50": at(16), "eta_p90": at(17)})
        rs.append(r)
    (f,) = [x for x in facts(rs) if x["kind"] == "clearance"]
    assert ov.sentence(f) == "4 flights need ATC clearance; earliest due 6:56 PM (AL-01)."


def test_a_trailing_parenthetical_is_dropped_from_site_names_in_sentences():
    cong = congestion([site("hp-x", "SunTrax (FDOT AAM test site)", 2, busy_from(0))])
    rs = [route("AL-05", "hp-x", at(10), at(12), at(14)), route("AL-06", "hp-x", at(11), at(14), at(17))]
    out = facts(rs, cong)
    assert ov.template_bullets(out)[0].startswith("SunTrax: AL-05 and AL-06 arrive within")
    busy = next(f for f in out if f["kind"] == "busy_site")
    assert busy["name"] == "SunTrax (FDOT AAM test site)"  # the fact keeps the full name
    assert ov.sentence(busy).startswith("SunTrax: moderate traffic forecast")
