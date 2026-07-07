"""Guardrail tests for the v8 taste-spec editorial structure (config-level, no API)."""
from __future__ import annotations

import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("httpx")
import podcast  # noqa: E402

EXPECTED_KEYS = ["cold_open", "hard_news", "politics", "economy", "tech_ai", "science",
                 "feature", "culture", "sports", "disagreement", "signoff"]
CONTENT_KEYS = {"hard_news", "politics", "economy", "tech_ai", "science", "feature",
                "culture", "sports"}


def test_segment_lineup_matches_spec_order():
    assert [s["key"] for s in podcast.SEGMENTS_FULL] == EXPECTED_KEYS


def test_total_length_is_about_120_min():
    total = sum(s["words"] for s in podcast.SEGMENTS_FULL)
    minutes = total / 150  # deliver.py uses 150 wpm for chapters
    assert 110 <= minutes <= 130, f"~{minutes:.0f} min off target"


def test_every_segment_has_purpose_freshness():
    for s in podcast.SEGMENTS_FULL:
        assert s["key"] in podcast.SEGMENT_PURPOSE
        assert s["key"] in podcast.SEGMENT_FRESHNESS


def test_curator_targets_are_the_content_segments():
    assert set(podcast.SEGMENT_TARGET_TOPICS) == CONTENT_KEYS


def test_tech_ai_is_capped():
    # The cap keeps Tech & AI from dominating (spec: <= ~4 topics, <= 3 AI).
    assert podcast.SEGMENT_TARGET_TOPICS["tech_ai"] <= 4


def test_ground_truth_prices_protected_macro_checked():
    # Real Yahoo prices must be shielded; hallucinated macro must stay checkable.
    markets = "S&P 500: 7,357.49 (-0.84%)\nGold: 4,045.30 (-3.5%)\nBrent crude: 73.50 (-4.84%)"
    protected = podcast._ground_truth_numbers(markets)
    exact, large = protected
    assert "7357.49" in exact and "4045.30" in exact
    lines = [
        {"text": "Gold is around $4,045 and Brent is about $74 today.", "segment": "economy"},
        {"text": "Core PCE rose to 2.6% year over year in May.", "segment": "economy"},
    ]
    claims = podcast._extract_numeric_claims(lines, protected)
    toks = {podcast._norm_num(t) for _, _, t in claims}
    # rounded prices ($4,045≈4045.30, $74≈73.50) are fuzzy-protected (regression: gold→$2,300)
    assert "4045" not in toks and "74" not in toks
    # the PCE macro figure is still fact-checkable
    assert any("2.6" in t for _, _, t in claims)


def test_is_protected_fuzzy_only_for_large_values():
    exact, large = podcast._ground_truth_numbers("Gold: 4,045.30\nNasdaq change: 2.55%")
    assert podcast._is_protected("$4,050", (exact, large))   # within 1.5% of 4045.30
    assert not podcast._is_protected("2.6%", (exact, large)) # small macro not shielded by 2.55


def test_economy_market_prices_skipped_macro_checked():
    import re
    lines = [
        {"text": "Bitcoin is down 2.3% and the 10-year yield sits at 4.42%.", "segment": "economy"},
        {"text": "Core PCE inflation is running 3.4% year over year.", "segment": "economy"},
        {"text": "The final score was 108-102.", "segment": "sports"},
    ]
    claims = podcast._extract_numeric_claims(lines)  # no ground-truth protection here
    econ = [(i, s, t) for (i, s, t) in claims if lines[i]["segment"] == "economy"]
    # in economy, only the macro-data sentence (PCE/inflation) is checked; crypto/yield are left alone
    assert econ and all(re.search(r"pce|inflation", s, re.I) for _, s, _ in econ)
    assert not any(podcast._norm_num(t) in ("2.3", "4.42") for _, _, t in econ)
    # non-economy numbers (a sports scoreline) are still checked
    assert any(lines[i]["segment"] == "sports" for i, _, _ in claims)


def test_content_segments_set_matches_lineup():
    assert podcast.CONTENT_SEGMENTS == CONTENT_KEYS | {"cold_open", "disagreement"}


def test_writer_template_placeholders_resolve():
    needed = {f for _, f, _, _ in string.Formatter().parse(podcast.SCRIPT_SEGMENT_USER_TEMPLATE) if f}
    provided = {"today_preamble", "date", "day_name", "label", "words", "target_turns", "purpose",
                "freshness", "lookback", "recap_block", "research_block", "prior_segments_block",
                "editor_note", "timeline_block", "running_threads_block"}
    assert needed == provided


def test_headline_scan_picks_top_per_segment():
    seg_picks = {"hard_news": [{"headline": "A"}, {"headline": "B"}],
                 "science": [{"headline": "C"}], "sports": []}
    scan = podcast._headline_scan_topics(seg_picks)
    heads = [t["headline"] for t in scan]
    assert heads == ["A", "C"]  # top of hard_news, top of science; empty sports skipped
