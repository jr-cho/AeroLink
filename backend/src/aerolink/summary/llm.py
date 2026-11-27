"""Two-sentence route summary from an LLM, with a number check (PLAN E5.6, ARCHITECTURE §7.5).

The LLM (any OpenAI-compatible API, configured by LLM_BASE_URL; no endpoint is assumed) only ever sees a small table of
facts taken from the route response: minutes, waits, traffic levels, arrival times and
place names. Every number in its answer must be one of the numbers in that table (or a
difference between two of them); otherwise the text is thrown away and a template
sentence is used. The LLM never influences routing.

Settings (environment, see ``.env.example``): ``LLM_BASE_URL``, ``LLM_MODEL``, ``LLM_API_KEY``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

SYSTEM_PROMPT = (
    "You write the one-line summary a navigation app shows above route options for a small "
    "aircraft. Write at most two short sentences. Use ONLY the facts given; never invent "
    "numbers, times or places. Prefer minutes. Option 1 is the recommended route; call a route "
    '"fastest" only if its facts say fastest. Mention why option 1 is recommended. '
    "Write plain English with no symbols, arrows, bullets or dashes."
)
NUMBER = re.compile(r"\d+(?::\d\d)?(?:\.\d+)?")


@dataclass(frozen=True)
class Facts:
    lines: list[str]  # what the LLM sees
    allowed: set[str]  # every number it may write


def _hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M")


def facts_from_response(response: dict, names: dict[str, str] | None = None) -> Facts:
    """Facts table and allowed numbers from a ``route-response`` (contracts v4.0)."""
    names = names or {}
    lines: list[str] = []
    minutes: list[int] = []
    allowed: set[str] = set()
    routes = response["routes"]
    quickest = _quickest(routes)
    for r in routes:
        mins = round(r["est_time_s"] / 60)
        wait = round(r["expected_wait_s"] / 60)
        risk = round(100 * r.get("bunching_risk", 0.0))
        dest = r["eta"][-1] if r.get("eta") else None
        place = names.get(dest["node"], dest["node"]) if dest else "destination"
        arrive = _hhmm(dest["eta_p50"]) if dest else None
        window = f"{_hhmm(dest['eta_p10'])}-{_hhmm(dest['eta_p90'])}" if dest else None
        lines.append(
            f"option {r['rank']} ({_badges(r, quickest)}): {mins} min flying, {wait} min expected wait, "
            f"traffic {r['traffic']}, bunching risk {risk}%, arrive {place} at {arrive} (likely {window}); "
            f"note: {r.get('reason') or 'none'}"
        )
        minutes.append(mins + wait)
        allowed |= {str(r["rank"]), str(mins), str(wait), str(mins + wait), str(risk)}
        allowed |= set(NUMBER.findall(place))  # e.g. "hp-61fl", "I-4"
        if dest:
            for k in ("eta_p10", "eta_p50", "eta_p90"):
                hhmm = _hhmm(dest[k])
                allowed |= {hhmm, hhmm.lstrip("0") or "0"}
        allowed |= set(NUMBER.findall(r.get("reason") or ""))
    for a in minutes:
        for b in minutes:
            if a > b:
                allowed.add(str(a - b))
    # numbers inside place names (e.g. "I-4", "SR-60") are allowed too
    for n in names.values():
        allowed |= set(NUMBER.findall(n))
    allowed.add(str(len(response["routes"])))
    return Facts(lines, allowed)


def check_numbers(text: str, facts: Facts) -> list[str]:
    """Numbers in ``text`` that are not in the facts (empty list = passes)."""
    return [n for n in NUMBER.findall(text) if n not in facts.allowed and n.lstrip("0") not in facts.allowed]


def _quickest(routes: list[dict]) -> dict | None:
    """Route with the shortest predicted time (cruise + expected wait); ties go to the better rank."""
    if not routes:
        return None
    return min(routes, key=lambda r: (r["est_time_s"] + r["expected_wait_s"], r["rank"]))


def _badges(r: dict, quickest: dict | None) -> str:
    """Same badges as the screens (web/src/shared/labels.ts): rank 1 is "recommended" (lowest cost);
    the shortest predicted time is "fastest"."""
    tags = (["recommended"] if r["rank"] == 1 else []) + (["fastest"] if quickest is r else [])
    return ", ".join(tags) or "alternative"


def template_summary(response: dict, names: dict[str, str] | None = None) -> str:
    """Deterministic fallback, always correct."""
    names = names or {}
    routes = response["routes"]
    if not routes:
        return "No route is available right now."
    best = routes[0]
    dest = best["eta"][-1] if best.get("eta") else None
    place = names.get(dest["node"], dest["node"]) if dest else "your destination"
    total = round(best["est_time_s"] / 60) + round(best["expected_wait_s"] / 60)  # same rounding as the facts
    text = f"Recommended route: about {total} min to {place}, {best['traffic']} traffic."
    if len(routes) > 1:
        other = round(routes[1]["est_time_s"] / 60) + round(routes[1]["expected_wait_s"] / 60)
        diff = other - total
        if diff > 0:
            text += f" The next option takes {diff} min longer."
        elif diff < 0:
            # Ranked below despite being quicker: its cost (time + wait + risk) is higher, so the risk is.
            text += f" The next option is {-diff} min quicker but has a higher bunching risk."
        else:
            text += " The next option takes about as long."
    return text


def call_llm(facts: Facts, timeout_s: float = 20.0, *, system: str = SYSTEM_PROMPT, max_tokens: int = 120) -> str:
    # No default endpoint (L-FIX): unset LLM_BASE_URL means no LLM, so the template summary is used.
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    model = os.environ.get("LLM_MODEL", "")
    key = os.environ.get("LLM_API_KEY", "")
    if not base or not model or not key:
        raise RuntimeError("set LLM_BASE_URL, LLM_MODEL and LLM_API_KEY (see .env.example); unset = template summary")
    body: dict = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Facts:\n" + "\n".join(facts.lines)},
        ],
    }
    # Reasoning models (e.g. Qwen3 on Ollama) otherwise spend the whole token budget thinking and return
    # empty content. "none" turns thinking off on Ollama's OpenAI-compatible API; set the variable to an empty
    # string for providers that reject the parameter.
    effort = os.environ.get("LLM_REASONING_EFFORT", "none")
    if effort:
        body["reasoning_effort"] = effort
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        # An explicit User-Agent: Cloudflare (in front of the self-hosted Ollama endpoint) answers 403 to
        # urllib's default "Python-urllib/x.y" regardless of the key.
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "AeroLink/0.1 (route-summary)",
            "X-Title": "AeroLink",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        content = json.load(r)["choices"][0]["message"].get("content") or ""
    # Never show a model's reasoning to a pilot, even if a server inlines it.
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


@dataclass(frozen=True)
class Summary:
    text: str
    source: str  # "llm" or "template"
    rejected: str | None = None  # LLM text that failed the check
    bad_numbers: tuple[str, ...] = ()


def summarize(response: dict, names: dict[str, str] | None = None, llm=call_llm) -> Summary:
    """LLM summary if it passes the number check, else the template. Never raises."""
    facts = facts_from_response(response, names)
    try:
        text = llm(facts)
    except Exception as e:  # noqa: BLE001 - network, quota, bad key: the user still gets a summary
        return Summary(template_summary(response, names), "template", rejected=f"error: {e}")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    bad = check_numbers(text, facts)
    if bad or not text or len(sentences) > 2 or len(text) > 300:
        return Summary(template_summary(response, names), "template", rejected=text, bad_numbers=tuple(bad))
    return Summary(text, "llm")


def iso(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
