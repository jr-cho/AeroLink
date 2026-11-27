"""U5: user-facing backend text is plain English, with no Unicode symbols, arrows, bullets or dashes."""

from __future__ import annotations

import ast
import re
from datetime import datetime
from pathlib import Path

import pytest
from test_e56_summary import RESPONSE
from test_r1_facts import at, busy_from, congestion, facts, route, site

from aerolink.service import ops_overview as ov
from aerolink.service.planner import FL_TZ
from aerolink.service.reroute import clock_range
from aerolink.summary import llm as llm_mod

SYMBOLS = re.compile("[°±·×‐-―•…←-⇿─-➿⬀-⯿]|\\.\\.\\.")
SRC = Path(__file__).parent.parent / "src" / "aerolink"
# Packages and modules whose strings a screen can show. Offline tools (eval, realdata, sim, forecast, airspace
# builders) print reports for developers and are out of scope.
SCANNED = ["service", "summary", "adsb_live", "traffic.py", "navigation", "routing", "offroute", "demand"]


def at_local(h: int, m: int) -> float:
    return datetime(2026, 11, 26, h, m, tzinfo=FL_TZ).timestamp()


def _string_nodes(tree: ast.AST):
    """Every string constant that is not a docstring."""
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node


def test_no_symbol_in_any_string_literal_of_the_user_facing_modules():
    bad: list[str] = []
    for name in SCANNED:
        base = SRC / name
        for path in [base] if base.is_file() else sorted(base.rglob("*.py")):
            for node in _string_nodes(ast.parse(path.read_text(encoding="utf-8"))):
                if SYMBOLS.search(str(node.value)):
                    bad.append(f"{path.relative_to(SRC)}:{node.lineno}: {node.value!r}")
    assert bad == []


def test_ops_overview_sentences_for_every_fact_kind_have_no_symbols():
    cong = congestion(
        [site("ev-orl", "Orlando Exec", 1, busy_from(10, "heavy", upto=20)), site("ev-lal", "Lakeland", 2)]
    )
    rs = [route("A", "ev-orl", at(12), at(12), at(13)), route("B", "ev-orl", at(12), at(12), at(13))]
    lines = [ov.sentence(f) for f in facts(rs, cong)]
    lines += [ov.sentence({"kind": "none"}), ov._span(at(-5), at(10)), ov._span(at(10), at(100))]  # type: ignore[arg-type]
    assert lines and [t for t in lines if SYMBOLS.search(t)] == []


def test_clock_ranges_use_words_not_dashes():
    for a, b in ((at_local(19, 42), at_local(19, 50)), (at_local(11, 58), at_local(12, 5))):
        assert SYMBOLS.search(clock_range(a, b)) is None and " to " in clock_range(a, b)


def test_route_summary_template_has_no_symbols():
    assert SYMBOLS.search(llm_mod.template_summary(RESPONSE)) is None


@pytest.mark.parametrize("prompt", [ov.OVERVIEW_PROMPT, ov.FLIGHT_PROMPT, llm_mod.SYSTEM_PROMPT])
def test_every_llm_prompt_asks_for_plain_english(prompt: str):
    assert "plain English with no symbols, arrows, bullets or dashes" in prompt
    assert SYMBOLS.search(prompt) is None


@pytest.mark.parametrize("cut", [ov.fit, lambda t: ov._cut(t, ov.FLIGHT_MAX_CHARS)])
def test_long_text_is_cut_at_a_whole_word_with_a_period_and_no_ellipsis(cut):
    long_text = "Orlando Exec: heavy traffic forecast, " + "alpha beta gamma, " * 40
    out = cut(long_text)
    assert len(out) <= max(ov.MAX_CHARS, ov.FLIGHT_MAX_CHARS) and out.endswith(".") and SYMBOLS.search(out) is None
    assert out[:-1] in long_text and long_text[len(out) - 1] in " ,"  # ended at a word boundary
    assert cut("Short.") == "Short."
