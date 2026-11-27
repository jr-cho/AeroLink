"""L-FIX (LF-B) backend wording and defaults: no OpenRouter default, Ops records, accepted/declined in the text."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from aerolink.network import Network
from aerolink.service import reroute
from aerolink.service.ai_api import DISCLAIMER_OPS, DISCLAIMER_PILOT, DISCLAIMER_VERSION, disclaimer
from aerolink.service.planner import Planner
from aerolink.service.settings import Settings
from aerolink.summary import llm

REPO = Path(__file__).resolve().parents[2]


def test_the_llm_endpoint_has_no_default_and_unset_means_no_model(monkeypatch: pytest.MonkeyPatch, fl_network: Network):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "k")
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):  # no openrouter.ai fallback, no network call
        llm.call_llm(llm.Facts(lines=["x"], allowed=set()))
    assert Planner(fl_network, Settings(method="classical", forecaster="cv")).llm is None  # template summary


def test_env_example_names_no_default_endpoint():
    text = (REPO / ".env.example").read_text()  # the example file only: never the real .env
    assert "openrouter.ai" not in text.lower()
    assert "\nLLM_BASE_URL=\n" in text


def test_disclaimer_has_pilot_and_ops_texts_at_the_new_version():
    d = disclaimer()
    assert d["version"] == DISCLAIMER_VERSION == "2026-11-27"
    assert d["pilot"] == DISCLAIMER_PILOT and d["ops"] == DISCLAIMER_OPS
    assert d["text"] == DISCLAIMER_PILOT  # the field older clients read stays the pilot text
    assert "not a clearance" in DISCLAIMER_PILOT and "pilot in command" in DISCLAIMER_PILOT
    assert "not ATC clearances or instructions" in DISCLAIMER_OPS
    assert "ATC operators" not in DISCLAIMER_PILOT  # Ops staff, not FAA ATC (decision 1)


def test_reroute_records_read_accepted_and_declined_but_keep_their_status_values():
    seen: list[dict] = []

    async def record(kind, **kw):
        seen.append({"kind": kind, **kw})

    svc = cast(Any, SimpleNamespace(events=SimpleNamespace(record=record)))  # only .events.record is used
    row: dict[str, Any] = {"id": "rr_1", "aircraft_id": "dev-001", "callsign": "N999AL", "flight_id": "f"}
    asyncio.run(reroute._log(svc, row, "acknowledged", "pilot dev-001"))
    asyncio.run(reroute._log(svc, row, "unable", "pilot dev-001"))
    assert "accepted" in seen[0]["message"] and "acknowledged" not in seen[0]["message"]
    assert "declined" in seen[1]["message"] and "unable" not in seen[1]["message"]
    assert seen[0]["detail"]["status"] == "acknowledged" and seen[1]["detail"]["status"] == "unable"  # API values
