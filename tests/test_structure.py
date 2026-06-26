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
    markets = "S&P 500: 7,357.49 (-0.84%)\nBrent crude: 74.13 (-4.84%)\nNVDA: 195.74 (-4.35%)"
    protected = podcast._ground_truth_numbers(markets)
    assert "7357.49" in protected and "74.13" in protected and "195.74" in protected
    lines = [
        {"text": "The S&P 500 is 7,357.49, down 0.84% on the day.", "segment": "economy"},
        {"text": "Core PCE rose to 2.6% year over year in May.", "segment": "economy"},
    ]
    claims = podcast._extract_numeric_claims(lines, protected)
    toks = {t for _, _, t in claims}
    # the injected price level is protected; the PCE figure is still fact-checkable
    assert not any(podcast._norm_num(t) == "7357.49" for t in toks)
    assert any("2.6" in t for t in toks)


def test_content_segments_set_matches_lineup():
    assert podcast.CONTENT_SEGMENTS == CONTENT_KEYS | {"cold_open", "disagreement"}


def test_writer_template_placeholders_resolve():
    needed = {f for _, f, _, _ in string.Formatter().parse(podcast.SCRIPT_SEGMENT_USER_TEMPLATE) if f}
    provided = {"today_preamble", "date", "day_name", "label", "words", "purpose", "freshness",
                "lookback", "recap_block", "research_block", "prior_segments_block", "editor_note",
                "timeline_block", "running_threads_block"}
    assert needed == provided


def test_headline_scan_picks_top_per_segment():
    seg_picks = {"hard_news": [{"headline": "A"}, {"headline": "B"}],
                 "science": [{"headline": "C"}], "sports": []}
    scan = podcast._headline_scan_topics(seg_picks)
    heads = [t["headline"] for t in scan]
    assert heads == ["A", "C"]  # top of hard_news, top of science; empty sports skipped
