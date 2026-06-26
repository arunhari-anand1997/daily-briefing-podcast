"""Unit tests for editorial.py — Tier 3 running-threads + timeline (pure)."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import editorial  # noqa: E402


def _recap(date, entities, topic_details=None):
    return {"date": date, "entities": entities, "topics": [],
            "topic_details": topic_details or []}


# ─── running threads ─────────────────────────────────────────────────────────

def test_detect_running_threads_min_episodes():
    recaps = [
        _recap("2026-06-23", ["OpenAI", "Macron"]),
        _recap("2026-06-24", ["OpenAI", "Apple"]),
        _recap("2026-06-25", ["OpenAI", "Macron"]),
    ]
    threads = editorial.detect_running_threads(recaps, min_episodes=3)
    ids = {t["entity"] for t in threads}
    assert "openai" in ids          # 3 episodes
    assert "macron" not in ids      # only 2
    assert "apple" not in ids       # only 1


def test_detect_running_threads_dedups_within_episode():
    recaps = [_recap("2026-06-25", ["OpenAI", "OpenAI", "OpenAI"])]
    # same entity many times in ONE episode != 3 episodes
    assert editorial.detect_running_threads(recaps, min_episodes=3) == []


def test_detect_running_threads_filters_stopwords():
    recaps = [_recap(f"2026-06-2{i}", ["China", "U.S.", "OpenAI"]) for i in range(3, 6)]
    threads = editorial.detect_running_threads(recaps, min_episodes=3)
    ids = {t["entity"] for t in threads}
    assert ids == {"openai"}  # China / U.S. are stoplisted


def test_detect_running_threads_caps_and_orders():
    recaps = []
    for i in range(3, 6):
        recaps.append(_recap(f"2026-06-2{i}", ["Alpha", "Beta", "Gamma", "Delta"]))
    threads = editorial.detect_running_threads(recaps, min_episodes=3, max_threads=2)
    assert len(threads) == 2


def test_format_running_threads_block_relative_days():
    today = dt.date(2026, 6, 25)
    threads = [{"display": "OpenAI", "entity": "openai",
                "episodes": ["2026-06-23", "2026-06-24", "2026-06-25"], "count": 3}]
    block = editorial.format_running_threads_block(threads, today)
    assert "OpenAI" in block
    assert "today" in block and "yesterday" in block
    assert "Tue" in block  # 2026-06-23 is a Tuesday


def test_format_running_threads_block_empty():
    assert editorial.format_running_threads_block([], dt.date(2026, 6, 25)) == ""


# ─── prior narration ─────────────────────────────────────────────────────────

def _td(topic, entities, segment, narration):
    return {"topic": topic, "entities": entities, "segment": segment,
            "narration": narration}


def test_find_prior_narration_matches_by_entity_overlap():
    recaps = [
        _recap("2026-06-22", ["France", "Macron", "Le Pen"], [
            _td("France election heats up", ["France", "Macron", "Le Pen"], "world",
                ["Maya: Macron's lead is narrowing.", "Ben: Le Pen smells blood."])]),
    ]
    got = editorial.find_prior_narration(
        recaps, entities={"Macron", "Le Pen", "France"}, today=dt.date(2026, 6, 25))
    assert got is not None
    assert got["days_ago"] == 3
    assert got["segment"] == "world"
    assert len(got["narration"]) == 2


def test_find_prior_narration_prefers_most_recent():
    recaps = [
        _recap("2026-06-20", ["Fed", "Powell"], [
            _td("Fed holds", ["Fed", "Powell"], "economy", ["old line"])]),
        _recap("2026-06-24", ["Fed", "Powell"], [
            _td("Fed signals cut", ["Fed", "Powell"], "economy", ["newer line"])]),
    ]
    got = editorial.find_prior_narration(
        recaps, entities={"Powell"}, today=dt.date(2026, 6, 25), min_overlap=1)
    assert got["date"] == "2026-06-24"
    assert got["narration"] == ["newer line"]


def test_find_prior_narration_none_when_no_overlap():
    recaps = [_recap("2026-06-24", ["Apple"], [
        _td("Apple ships", ["Apple"], "tech_business", ["a line"])])]
    assert editorial.find_prior_narration(
        recaps, entities={"Tesla", "Musk"}, today=dt.date(2026, 6, 25)) is None


def test_find_prior_narration_skips_topic_without_narration():
    recaps = [_recap("2026-06-24", ["Tesla"], [
        _td("Tesla news", ["Tesla", "Musk"], "tech_business", [])])]
    assert editorial.find_prior_narration(
        recaps, entities={"Tesla", "Musk"}, today=dt.date(2026, 6, 25)) is None


def test_find_prior_narration_handles_old_recaps_without_topic_details():
    recaps = [{"date": "2026-06-24", "entities": ["Tesla"], "topics": ["t"]}]
    assert editorial.find_prior_narration(
        recaps, entities={"Tesla"}, today=dt.date(2026, 6, 25)) is None


# ─── timeline block ──────────────────────────────────────────────────────────

def test_format_timeline_block_renders_quote_and_instruction():
    updates = [{
        "topic": "France election",
        "update_note": "Polls tightened 3 points.",
        "prior": {"days_ago": 3, "narration": ["Maya: Macron's lead is narrowing."],
                  "topic": "France election", "segment": "world"},
    }]
    block = editorial.format_timeline_block(updates)
    assert "3 days ago" in block
    assert "Macron's lead is narrowing" in block
    assert "Polls tightened" in block
    assert "picking up where we left off" in block


def test_format_timeline_block_skips_updates_without_prior():
    assert editorial.format_timeline_block([{"topic": "x", "prior": None}]) == ""


# ─── build_topic_details ─────────────────────────────────────────────────────

def test_build_topic_details_assigns_lines_by_entity_overlap():
    dialogue = [
        {"speaker": "kore", "text": "Welcome back.", "segment": "world", "is_intro": True},
        {"speaker": "kore", "text": "Macron's lead is narrowing in France.", "segment": "world"},
        {"speaker": "charon", "text": "Le Pen smells blood.", "segment": "world"},
        {"speaker": "kore", "text": "Meanwhile OpenAI shipped a new model.", "segment": "tech_business"},
    ]
    curated = {
        "world": [{"headline": "France election", "entities": ["France", "Macron", "Le Pen"]}],
        "tech_business": [{"headline": "OpenAI ships", "entities": ["OpenAI"]}],
    }
    details = editorial.build_topic_details(dialogue, curated)
    by_seg = {d["segment"]: d for d in details}
    assert set(by_seg) == {"world", "tech_business"}
    # intro line excluded; two world lines captured
    assert len(by_seg["world"]["narration"]) == 2
    assert by_seg["world"]["narration"][0].startswith("Maya:")
    assert "OpenAI" in by_seg["tech_business"]["narration"][0]


def test_build_topic_details_skips_topics_with_no_lines():
    dialogue = [{"speaker": "kore", "text": "Apple news only.", "segment": "tech_business"}]
    curated = {"tech_business": [
        {"headline": "Apple", "entities": ["Apple"]},
        {"headline": "Tesla", "entities": ["Tesla"]}]}
    details = editorial.build_topic_details(dialogue, curated)
    # Tesla topic gets no lines -> excluded; Apple present
    assert [d["topic"] for d in details] == ["Apple"]


def test_build_topic_details_roundtrips_into_find_prior_narration():
    dialogue = [
        {"speaker": "kore", "text": "Macron's lead is narrowing.", "segment": "world"},
        {"speaker": "charon", "text": "Le Pen smells blood.", "segment": "world"},
    ]
    curated = {"world": [{"headline": "France election",
                          "entities": ["France", "Macron", "Le Pen"]}]}
    details = editorial.build_topic_details(dialogue, curated)
    recap = {"date": "2026-06-22", "entities": ["France", "Macron"],
             "topic_details": details}
    got = editorial.find_prior_narration(
        [recap], entities={"Macron", "Le Pen"}, today=dt.date(2026, 6, 25))
    assert got is not None and got["days_ago"] == 3
    assert any("Macron" in ln for ln in got["narration"])
