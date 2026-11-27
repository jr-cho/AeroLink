"""AI overview for the ATC screen (PLAN R1, contract R-contract §1, §4, §5).

The pure part: structured facts computed from data the ops endpoints already return (routes, congestion, open
reroutes); template sentences rendered from those facts; the language model only phrases the same facts, and every
number it writes must appear in the facts (the number check).

Nothing here calls a model, touches the event loop or changes a flight. Times are rendered h:mm AM/PM in
America/New_York, like the rest of the UI.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from aerolink.service import ops as ops_api
from aerolink.service.auth import require_operator
from aerolink.service.planner import FL_TZ
from aerolink.service.reroute import open_list
from aerolink.summary import llm as llm_mod
from aerolink.summary.llm import NUMBER, Facts, check_numbers

log = logging.getLogger("aerolink.service.ops_overview")


def unavailable() -> JSONResponse:
    """A fresh 503 per request (§11.3)."""
    return JSONResponse({"error": "overview_unavailable"}, status_code=503)


HORIZON_MIN = 30
HORIZON_S = HORIZON_MIN * 60
SLOT_S = 300  # the congestion forecast is in 5-minute slots
LIVE = ("planned", "active", "off_route")  # off-route flights still fly and land (orchestrator ruling 3)
PRIORITY = {"bunch": 0, "busy_site": 1, "heavy_corridor": 2, "reroute": 3, "clearance": 4}
MAX_BULLETS = 4
MAX_CHARS = 140
CALLSIGNS_SHOWN = 3  # bunches and corridors
CLEARANCE_SHOWN = 2  # "AL-01, AL-02 and 2 more need ATC clearance by 11:17 PM"
REQUIREMENT_TEXT = {
    "contact_tower": "two-way radio contact",
    "contact_approach": "approach contact",
    "clearance": "ATC clearance",
}

Fact = dict[str, Any]
Highway = Callable[[str, str], str | None]


def clock(t_s: float) -> str:
    """``7:42 PM`` in America/New_York (the format every screen uses)."""
    t = datetime.fromtimestamp(t_s, FL_TZ)
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def short(name: str) -> str:
    """Site name as said on the screen: a trailing parenthetical is dropped (``SunTrax (FDOT AAM test site)``)."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip() or name


def _join(items: list[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _callsigns(callsigns: list[str], shown: int = CALLSIGNS_SHOWN) -> str:
    head = callsigns[:shown]
    more = len(callsigns) - len(head)
    if more > 0:
        return ", ".join(head) + f" and {more} more"
    return _join(head)


def _slot_span(
    now_s: float, steps_min: list[int], levels: list[str], want: tuple[str, ...]
) -> tuple[float, float, str] | None:
    """(from_s, to_s, worst level) of the slots inside the horizon whose level is in ``want``; None if none are.
    A slot counts if it starts before the horizon ends (ruling 6: any part inside)."""
    hit = [(m, lv) for m, lv in zip(steps_min, levels, strict=False) if m < HORIZON_MIN and lv in want]
    if not hit:
        return None
    worst = "heavy" if any(lv == "heavy" for _, lv in hit) else hit[0][1]
    return now_s + hit[0][0] * 60, now_s + hit[-1][0] * 60 + SLOT_S, worst


def _eta_of(route: dict[str, Any], node: str) -> float | None:
    for n in route.get("nodes", []):
        if n["node"] == node:
            return float(n["eta_p50"])
    return None


def in_horizon(r: dict[str, Any], now_s: float) -> bool:
    """Ruling 6: a flight counts if ANY part of its arrival window (p10–p90) is inside now..now+30 min."""
    return r.get("eta_p10") is not None and float(r["eta_p90"]) >= now_s and float(r["eta_p10"]) <= now_s + HORIZON_S


def _live_arrivals(routes: Iterable[dict[str, Any]], node: str, now_s: float) -> list[dict[str, Any]]:
    """Live flights to ``node`` whose arrival window is in the horizon, soonest first."""
    rows = [r for r in routes if r["status"] in LIVE and r["destination"] == node and in_horizon(r, now_s)]
    return sorted(rows, key=lambda r: float(r["eta_p50"]))


def busy_sites(
    now_s: float, routes: list[dict[str, Any]], congestion: dict[str, Any], names: dict[str, str]
) -> list[tuple[bool, Fact]]:
    out = []
    steps = list(congestion["steps_min"])
    for site in congestion["sites"]:
        span = _slot_span(now_s, steps, site["levels"], ("moderate", "heavy"))
        if span is None:
            continue
        arrivals = _live_arrivals(routes, site["node"], now_s)
        medical = any(r.get("medical") for r in arrivals)
        out.append(
            (
                medical,
                {"kind": "busy_site", "node": site["node"], "name": names.get(site["node"], site["name"]),
                 "level": span[2], "from_s": span[0], "to_s": span[1], "arrivals": len(arrivals),
                 "pads": site["pads"], "medical": medical},
            )
        )  # fmt: skip
    return out


def _spans(
    edges: list[tuple[str, str, str | None, float, float]],
) -> list[list[tuple[str, str, str | None, float, float]]]:
    """Contiguous spans: heavy edges of one highway (None = connector) that share a node. Input order is kept."""
    parent = list(range(len(edges)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (u, v, hw, _, _) in enumerate(edges):
        for j in range(i):
            uj, vj, hwj, _, _ = edges[j]
            if hw == hwj and {u, v} & {uj, vj}:
                parent[find(i)] = find(j)
    groups: dict[int, list[tuple[str, str, str | None, float, float]]] = {}
    for i, e in enumerate(edges):
        groups.setdefault(find(i), []).append(e)
    return list(groups.values())


def _span_ends(group: list[tuple[str, str, str | None, float, float]]) -> tuple[str, str]:
    """The span's first and last node: its two dead ends in the order they first appear (else the first and last)."""
    deg: Counter[str] = Counter()
    order: list[str] = []
    for u, v, *_ in group:
        for n in (u, v):
            deg[n] += 1
            if n not in order:
                order.append(n)
    ends = [n for n in order if deg[n] == 1]
    return (ends[0], ends[1]) if len(ends) == 2 else (order[0], order[-1])


def heavy_corridors(
    now_s: float,
    routes: list[dict[str, Any]],
    congestion: dict[str, Any],
    names: dict[str, str],
    highway: Highway,
) -> list[tuple[bool, Fact]]:
    """One fact per contiguous heavy span (a long highway is not one fact). Name: ``I-4 between A and B``, or
    ``A to B`` for a connector without a highway; A and B are the span's first and last node names."""
    steps = list(congestion["steps_min"])
    heavy: list[tuple[str, str, str | None, float, float]] = []
    for e in congestion["edges"]:
        span = _slot_span(now_s, steps, e["levels"], ("heavy",))
        if span is not None:
            u, v = e["edge"]
            heavy.append((u, v, highway(u, v), span[0], span[1]))
    out = []
    for group in _spans(heavy):
        hw = group[0][2]
        a, b = (short(names.get(n, n)) for n in _span_ends(group))
        name = f"{hw} between {a} and {b}" if hw else f"{a} to {b}"
        from_s = min(g[3] for g in group)
        to_s = max(g[4] for g in group)
        crossing = []
        for r in routes:
            if r["status"] not in LIVE:
                continue
            pairs = {frozenset(p) for p in zip(r.get("path", []), r.get("path", [])[1:], strict=False)}
            for u, v, *_ in group:
                if frozenset((u, v)) in pairs:
                    etas = [t for t in (_eta_of(r, u), _eta_of(r, v)) if t is not None]
                    if any(from_s <= t <= to_s for t in etas):
                        crossing.append(r)
                        break
        crossing.sort(key=lambda r: min(float(n["eta_p50"]) for n in r.get("nodes", [{"eta_p50": 0.0}])))
        medical = any(r.get("medical") for r in crossing)
        out.append(
            (
                medical,
                {"kind": "heavy_corridor", "name": name, "edge": f"{group[0][0]}|{group[0][1]}",
                 "edges": [f"{u}|{v}" for u, v, *_ in group], "from_s": from_s, "to_s": to_s,
                 "flights": [r["callsign"] for r in crossing], "medical": medical},
            )
        )  # fmt: skip
    return out


def bunches(now_s: float, routes: list[dict[str, Any]], congestion: dict[str, Any]) -> list[tuple[bool, Fact]]:
    """At one landing site, the arrivals whose p10–p90 windows overlap (transitively) form one bunch."""
    pads = {s["node"]: s["pads"] for s in congestion["sites"]}
    names = {s["node"]: short(s["name"]) for s in congestion["sites"]}
    per_site: dict[str, list[dict[str, Any]]] = {}
    for r in routes:
        if r["status"] not in LIVE or r.get("eta_p10") is None or not in_horizon(r, now_s):
            continue
        per_site.setdefault(r["destination"], []).append(r)
    out = []
    for node, rows in per_site.items():
        rows.sort(key=lambda r: float(r["eta_p10"]))
        group: list[dict[str, Any]] = []
        for r in [*rows, None]:  # the sentinel closes the last group
            if group and (r is None or float(r["eta_p10"]) > max(float(g["eta_p90"]) for g in group)):
                if len(group) >= 2:
                    out.append(_bunch_fact(node, names.get(node, node), group, pads.get(node)))
                group = []
            if r is not None:
                group.append(r)
    return out


def _bunch_fact(node: str, name: str, group: list[dict[str, Any]], pads: int | None) -> tuple[bool, Fact]:
    group = sorted(group, key=lambda r: float(r["eta_p50"]))
    p50s = [float(r["eta_p50"]) for r in group]
    medical = any(r.get("medical") for r in group)
    return (
        medical,
        {"kind": "bunch", "node": node, "name": name,
         "flight_ids": [r["flight_id"] for r in group], "callsigns": [r["callsign"] for r in group],
         "from_s": min(float(r["eta_p10"]) for r in group), "to_s": max(float(r["eta_p90"]) for r in group),
         "within_min": max(1, math.ceil((max(p50s) - min(p50s)) / 60)), "pads": pads, "medical": medical},
    )  # fmt: skip


def reroute_facts(reroutes: list[dict[str, Any]]) -> list[tuple[bool, Fact]]:
    out = []
    for rr in reroutes:
        medical = rr.get("trigger") == "medical"
        out.append(
            (
                medical,
                {"kind": "reroute", "reroute_id": rr["id"], "callsign": rr["callsign"], "gain_s": rr.get("gain_s"),
                 "status": rr["status"], "medical": medical},
            )
        )  # fmt: skip
    return out


def clearance_facts(now_s: float, routes: list[dict[str, Any]]) -> list[tuple[bool, Fact]]:
    """Pending ATC actions due within the horizon. ``by_s``: when the flight reaches the start of its first leg
    that needs ATC (its destination ETA if that leg is not on the route's node list)."""
    out = []
    for r in routes:
        atc = r.get("atc") or {}
        if atc.get("status") != "pending" or atc.get("requirement") in (None, "none"):
            continue
        legs = atc.get("legs") or []
        by = _eta_of(r, legs[0][0]) if legs else None
        by_s = by if by is not None else r.get("eta_p50")
        if by_s is None or float(by_s) > now_s + HORIZON_S:
            continue
        medical = bool(r.get("medical"))
        out.append(
            (
                medical,
                {"kind": "clearance", "flight_id": r["flight_id"], "callsign": r["callsign"],
                 "requirement": atc["requirement"], "by_s": float(by_s), "overdue": float(by_s) < now_s,
                 "medical": medical},
            )
        )  # fmt: skip
    return out


def group_clearances(items: list[tuple[bool, Fact]]) -> list[tuple[bool, Fact]]:
    """All pending clearances in the horizon become ONE fact (orchestrator ruling: they flooded the overview).
    ``callsigns`` are in due order, so the first one is the earliest."""
    if not items:
        return []
    rows = sorted(items, key=lambda t: t[1]["by_s"])
    return [
        (
            any(m for m, _ in rows),
            {"kind": "clearance", "flight_ids": [f["flight_id"] for _, f in rows],
             "callsigns": [f["callsign"] for _, f in rows], "count": len(rows),
             "earliest_due_s": rows[0][1]["by_s"], "overdue_count": sum(1 for _, f in rows if f["overdue"]),
             "medical": any(m for m, _ in rows)},
        )
    ]  # fmt: skip


def build_facts(
    now_s: float,
    routes: list[dict[str, Any]],
    congestion: dict[str, Any],
    reroutes: list[dict[str, Any]],
    *,
    node_names: dict[str, str] | None = None,
    highway: Highway | None = None,
) -> list[Fact]:
    """Facts in contract order: medical first, then bunch, busy_site, heavy_corridor, reroute, clearance.
    ``node_names``: node id -> name (default: the landing sites' names). ``highway(u, v)``: the corridor's highway
    name, None for a connector (default: none). ``[{"kind": "quiet"}]`` when nothing is notable."""
    names = dict(node_names or {})
    names.update({s["node"]: s["name"] for s in congestion["sites"]})
    tagged = [
        *bunches(now_s, routes, congestion),
        *busy_sites(now_s, routes, congestion, names),
        *heavy_corridors(now_s, routes, congestion, names, highway or (lambda u, v: None)),
        *reroute_facts(reroutes),
        *group_clearances(clearance_facts(now_s, routes)),
    ]
    tagged.sort(key=lambda t: (not t[0], PRIORITY[t[1]["kind"]], t[1].get("from_s", t[1].get("by_s", 0.0))))
    facts = [f for _, f in tagged]
    return facts or [{"kind": "quiet"}]


def sentence(fact: Fact) -> str:
    """One plain sentence per fact: the bullet the template shows, and a line the language model may use."""
    k = fact["kind"]
    if k == "bunch":
        text = f"{short(fact['name'])}: {_callsigns(fact['callsigns'])} arrive within {fact['within_min']} min"
        if fact["pads"] is not None and len(fact["callsigns"]) > fact["pads"]:
            text += f"; expect a pad wait ({len(fact['callsigns'])} arrivals, {fact['pads']} pads)"
        return text + "."
    if k == "busy_site":
        level = "heavy" if fact["level"] == "heavy" else "moderate"
        text = f"{short(fact['name'])}: {level} traffic forecast {_span(fact['from_s'], fact['to_s'])}"
        if fact["arrivals"]:
            text += f" ({fact['arrivals']} arrival{'s' if fact['arrivals'] != 1 else ''} due)"
        return text + "."
    if k == "heavy_corridor":
        text = f"{fact['name']} is heavy {clock(fact['from_s'])} to {clock(fact['to_s'])}"
        if fact["flights"]:
            text += f" with {_callsigns(fact['flights'])} on it"
        return text + "."
    if k == "reroute":
        gain = fact.get("gain_s")
        if gain is None:
            tail = ""
        elif gain < 60:  # under a minute: say so, never "saves 0 min"
            tail = ", arrives no later"
        else:
            tail = f", saves {round(gain / 60)} min"
        return f"A reroute for {fact['callsign']} waits for approval{tail}."
    if k == "clearance":
        due = clock(fact["earliest_due_s"])
        if fact["count"] == 1:
            cs = fact["callsigns"][0]
            if fact["overdue_count"]:
                return f"{cs}: ATC clearance still pending (due {due})."
            return f"{cs} needs ATC clearance by {due}."
        text = f"{fact['count']} flights need ATC clearance; earliest due {due} ({fact['callsigns'][0]})"
        if fact["overdue_count"]:
            text += f"; {fact['overdue_count']} overdue"
        return text + "."
    return f"Traffic is light everywhere for the next {HORIZON_MIN} min."


def _span(a: float, b: float) -> str:
    """``10:20 to 10:25 PM`` when both ends share a half of the day, else ``10:20 PM to 1:05 AM``."""
    ca, cb = clock(a), clock(b)
    if ca[-2:] == cb[-2:]:
        return f"{ca[:-3]} to {cb}"
    return f"{ca} to {cb}"


def _cut(text: str, limit: int) -> str:
    """At most ``limit`` characters: a longer text is cut at the last whole word that fits and ends with a period."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut if cut.endswith(".") else cut + "."


def fit(text: str) -> str:
    """Bullets are at most MAX_CHARS long; a longer sentence is cut at a word and ends with a period."""
    return _cut(text, MAX_CHARS)


def allowed_numbers(lines: Iterable[str]) -> set[str]:
    """Every number the language model may write: those in the fact sentences, and the horizon itself."""
    out = {str(HORIZON_MIN)}
    for line in lines:
        out |= set(NUMBER.findall(line))
    return out


def facts_for_llm(facts: list[Fact]) -> Facts:
    lines = [sentence(f) for f in facts]
    return Facts(lines, allowed_numbers(lines))


def template_bullets(facts: list[Fact]) -> list[str]:
    """Up to MAX_BULLETS bullets, one per fact in order (the quiet case is a single bullet)."""
    return [fit(sentence(f)) for f in facts[:MAX_BULLETS]]


# --- checkpoint 2: cache, background refresh, endpoints (contract §1, §2, §4, §5, §9) -------------------------------
router = APIRouter()
OVERVIEW_TTL_S = 45.0  # LLM text is regenerated only when the facts changed AND this old (ruling 2)
FLIGHT_TTL_S = 60.0
LLM_TIMEOUT_S = 20.0  # §9.1: wait_for this long, then the template; the slot stays taken until the thread returns
REFRESH_S = 5.0  # background refresh of the snapshot and facts (lifespan task in app.py)
OVERVIEW_MAX_TOKENS = 220
FLIGHT_MAX_TOKENS = 120
FLIGHT_MAX_CHARS = 260
FLIGHT_MAX_SENTENCES = 2
OVERVIEW_PROMPT = (
    "You write the AI overview an air traffic controller sees above the flights in Central Florida. "
    "Write up to 4 short lines, each starting with '- ', plain text, no markdown, each under 140 characters. "
    "Use ONLY the facts given. Never invent numbers, times, places or advice beyond the facts. "
    "Keep the times exactly as the facts give them. "
    "Write plain English with no symbols, arrows, bullets or dashes."
)
FLIGHT_PROMPT = (
    "You write a short overview an air traffic controller reads about one flight. "
    "Write at most 2 short sentences, under 260 characters, plain text, no markdown. "
    "Use ONLY the facts given; never invent numbers, times, places or advice beyond the facts. "
    "Write plain English with no symbols, arrows, bullets or dashes."
)
MARKDOWN = re.compile(r"[*#`]|\]\(")


def call_llm(system_prompt: str, lines: list[str], max_tokens: int) -> str:
    """The language-model seam (contract §5, ruling 8): module-level so tests can monkeypatch it. Same client
    and settings as the route summary (summary/llm.py): raises when no model is configured."""
    return llm_mod.call_llm(Facts(lines, set()), LLM_TIMEOUT_S, system=system_prompt, max_tokens=max_tokens)


class LlmBusy(Exception):
    """The one model slot is taken (a previous call has not returned yet): this attempt is skipped, never queued."""


@dataclass
class Overview:
    facts: list[Fact]
    digest: str  # identity of the current facts as the user reads them (their sentences, minute resolution)
    template: list[str]  # template bullets for the current facts (refreshed every tick, §9.4)
    generated_at: str
    llm: list[str] | None = None  # LLM bullets, valid only while llm_digest == digest (§9.4)
    llm_digest: str | None = None
    llm_at: float | None = None  # when the LLM last ran; None: a cold template, LLM still due
    check: dict[str, Any] = field(default_factory=lambda: {"rejected": False})


@dataclass
class FlightText:
    callsign: str
    digest: str  # identity of the flight's current facts (their sentences)
    template: str  # template text for the current facts
    generated_at: str
    llm: str | None = None  # LLM text, valid only while llm_digest == digest (§9.4)
    llm_digest: str | None = None
    llm_at: float | None = None


@dataclass
class Cache:
    snapshot: dict[str, Any] | None = None
    overview: Overview | None = None
    flights: dict[str, FlightText] = field(default_factory=dict)
    inflight: set[str] = field(default_factory=set)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # two refreshes never overlap
    llm_busy: bool = False  # the one model slot (§9.1): taken until the worker thread really returns


def cache_of(app: Any) -> Cache:
    cache = getattr(app.state, "ops_overview", None)
    if cache is None:
        cache = Cache()
        app.state.ops_overview = cache
    return cache


class _AppRequest:
    """What the ops handlers read from a request (``request.app``): lets the background task call them."""

    def __init__(self, app: Any) -> None:
        self.app = app


def _iso(t_s: float) -> str:
    return datetime.fromtimestamp(t_s, UTC).isoformat()


def _digest(lines: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def _highway_of(network: Any) -> Callable[[str, str], str | None]:
    """A corridor's highway name (``I-4``); None for a connector or an unknown pair."""

    def highway(u: str, v: str) -> str | None:
        for a, b in ((u, v), (v, u)):
            try:
                edge = network.edge_between(a, b)
            except KeyError:
                continue
            if edge is not None:
                return edge.highway
        return None

    return highway


async def take_snapshot(app: Any) -> dict[str, Any]:
    """Live data for the overview, read through the existing ops endpoints (the same numbers ATC sees).
    The horizon's "now" is the service clock: it stamps departures and ETAs, so both share one clock."""
    s = app.state.service
    req = cast(Request, _AppRequest(app))
    routes = await ops_api.ops_routes(req)
    congestion = await ops_api.ops_congestion(req)
    conflicts = (await ops_api.ops_conflicts(req))["conflicts"]
    reroutes = await open_list(s)
    return {
        "now": s.clock().timestamp(),
        "routes": routes,
        "congestion": congestion,
        "conflicts": conflicts,
        "reroutes": reroutes,
        "names": dict(s.planner.names),
        "network": s.planner.network,
    }


def facts_of(snap: dict[str, Any]) -> list[Fact]:
    return build_facts(
        snap["now"],
        snap["routes"],
        snap["congestion"],
        snap["reroutes"],
        node_names=snap["names"],
        highway=_highway_of(snap["network"]),
    )


def _facts_digest(facts: list[Fact]) -> str:
    return _digest(sentence(f) for f in facts)


async def run_llm(cache: Cache, fn: Callable[..., str], *args: Any) -> str:
    """One model call at a time, never queued (§9.1). Raises LlmBusy when a call is already running, and
    TimeoutError after LLM_TIMEOUT_S. On timeout the worker thread keeps the slot until it really returns."""
    if cache.llm_busy:
        raise LlmBusy()
    cache.llm_busy = True
    fut: asyncio.Future[str] = asyncio.ensure_future(asyncio.to_thread(fn, *args))

    def released(f: asyncio.Future[str]) -> None:
        cache.llm_busy = False
        if not f.cancelled():
            f.exception()  # consume it: a late failure after a timeout is not an unretrieved exception

    fut.add_done_callback(released)
    return await asyncio.wait_for(asyncio.shield(fut), LLM_TIMEOUT_S)


def parse_overview(text: str, facts: Facts) -> tuple[list[str], str | None]:
    """LLM text -> bullets, or the reason it is refused (ruling 4: limits and the number check)."""
    bullets = [ln.strip()[2:].strip() for ln in text.splitlines() if ln.strip().startswith("- ")]
    if not bullets:
        return [], "no '- ' bullet lines"
    if len(bullets) > MAX_BULLETS:
        return [], f"{len(bullets)} bullets (limit {MAX_BULLETS})"
    if any(len(b) > MAX_CHARS for b in bullets):
        return [], f"a bullet is over {MAX_CHARS} characters"
    if MARKDOWN.search(text):
        return [], "markdown in the text"
    bad = check_numbers(text, facts)
    if bad:
        return [], "numbers not in the facts: " + ", ".join(bad)
    return bullets, None


async def _ask_overview(cache: Cache, facts: list[Fact]) -> tuple[list[str] | None, dict[str, Any]]:
    """One model attempt. LlmBusy propagates (skip); every other failure becomes the template with a reason."""
    fl = facts_for_llm(facts)
    try:
        text = await run_llm(cache, call_llm, OVERVIEW_PROMPT, fl.lines, OVERVIEW_MAX_TOKENS)
    except LlmBusy:
        raise
    except TimeoutError:
        return None, {"rejected": False, "reason": f"error: no answer within {LLM_TIMEOUT_S:g} s"}
    except Exception as e:  # noqa: BLE001 - no model, network, quota: the controller still gets the template
        return None, {"rejected": False, "reason": f"error: {e}"}
    bullets, why = parse_overview(text, fl)
    if why is not None:
        return None, {"rejected": True, "reason": why}
    return bullets, {"rejected": False}


async def refresh_overview(app: Any) -> None:
    """One background tick (§9.4): snapshot, facts and template bullets are refreshed every time. LLM text is
    regenerated when it is due (none yet, or the facts changed and it is OVERVIEW_TTL_S old), and shown only while
    it was written from the current facts. A busy model slot skips the attempt; the next tick tries again."""
    cache = cache_of(app)
    async with cache.refresh_lock:
        snap = await take_snapshot(app)
        cache.snapshot = snap
        now = snap["now"]
        facts = facts_of(snap)
        digest = _facts_digest(facts)
        live = {r["flight_id"] for r in snap["routes"]}
        for fid in [f for f in cache.flights if f not in live]:  # §9.2: flights no longer live
            del cache.flights[fid]
        ov = cache.overview
        if ov is None:  # cold start: the template, synchronously; the LLM is due from the next tick
            cache.overview = Overview(facts, digest, template_bullets(facts), _iso(now))
            return
        ov.facts, ov.digest = facts, digest
        ov.template, ov.generated_at = template_bullets(facts), _iso(now)
        due = ov.llm_at is None or (ov.llm_digest != digest and now - ov.llm_at >= OVERVIEW_TTL_S)
        if not due:
            return
        try:
            bullets, check = await _ask_overview(cache, facts)
        except LlmBusy:
            return
        ov.check, ov.llm_at = check, now
        ov.llm, ov.llm_digest = (bullets, digest) if bullets is not None else (None, None)


def shown_overview(ov: Overview) -> tuple[str, list[str]]:
    """The text to show: the LLM bullets only while they were written from the current facts (§9.4)."""
    if ov.llm is not None and ov.llm_digest == ov.digest:
        return "llm", ov.llm
    return "template", ov.template


def overview_json(ov: Overview) -> dict[str, Any]:
    source, bullets = shown_overview(ov)
    return {
        "generated_at": ov.generated_at,
        "horizon_min": HORIZON_MIN,
        "source": source,
        "bullets": bullets,
        "facts": ov.facts,
        "bunches": [f for f in ov.facts if f["kind"] == "bunch"],
        "check": ov.check,
    }


@router.get("/api/ops/overview", dependencies=[Depends(require_operator)], response_model=None)
async def get_overview(request: Request) -> dict[str, Any] | JSONResponse:
    """Instant: the cached overview. The very first call computes the template synchronously (never an empty
    placeholder); the LLM text follows from the background refresh. If that first snapshot fails there is nothing
    to serve: 503 ``overview_unavailable`` (logged), never an unhandled 500 (§11.3)."""
    cache = cache_of(request.app)
    if cache.overview is None:
        try:
            await refresh_overview(request.app)
        except Exception:  # noqa: BLE001 - logged below; the client gets a clean 503
            log.exception("overview snapshot failed on a cold start")
        if cache.overview is None:
            return unavailable()
    return overview_json(cache.overview)


# --- per flight ---------------------------------------------------------------------------------------------
def flight_sentences(snap: dict[str, Any], row: dict[str, Any]) -> list[str]:
    """Facts about one flight as sentences, in §9.5 priority: arrival window, open reroute, pending ATC requirement,
    heavy or moderate legs on the route; then the destination and predicted conflicts. The template takes the first
    two, so the priority decides what the controller reads."""
    now, names, cong = snap["now"], snap["names"], snap["congestion"]
    out: list[str] = []
    if row.get("eta_p10") is not None and row.get("eta_p50") is not None:
        dest = short(names.get(row["destination"], row["destination"]))
        out.append(
            f"Arrives at {dest} {clock(float(row['eta_p10']))} to {clock(float(row['eta_p90']))}, "
            f"expected {clock(float(row['eta_p50']))}."
        )
    for rr in snap["reroutes"]:
        if rr["flight_id"] == row["flight_id"]:
            out.append(sentence(reroute_facts([rr])[0][1]))
    for _, c in clearance_facts(now, [row]):
        need = REQUIREMENT_TEXT.get(c["requirement"], c["requirement"])
        due = clock(c["by_s"])
        if c["overdue"]:
            out.append(f"{c['callsign']}: {need} still pending (due {due}).")
        else:
            out.append(f"{c['callsign']} needs {need} by {due}.")
    steps = list(cong["steps_min"])
    levels = {tuple(e["edge"]): e["levels"] for e in cong["edges"]}
    highway = _highway_of(snap["network"])
    path = row.get("path", [])
    for u, v in zip(path, path[1:], strict=False):
        lv = levels.get((u, v)) or levels.get((v, u))
        span = _slot_span(now, steps, lv, ("moderate", "heavy")) if lv else None
        if span is None:
            continue
        hw = highway(u, v)
        a, b = short(names.get(u, u)), short(names.get(v, v))
        name = f"{hw} between {a} and {b}" if hw else f"{a} to {b}"
        out.append(f"{name} is {span[2]} {clock(span[0])} to {clock(span[1])} on this route.")
    out += [sentence(f) for _, f in busy_sites(now, [row], cong, names) if f["node"] == row["destination"]]
    callsigns = {r["flight_id"]: r["callsign"] for r in snap["routes"]}
    for c in snap["conflicts"]:
        refs = [f["flight_id"] for f in c.get("flights", [])]
        if row["flight_id"] not in refs:
            continue
        others = [callsigns[x] for x in refs if x != row["flight_id"] and x in callsigns]
        who = f" with {others[0]}" if others else ""
        minutes = max(1, math.ceil(float(c["time_to_conflict_s"]) / 60))
        out.append(f"Predicted {c['severity']} {c['kind']} conflict{who} in {minutes} min.")
        if len(out) > 6:
            break
    return out or [f"Nothing notable on this route in the next {HORIZON_MIN} min."]


def flight_template(lines: list[str]) -> str:
    return _cut(" ".join(lines[:FLIGHT_MAX_SENTENCES]), FLIGHT_MAX_CHARS)


def parse_flight(text: str, facts: Facts) -> str | None:
    """Why the flight text is refused (ruling 1: limits and the number check), or None when it passes."""
    t = text.strip()
    if not t:
        return "empty"
    if len(t) > FLIGHT_MAX_CHARS:
        return f"over {FLIGHT_MAX_CHARS} characters"
    if len(re.split(r"(?<=[.!?])\s+", t)) > FLIGHT_MAX_SENTENCES or "\n" in t:
        return "more than 2 sentences or more than one paragraph"
    if MARKDOWN.search(t):
        return "markdown in the text"
    bad = check_numbers(t, facts)
    if bad:
        return "numbers not in the facts: " + ", ".join(bad)
    return None


def _flight_json(flight_id: str, entry: FlightText) -> dict[str, Any]:
    current = entry.llm is not None and entry.llm_digest == entry.digest
    return {
        "flight_id": flight_id,
        "callsign": entry.callsign,
        "source": "llm" if current else "template",
        "text": entry.llm if current else entry.template,
        "generated_at": entry.generated_at,
    }


async def _llm_flight(app: Any, flight_id: str, lines: list[str], digest: str, now: float) -> None:
    """One per-flight model attempt, through the same single slot as the overview. Skipped while it is busy."""
    cache = cache_of(app)
    try:
        raw = await run_llm(cache, call_llm, FLIGHT_PROMPT, lines, FLIGHT_MAX_TOKENS)
    except LlmBusy:
        return
    except Exception:  # noqa: BLE001 - timeout or model error: the template stands
        text = None
    else:
        text = raw.strip() if parse_flight(raw, facts_for_llm_lines(lines)) is None else None
    finally:
        cache.inflight.discard(flight_id)
    entry = cache.flights.get(flight_id)
    if entry is None:  # the flight ended while the model ran
        return
    entry.llm, entry.llm_digest, entry.llm_at = text, digest, now


def facts_for_llm_lines(lines: list[str]) -> Facts:
    return Facts(lines, allowed_numbers(lines))


@router.get("/api/ops/flights/{flight_id}/overview", dependencies=[Depends(require_operator)], response_model=None)
async def get_flight_overview(request: Request, flight_id: str) -> dict[str, Any] | JSONResponse:
    """Instant: the cached text for one flight. The first call gives the template and asks the model in the
    background; a changed flight is regenerated once its LLM text is FLIGHT_TTL_S old."""
    app = request.app
    cache = cache_of(app)
    if cache.snapshot is None:
        try:
            async with cache.refresh_lock:
                if cache.snapshot is None:
                    cache.snapshot = await take_snapshot(app)
        except Exception:  # noqa: BLE001 - logged; a cold cache has nothing to serve
            log.exception("flight overview snapshot failed on a cold start")
        if cache.snapshot is None:
            return unavailable()
    snap = cache.snapshot
    row = next((r for r in snap["routes"] if r["flight_id"] == flight_id), None)
    if row is None:
        raise HTTPException(404, "no such planned or active flight")
    lines = flight_sentences(snap, row)
    digest = _digest(lines)
    now = snap["now"]
    entry = cache.flights.get(flight_id)
    if entry is None:
        entry = FlightText(row["callsign"], digest, flight_template(lines), _iso(now))
        cache.flights[flight_id] = entry
    elif entry.digest != digest:  # the facts moved: the template shows until the next LLM text (§9.4)
        entry.digest, entry.template, entry.generated_at = digest, flight_template(lines), _iso(now)
    due = entry.llm_at is None or (entry.llm_digest != digest and now - entry.llm_at >= FLIGHT_TTL_S)
    if due and flight_id not in cache.inflight:
        cache.inflight.add(flight_id)
        task = asyncio.get_running_loop().create_task(_llm_flight(app, flight_id, lines, digest, now))
        cache.tasks.add(task)
        task.add_done_callback(cache.tasks.discard)
    return _flight_json(flight_id, entry)
