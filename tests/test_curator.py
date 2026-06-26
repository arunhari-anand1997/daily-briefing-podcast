"""Unit tests for curator.py — Pass 2 labels + digest parsing (DI'd chat_fn)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import curator  # noqa: E402


def _curated():
    return {"segments": {
        "world": [
            {"headline": "France election heats up", "detail": "..."},
            {"headline": "New summit announced", "detail": "..."},
        ],
        "tech_business": [
            {"headline": "OpenAI ships model", "detail": "..."},
        ],
    }, "editor_note": "n"}


# ─── parse_labels ────────────────────────────────────────────────────────────

def test_parse_labels_basic():
    raw = json.dumps({"labels": [
        {"headline": "France election heats up", "status": "thread_continuation",
         "relates_to_thread_id": "france-election"},
        {"headline": "OpenAI ships model", "status": "update",
         "update_note": "benchmark numbers released"},
    ]})
    labels = curator.parse_labels(raw)
    assert labels["france election heats up"]["status"] == "thread_continuation"
    assert labels["openai ships model"]["update_note"] == "benchmark numbers released"


def test_parse_labels_tolerates_fences_and_prose():
    raw = "Sure!\n```json\n" + json.dumps({"labels": [
        {"headline": "X", "status": "new"}]}) + "\n```"
    assert curator.parse_labels(raw)["x"]["status"] == "new"


def test_parse_labels_drops_invalid_status():
    raw = json.dumps({"labels": [{"headline": "X", "status": "bogus"}]})
    assert curator.parse_labels(raw) == {}


def test_parse_labels_garbage_returns_empty():
    assert curator.parse_labels("not json at all") == {}


# ─── apply_labels ────────────────────────────────────────────────────────────

def test_apply_labels_drops_repeats_and_keeps_updates():
    labels = {
        "france election heats up": {"status": "thread_continuation",
                                     "relates_to_thread_id": "france-election",
                                     "update_note": ""},
        "new summit announced": {"status": "repeat", "relates_to_thread_id": "",
                                 "update_note": ""},
        "openai ships model": {"status": "update", "relates_to_thread_id": "",
                               "update_note": "benchmarks released"},
    }
    out, log = curator.apply_labels(_curated(), labels)
    world = out["segments"]["world"]
    heads = [w["headline"] for w in world]
    assert "New summit announced" not in heads          # repeat dropped
    assert "France election heats up" in heads
    fr = next(w for w in world if w["headline"] == "France election heats up")
    assert fr["status"] == "thread_continuation"
    assert fr["relates_to_thread_id"] == "france-election"
    assert fr["update_note"]  # synthesized so dedupe keeps it
    oai = out["segments"]["tech_business"][0]
    assert oai["status"] == "update" and oai["update_note"] == "benchmarks released"


def test_apply_labels_unlabeled_defaults_to_new():
    out, _ = curator.apply_labels(_curated(), {})  # no labels at all
    assert all(it["status"] == "new"
               for seg in out["segments"].values() for it in seg)


def test_apply_labels_fuzzy_prefix_match():
    # label headline is a truncated/prefix variant of the pool headline
    labels = {"france election": {"status": "repeat", "relates_to_thread_id": "",
                                  "update_note": ""}}
    out, _ = curator.apply_labels(_curated(), labels)
    heads = [w["headline"] for w in out["segments"]["world"]]
    assert "France election heats up" not in heads  # matched by prefix, dropped


# ─── memory_review (fake chat_fn) ────────────────────────────────────────────

def test_memory_review_applies_labels():
    def fake_chat(prompt, *, model, max_tokens, system, temperature):
        return json.dumps({"labels": [
            {"headline": "France election heats up", "status": "repeat"},
            {"headline": "New summit announced", "status": "new"},
            {"headline": "OpenAI ships model", "status": "update",
             "update_note": "new benchmark"},
        ]})
    out = curator.memory_review(
        _curated(), recaps_block="...", open_threads={"threads": []},
        weekly_digest=None, date_str="2026-06-25", chat_fn=fake_chat,
        model="sonar")
    heads = [w["headline"] for w in out["segments"]["world"]]
    assert "France election heats up" not in heads  # repeat dropped


def test_memory_review_non_fatal_on_chat_error():
    def boom(*a, **k):
        raise RuntimeError("api down")
    curated = _curated()
    out = curator.memory_review(
        curated, recaps_block="", open_threads={"threads": []},
        weekly_digest=None, date_str="2026-06-25", chat_fn=boom, model="sonar")
    # unchanged: still has both world topics
    assert len(out["segments"]["world"]) == 2


def test_memory_review_non_fatal_on_unparseable():
    out = curator.memory_review(
        _curated(), recaps_block="", open_threads={"threads": []},
        weekly_digest=None, date_str="2026-06-25",
        chat_fn=lambda *a, **k: "garbage", model="sonar")
    assert len(out["segments"]["world"]) == 2


# ─── digest generation (fake chat_fn) ────────────────────────────────────────

def test_generate_weekly_digest_stamps_metadata():
    payload = {"threads": [{"thread_id": "t", "label": "T", "episodes": ["2026-06-22"]}],
               "closed_stories": [], "single_day_stories": ["x"]}
    out = curator.generate_weekly_digest(
        [{"date": "2026-06-22", "topics": ["t"], "entities": ["E"]}],
        iso_week="2026-W25", start_date="2026-06-22", end_date="2026-06-28",
        chat_fn=lambda *a, **k: json.dumps(payload), model="sonar")
    assert out["iso_week"] == "2026-W25"
    assert out["start_date"] == "2026-06-22" and out["end_date"] == "2026-06-28"
    assert out["threads"][0]["thread_id"] == "t"


def test_generate_monthly_digest_handles_bad_json():
    out = curator.generate_monthly_digest(
        [{"date": "2026-06-01", "topics": [], "entities": []}],
        month="2026-06", chat_fn=lambda *a, **k: "not json", model="sonar")
    assert out["month"] == "2026-06"
    assert out["threads"] == [] and out["single_day_stories"] == []
