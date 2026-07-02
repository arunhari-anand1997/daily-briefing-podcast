"""Tests for the cold-open digest + recency claim extraction (pure, no API)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("httpx")
import podcast  # noqa: E402


# ─── _finished_show_digest ───────────────────────────────────────────────────

def test_finished_show_digest_uses_first_substantive_line():
    segments = [{"key": "cold_open", "label": "Cold open"},
                {"key": "economy", "label": "Economy"},
                {"key": "signoff", "label": "Outro"}]
    results = {"economy": [
        {"text": "On to the economy.", "is_intro": True, "segment": "economy"},
        {"text": "Core PCE rose to 3.4% year over year in May, still well above target.",
         "segment": "economy"},
        {"text": "Markets sold off.", "segment": "economy"}]}
    digest = podcast._finished_show_digest(results, segments)
    assert len(digest) == 1
    # intro skipped; first >=12-word line chosen as the headline
    assert "3.4%" in digest[0]["headline"]
    assert digest[0]["beat"] == "Economy"


def test_finished_show_digest_excludes_meta_segments():
    segments = [{"key": "cold_open", "label": "C"}, {"key": "disagreement", "label": "D"},
                {"key": "signoff", "label": "O"}, {"key": "sports", "label": "Sports"}]
    results = {"disagreement": [{"text": "x" * 80, "segment": "disagreement"}],
               "sports": [{"text": "The home team won the final on Sunday in a thriller.",
                           "segment": "sports"}]}
    digest = podcast._finished_show_digest(results, segments)
    beats = [d["beat"] for d in digest]
    assert beats == ["Sports"]  # cold_open/disagreement/signoff excluded


# ─── _extract_recency_claims ─────────────────────────────────────────────────

def test_extract_recency_claims_matches_event_verbs():
    lines = [
        {"text": "The Knicks won their first title since 1973 on Thursday.", "segment": "sports"},
        {"text": "Here is some general analysis with no datable event at all.", "segment": "feature"},
        {"text": "Giannis was traded to Miami in a blockbuster.", "segment": "sports"},
    ]
    claims = podcast._extract_recency_claims(lines)
    idxs = {i for i, _ in claims}
    assert 0 in idxs and 2 in idxs and 1 not in idxs


def test_extract_recency_claims_skips_intro_and_economy():
    lines = [
        {"text": "Markets today: the S&P 500 dropped 0.8%.", "segment": "economy"},  # skipped seg
        {"text": "Welcome back.", "is_intro": True, "segment": "sports"},            # intro skipped
        {"text": "Ecuador defeated Germany on Wednesday to advance.", "segment": "sports"},
    ]
    claims = podcast._extract_recency_claims(lines)
    assert [i for i, _ in claims] == [2]


def test_extract_recency_claims_catches_legislative_and_temporal():
    # regression: the July 2 "One Big Beautiful Bill" line was NOT flagged (extractor gap)
    lines = [
        {"text": "Late Wednesday, House Republicans muscled through the One Big Beautiful Bill.",
         "segment": "hard_news"},
        {"text": "The Senate passed the measure overnight after an all-night session.",
         "segment": "politics"},
        {"text": "A federal court struck down the rule earlier this week.", "segment": "politics"},
    ]
    claims = podcast._extract_recency_claims(lines)
    assert len(claims) == 3  # all three now extracted for verification


def test_extract_recency_claims_prioritizes_sports_first():
    lines = [
        {"text": "A new study was released this week on longevity.", "segment": "science"},
        {"text": "The champions clinched the series on Sunday night.", "segment": "sports"},
    ]
    claims = podcast._extract_recency_claims(lines, max_claims=1)
    # sports has higher priority -> kept when capped to 1
    assert claims[0][0] == 1
