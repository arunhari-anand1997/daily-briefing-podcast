"""Integration tests for the v7 wiring in podcast.py.

The LLM boundary (pplx_chat) and RECAPS_DIR are monkeypatched, so these run
without API keys and validate the glue we can't exercise live: Pass 2 labels,
editorial blocks reaching the writer prompt, recap topic_details, open-threads
evolution, and digest generation.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("httpx")  # podcast.py imports httpx at module load
import podcast  # noqa: E402
import memory  # noqa: E402


def _curated_world(status=None):
    topic = {"headline": "France election heats up", "detail": "Macron vs Le Pen",
             "entities": ["France", "Macron", "Le Pen"], "url": "http://x", "beat": "geopolitics"}
    if status:
        topic["status"] = status
    return {"segments": {"world": [topic]}, "editor_note": "n"}


def _recaps_with_thread():
    """3 episodes mentioning Macron/Le Pen, the latest with narration."""
    return [
        {"date": "2026-06-21", "entities": ["Macron", "Le Pen"], "topics": ["t"]},
        {"date": "2026-06-22", "entities": ["Macron", "Le Pen"], "topics": ["t"]},
        {"date": "2026-06-23", "entities": ["Macron", "Le Pen"], "topics": ["t"],
         "topic_details": [{"topic": "France election", "segment": "world",
                            "entities": ["France", "Macron", "Le Pen"],
                            "narration": ["Maya: Macron's lead is narrowing.",
                                          "Ben: Le Pen smells blood."]}]},
    ]


# ─── Pass 2 wiring ───────────────────────────────────────────────────────────

def test_run_two_pass_curation_drops_repeat(monkeypatch, tmp_path):
    monkeypatch.setattr(podcast, "RECAPS_DIR", tmp_path)
    monkeypatch.setattr(podcast, "pplx_chat",
                        lambda *a, **k: json.dumps({"labels": [
                            {"headline": "France election heats up", "status": "repeat"}]}))
    out = podcast.run_two_pass_curation(_curated_world(), "recap-block",
                                        _recaps_with_thread(), "2026-06-25")
    assert out["segments"]["world"] == []  # repeat dropped


# ─── editorial blocks reach the writer prompt ────────────────────────────────

def test_generate_script_injects_editorial_blocks(monkeypatch):
    captured = {}

    def fake_chat(prompt, *, model=None, max_tokens=None, system=None, temperature=None):
        captured["prompt"] = prompt
        return json.dumps([{"speaker": "kore", "text": "A line about Macron and France."}])

    monkeypatch.setattr(podcast, "pplx_chat", fake_chat)
    segs = [{"key": "world", "label": "World", "words": 300}]
    podcast.generate_script(_curated_world(status="thread_continuation"), "MKT",
                            "recap-block", segs, "2026-06-25",
                            recaps=_recaps_with_thread(), two_pass=True)
    assert "RUNNING THREADS THIS WEEK" in captured["prompt"]
    assert "STORY TIMELINE" in captured["prompt"]
    assert "Macron's lead is narrowing" in captured["prompt"]  # prior narration quoted


def test_generate_script_no_blocks_when_flag_off(monkeypatch):
    captured = {}

    def fake_chat(prompt, *, model=None, max_tokens=None, system=None, temperature=None):
        captured["prompt"] = prompt
        return json.dumps([{"speaker": "kore", "text": "x"}])

    monkeypatch.setattr(podcast, "pplx_chat", fake_chat)
    segs = [{"key": "world", "label": "World", "words": 300}]
    podcast.generate_script(_curated_world(status="thread_continuation"), "MKT",
                            "recap-block", segs, "2026-06-25",
                            recaps=_recaps_with_thread(), two_pass=False)
    assert "RUNNING THREADS THIS WEEK" not in captured["prompt"]
    assert "STORY TIMELINE" not in captured["prompt"]


# ─── recap topic_details ─────────────────────────────────────────────────────

def test_extract_recap_attaches_topic_details(monkeypatch):
    monkeypatch.setattr(podcast, "pplx_chat", lambda *a, **k: json.dumps(
        {"headline": "h", "topics": ["France election"], "entities": ["Macron"], "summary": "s"}))
    dialogue = [
        {"speaker": "kore", "text": "Macron's lead is narrowing.", "segment": "world"},
        {"speaker": "charon", "text": "Le Pen smells blood.", "segment": "world"},
    ]
    curated_segments = {"world": [{"headline": "France election",
                                   "entities": ["France", "Macron", "Le Pen"]}]}
    recap = podcast.extract_recap(dialogue, "2026-06-25", curated_segments=curated_segments)
    assert "topic_details" in recap
    assert recap["topic_details"][0]["segment"] == "world"
    assert len(recap["topic_details"][0]["narration"]) == 2


def test_extract_recap_no_topic_details_without_curated(monkeypatch):
    monkeypatch.setattr(podcast, "pplx_chat", lambda *a, **k: json.dumps(
        {"headline": "h", "topics": [], "entities": [], "summary": "s"}))
    recap = podcast.extract_recap([{"speaker": "kore", "text": "x", "segment": "world"}],
                                  "2026-06-25")
    assert "topic_details" not in recap  # v6.1 path unchanged


# ─── open threads evolution ──────────────────────────────────────────────────

def test_update_open_threads_file_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(podcast, "RECAPS_DIR", tmp_path)
    curated = _curated_world(status="thread_continuation")
    curated["segments"]["world"][0]["relates_to_thread_id"] = "france-election"
    curated["segments"]["world"][0]["update_note"] = "polls tightened"
    podcast.update_open_threads_file(curated, _recaps_with_thread(), "2026-06-25")
    ot = memory.load_open_threads(tmp_path)
    assert ot["updated"] == "2026-06-25"
    ids = {t["thread_id"] for t in ot["threads"]}
    assert "france-election" in ids
    fr = next(t for t in ot["threads"] if t["thread_id"] == "france-election")
    assert "polls tightened" in fr["hooks"]


# ─── digest generation timing ────────────────────────────────────────────────

def test_enforce_news_floor_fills_and_strips_reddit():
    curated = {"segments": {
        "hard_news": [],  # empty -> must be filled
        "politics": [{"headline": "r/changemyview debate on X", "beat": "reddit"}],  # filler only
    }}
    research = {
        "geopolitics": [{"headline": "Iran war escalates with new strikes"},
                        {"headline": "Ukraine hits Russian refinery"}],
        "us_politics": [{"headline": "Supreme Court rules on birthright citizenship"},
                        {"headline": "Senate passes budget bill"}],
        "economics": [{"headline": "Jobs report beats expectations"}],
    }
    out = podcast.enforce_news_floor(curated, research, min_hard_news=3)
    hn = out["segments"]["hard_news"]
    assert len(hn) >= 3  # floor satisfied
    assert all(it.get("beat") != "reddit" for it in hn)  # no forum filler in hard news
    pol = out["segments"]["politics"]
    # politics reinforced with real reporting up front; reddit filler kept as backup
    assert pol[0].get("beat") in ("us_politics", "geopolitics")
    assert any(it.get("beat") == "us_politics" for it in pol)


def test_curate_topics_falls_back_on_api_failure(monkeypatch):
    # A curator API failure (e.g. 400) must NOT crash the run — fall back deterministically.
    def boom(*a, **k):
        raise RuntimeError("400 Bad Request")
    monkeypatch.setattr(podcast, "pplx_chat", boom)
    research = {
        "geopolitics": [{"headline": "Iran war escalates with new strikes", "detail": "war", "url": "http://x"}],
        "us_politics": [{"headline": "Senate passes major budget bill", "detail": "vote", "url": "http://y"}],
    }
    out = podcast.curate_topics(research, "recap-block", "2026-06-30")
    assert "segments" in out and isinstance(out["segments"], dict)
    # fallback routed by keyword: a war story lands in hard_news, a senate vote in politics
    placed = {it.get("headline") for seg in out["segments"].values() for it in (seg or [])}
    assert "Iran war escalates with new strikes" in placed


def test_currency_check_drops_stale_anniversary_item(monkeypatch):
    curated = {"segments": {"hard_news": [
        {"headline": "House passes One Big Beautiful Bill after all-night session", "detail": "..."},
        {"headline": "Russia launches major barrage on Ukraine", "detail": "..."},
    ]}}

    def fake_chat(*a, **k):
        return json.dumps({"items": [
            {"id": 1, "verdict": "stale", "actually_happened": "July 2025",
             "reason": "the bill passed a year ago; this is the anniversary"},
            {"id": 2, "verdict": "recent", "actually_happened": "last few days", "reason": "ongoing war"},
        ]})

    monkeypatch.setattr(podcast, "pplx_chat", fake_chat)
    out = podcast.currency_check_news(curated, "2026-07-02")
    heads = [it["headline"] for it in out["segments"]["hard_news"]]
    assert "House passes One Big Beautiful Bill after all-night session" not in heads  # dropped
    assert "Russia launches major barrage on Ukraine" in heads                          # kept


def test_currency_check_non_fatal_on_api_error(monkeypatch):
    curated = {"segments": {"hard_news": [{"headline": "Some story", "detail": "x"}]}}
    monkeypatch.setattr(podcast, "pplx_chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = podcast.currency_check_news(curated, "2026-07-02")
    assert out["segments"]["hard_news"]  # unchanged, no crash


def test_gather_research_retries_empty_beats(monkeypatch):
    import collections
    calls = collections.Counter()

    def fake_beat(label, prompt, preamble):
        calls[label] += 1
        # us_politics fails (empty) twice, then recovers; everything else is fine immediately
        if label == "us_politics" and calls[label] < 3:
            return []
        return [{"headline": f"{label} story", "beat": label}]

    monkeypatch.setattr(podcast, "_run_one_beat", fake_beat)
    res = podcast.gather_research(max_retry_rounds=2)
    assert res["us_politics"], "empty critical beat should recover after retries"
    assert calls["us_politics"] == 3       # initial + 2 retries
    assert calls["geopolitics"] == 1       # a beat that succeeded is not retried


def test_maybe_generate_digests_weekly_on_sunday(monkeypatch, tmp_path):
    monkeypatch.setattr(podcast, "RECAPS_DIR", tmp_path)
    # seed a few dailies in the week of 2026-06-28 (a Sunday)
    for ds in ("2026-06-24", "2026-06-25", "2026-06-26"):
        (tmp_path / f"{ds}.json").write_text(json.dumps(
            {"date": ds, "topics": ["t"], "entities": ["Macron"]}))
    monkeypatch.setattr(podcast, "pplx_chat", lambda *a, **k: json.dumps(
        {"threads": [{"thread_id": "france-election", "label": "France",
                      "episodes": ["2026-06-24"]}],
         "closed_stories": [], "single_day_stories": []}))
    podcast.maybe_generate_digests("2026-06-28")  # Sunday
    wk = memory.iso_week_key(dt.date(2026, 6, 28))
    assert memory.weekly_digest_exists(tmp_path, wk)
    digest = memory.load_weekly_digest(tmp_path, wk)
    assert digest["threads"][0]["thread_id"] == "france-election"


def test_maybe_generate_digests_skips_when_present_and_not_due(monkeypatch, tmp_path):
    monkeypatch.setattr(podcast, "RECAPS_DIR", tmp_path)
    # Wednesday 2026-06-24, this week's digest already exists -> no chat call
    wk = memory.iso_week_key(dt.date(2026, 6, 24))
    memory.save_weekly_digest(tmp_path, {"iso_week": wk, "end_date": "2026-06-28"})
    # also pre-create previous month's digest so monthly path is a no-op too
    memory.save_monthly_digest(tmp_path, {"month": "2026-05"})

    def boom(*a, **k):
        raise AssertionError("pplx_chat should not be called")

    monkeypatch.setattr(podcast, "pplx_chat", boom)
    podcast.maybe_generate_digests("2026-06-24")  # must not raise
