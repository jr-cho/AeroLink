"""The AI identity, its measured results and the disclaimer (U-API §3, UI-AI-PLAN §D).

``GET /api/ai`` (public, cheap) says which models run, what they do, and the results. Every number is read from the
committed eval files in ``backend/eval-results`` (never typed in here), so the screens' "How the AI works" sheet
can only show what the evaluations measured.

Disclaimer acknowledgements are event-log entries of kind ``ai_ack`` (one per acceptance, with the text version):
a pilot's for their unit (``POST /api/devices/{id}/ai-ack``), an operator's (``POST /api/auth/ai-ack``). The
latest one tells the screen whether to show the one-time notice again (``ai_ack_version``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Request

from aerolink.service.db import events

router = APIRouter()
audit = logging.getLogger("aerolink.audit")

DISCLAIMER_VERSION = "2026-11-27"  # bumped with the L-FIX texts: everyone acknowledges once more
# Pilot text: Associate C's draft (.work/lawyer/R2-C.md, finding C-02). Two phrases are adjusted to the binding decision
# that dashboard users are AeroLink Ops staff, not FAA ATC: "ATC operators" -> "AeroLink Ops staff", and the reroute
# card's name "ATC reroute" -> "Ops reroute" (the card's new name). Flagged for counsel to confirm.
DISCLAIMER_PILOT = (
    "AeroLink is a prototype and is advisory only. It is not certified or approved by the FAA, and it is not an air "
    "traffic control service or a collision-avoidance system. Its AI predicts traffic and suggests routes, and it can "
    "be wrong or out of date. Traffic from other aircraft comes from a public feed that can be incomplete or delayed, "
    "so an empty screen does not mean the sky is empty. You, as pilot in command, remain responsible for the safe "
    "operation of your aircraft: keep looking outside and follow the clearances and instructions ATC gives you "
    'directly. A message in AeroLink, including an "Ops reroute", is not a clearance. AeroLink records your unit\'s '
    "position, routes and answers and shows them to AeroLink Ops staff."
)
# Ops text: Associate C's draft for the one-time notice on the Ops dashboard (R2-C.md, C-02), verbatim.
DISCLAIMER_OPS = (
    "AeroLink is a prototype and is advisory only. It is not an FAA system, and nothing here replaces your approved "
    "displays, procedures or authority. Its AI predicts traffic and phrases overviews and may be wrong or out of date. "
    "Live traffic is from a public feed that can be incomplete or delayed. Messages you send through AeroLink are not "
    "ATC clearances or instructions; use your normal channels. AeroLink records your actions under your username."
)
DISCLAIMER_TEXT = DISCLAIMER_PILOT  # the field older clients read: the pilot text (kept for compatibility)


# One line each: what it is and what it feeds on screen. Names are the labels the screens show.
MODELS = [
    {
        "key": "forecaster",
        "title": "Traffic & arrival-time forecaster",
        "name": "aerolink-gbm",
        "what": "Predicts where each aircraft will be and when it arrives, with a calibrated window.",
        "drives": "Traffic colours on the corridors, ETA ranges, route options and reroute proposals.",
    },
    {
        "key": "demand",
        "title": "Demand forecaster",
        "name": "aerolink-demand",
        "what": "Estimates flights not yet planned for the next 30 minutes, from time of day and the network.",
        "drives": "Expected load in the traffic forecast (AeroLink routing only).",
    },
    {
        "key": "offroute",
        "title": "Off-route detector",
        "name": "aerolink-offroute",
        "what": "Learns how far a flight may drift from its planned route before it counts as off route.",
        "drives": "The 'recalculating' banner and the off-route status on the Ops screen.",
    },
    {
        "key": "summary",
        "title": "Route summary (language model)",
        "name": "route-summary",
        "what": "Writes the one-line route summary; its numbers are checked before they are shown.",
        "drives": "The summary line on route options (the template is used when the check fails).",
    },
]


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _rounded(x: Any, digits: int) -> float | None:
    return None if x is None else round(float(x), digits)


def results(eval_dir: Path) -> dict[str, Any]:
    """The headline measurements, from the committed files. A missing file gives nulls, never an error."""
    m5 = _read(eval_dir / "m5-ablations.json") or {}
    off = _read(eval_dir / "offroute-e5.5.json") or {}
    rows = {r["config"]: r for r in m5.get("summary", []) if isinstance(r, dict) and "config" in r}
    ai, classical = rows.get("A5"), rows.get("B1")  # A5: the full AeroLink engine; B1: classical navigation
    ai_off = (off.get("results") or {}).get("ai") or {}
    same_routes = bool(
        ai and classical
        and ai.get("trip_time_mean_s") == classical.get("trip_time_mean_s")
        and ai.get("door_to_door_mean_s") == classical.get("door_to_door_mean_s")
        and ai.get("suggestions_offered") == 0
    )  # fmt: skip
    if same_routes and classical is not None:
        note = (
            f"On the {classical['trips']} committed trips ({classical['runs']} scenario runs) the AI engine and "
            "classical navigation chose the same routes: identical trip times and no suggestions offered."
        )
    else:
        note = "Route choices differ from classical navigation on the committed scenarios; see the ablation table."
    return {
        "eta_error_ai_s": _rounded(ai and ai.get("eta_error_mean_s"), 0),
        "eta_error_classical_s": _rounded(classical and classical.get("eta_error_mean_s"), 0),
        "window_coverage": _rounded(ai and ai.get("eta_coverage"), 2),
        "offroute_precision": _rounded(ai_off.get("precision"), 2),
        "offroute_recall": _rounded(ai_off.get("recall"), 3),
        "route_choice_note": note,
    }


def disclaimer() -> dict[str, str]:
    return {"version": DISCLAIMER_VERSION, "text": DISCLAIMER_TEXT, "pilot": DISCLAIMER_PILOT, "ops": DISCLAIMER_OPS}


async def ack_version(store: Any, aircraft_id: str | None = None, operator: str | None = None) -> str | None:
    """The disclaimer version last acknowledged by a unit's pilot (``aircraft_id``) or by an operator (``operator``)."""
    q = sa.select(events.c.detail).where(events.c.kind == "ai_ack")
    if aircraft_id is not None:
        q = q.where(events.c.aircraft_id == aircraft_id)
    elif operator is not None:
        q = q.where(events.c.aircraft_id.is_(None), events.c.acked_by == operator)
    else:
        return None
    q = q.order_by(events.c.id.desc()).limit(1)
    async with store.engine.connect() as c:
        row = (await c.execute(q)).first()
    return str(row[0].get("version")) if row and isinstance(row[0], dict) and row[0].get("version") else None


async def record_ack(svc: Any, version: str, *, by: str, aircraft_id: str | None = None) -> None:
    audit.info("AI advisory %s acknowledged by %s%s", version, by, f" for {aircraft_id}" if aircraft_id else "")
    await svc.events.record(
        "ai_ack",
        aircraft_id=aircraft_id,
        message=f"{by} acknowledged the AI advisory ({version})",
        detail={"version": version, "by": by},
        acked_by=by[:64],
    )


@router.get("/api/ai")
async def ai_info(request: Request) -> dict[str, Any]:
    s = request.app.state.service
    p = s.planner
    info = next((m for m in p.loaded_models if m.get("name") == p.forecaster.name), {})
    age = round((s.clock() - s.forecast_at).total_seconds(), 1) if s.forecast_at else None
    return {
        "forecaster": {
            "name": p.forecaster.name,
            "version": info.get("version"),
            "updated_at": info.get("loaded_at"),
            "forecast_age_s": age,
        },
        "models": MODELS,
        "results": results(p.settings.eval_dir),
        "disclaimer": disclaimer(),
    }
