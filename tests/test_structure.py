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


def test_markets_segment_excluded_from_factcheck():
    # economy numbers come from Yahoo ground truth; never let the verifier touch them.
    assert "economy" in podcast._FACT_CHECK_SKIP_SEGMENTS


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
