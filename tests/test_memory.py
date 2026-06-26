"""Unit tests for memory.py — the pure tiered-recap data layer.

No network/LLM. Uses tmp_path for isolation plus the repo's real recaps/ as a
read-only fixture sanity check.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory  # noqa: E402

REPO_RECAPS = Path(__file__).resolve().parent.parent / "recaps"


def _write_daily(d: Path, date_str: str, **extra) -> None:
    rec = {"date": date_str, "headline": f"h-{date_str}",
           "topics": [f"topic-{date_str}"], "entities": ["X"], "summary": "s"}
    rec.update(extra)
    (d / f"{date_str}.json").write_text(json.dumps(rec))


# ─── key helpers ─────────────────────────────────────────────────────────────

def test_iso_week_key():
    assert memory.iso_week_key(dt.date(2026, 6, 25)) == "2026-W26"
    # zero-padded single-digit week
    assert memory.iso_week_key(dt.date(2026, 1, 5)) == "2026-W02"


def test_month_key():
    assert memory.month_key(dt.date(2026, 6, 25)) == "2026-06"
    assert memory.month_key(dt.date(2026, 12, 1)) == "2026-12"


def test_iso_week_bounds_is_monday_to_sunday():
    mon, sun = memory.iso_week_bounds(dt.date(2026, 6, 25))  # a Thursday
    assert mon.isoweekday() == 1 and sun.isoweekday() == 7
    assert (sun - mon).days == 6
    assert mon <= dt.date(2026, 6, 25) <= sun


def test_slugify_thread():
    assert memory.slugify_thread("France 2026 presidential election") == \
        "france-2026-presidential-election"
    assert memory.slugify_thread("  OpenAI / Anthropic!! ") == "openai-anthropic"
    assert memory.slugify_thread("") == "thread"


# ─── daily recaps ────────────────────────────────────────────────────────────

def test_load_daily_recaps_skips_end_date_and_orders(tmp_path):
    for ds in ("2026-06-20", "2026-06-21", "2026-06-22"):
        _write_daily(tmp_path, ds)
    # end_date = 06-22 -> should load 20 and 21, NOT 22, oldest-first
    recs = memory.load_daily_recaps(tmp_path, dt.date(2026, 6, 22), lookback_days=7)
    assert [r["date"] for r in recs] == ["2026-06-20", "2026-06-21"]


def test_load_daily_recaps_respects_lookback_window(tmp_path):
    for day in range(1, 11):
        _write_daily(tmp_path, f"2026-06-{day:02d}")
    recs = memory.load_daily_recaps(tmp_path, dt.date(2026, 6, 10), lookback_days=3)
    assert [r["date"] for r in recs] == ["2026-06-07", "2026-06-08", "2026-06-09"]


def test_load_daily_recap_missing_returns_none(tmp_path):
    assert memory.load_daily_recap(tmp_path, dt.date(2026, 6, 1)) is None


def test_load_daily_recap_bad_json_returns_none(tmp_path):
    (tmp_path / "2026-06-01.json").write_text("{not valid")
    assert memory.load_daily_recap(tmp_path, dt.date(2026, 6, 1)) is None


def test_load_daily_recaps_in_range_inclusive(tmp_path):
    for ds in ("2026-06-20", "2026-06-22", "2026-06-23"):  # 21 intentionally missing
        _write_daily(tmp_path, ds)
    recs = memory.load_daily_recaps_in_range(
        tmp_path, dt.date(2026, 6, 20), dt.date(2026, 6, 23))
    assert [r["date"] for r in recs] == ["2026-06-20", "2026-06-22", "2026-06-23"]


def test_daily_recap_dates_ignores_digests_and_garbage(tmp_path):
    _write_daily(tmp_path, "2026-06-20")
    _write_daily(tmp_path, "2026-06-21")
    (tmp_path / "weekly_2026-W25.json").write_text("{}")
    (tmp_path / "open_threads.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("hi")
    assert memory.daily_recap_dates(tmp_path) == \
        [dt.date(2026, 6, 20), dt.date(2026, 6, 21)]


def test_prune_daily_recaps(tmp_path):
    for day in range(1, 21):
        _write_daily(tmp_path, f"2026-06-{day:02d}")
    pruned = memory.prune_daily_recaps(tmp_path, dt.date(2026, 6, 20), keep_days=14)
    remaining = {d.isoformat() for d in memory.daily_recap_dates(tmp_path)}
    # cutoff = 06-06; anything strictly older than 06-06 is pruned
    assert all(p < dt.date(2026, 6, 6) for p in pruned)
    assert "2026-06-06" in remaining and "2026-06-01" not in remaining


# ─── weekly / monthly digests ────────────────────────────────────────────────

def test_weekly_digest_roundtrip_and_exists(tmp_path):
    digest = {"iso_week": "2026-W25", "start_date": "2026-06-15",
              "end_date": "2026-06-21", "threads": []}
    assert not memory.weekly_digest_exists(tmp_path, "2026-W25")
    memory.save_weekly_digest(tmp_path, digest)
    assert memory.weekly_digest_exists(tmp_path, "2026-W25")
    assert memory.load_weekly_digest(tmp_path, "2026-W25")["start_date"] == "2026-06-15"


def test_monthly_digest_roundtrip(tmp_path):
    digest = {"month": "2026-05", "threads": []}
    memory.save_monthly_digest(tmp_path, digest)
    assert memory.monthly_digest_exists(tmp_path, "2026-05")
    assert memory.load_monthly_digest(tmp_path, "2026-05")["month"] == "2026-05"


def test_latest_weekly_digest_picks_newest(tmp_path):
    for wk, end in [("2026-W23", "2026-06-07"), ("2026-W25", "2026-06-21"),
                    ("2026-W24", "2026-06-14")]:
        memory.save_weekly_digest(tmp_path, {"iso_week": wk, "end_date": end})
    assert memory.latest_weekly_digest(tmp_path)["iso_week"] == "2026-W25"


def test_latest_weekly_digest_before_filter(tmp_path):
    for wk, end in [("2026-W24", "2026-06-14"), ("2026-W25", "2026-06-21")]:
        memory.save_weekly_digest(tmp_path, {"iso_week": wk, "end_date": end})
    got = memory.latest_weekly_digest(tmp_path, before=dt.date(2026, 6, 16))
    assert got["iso_week"] == "2026-W24"


def test_latest_weekly_digest_none_when_empty(tmp_path):
    assert memory.latest_weekly_digest(tmp_path) is None


# ─── open threads ────────────────────────────────────────────────────────────

def test_load_open_threads_skeleton_when_missing(tmp_path):
    ot = memory.load_open_threads(tmp_path)
    assert ot == {"updated": "", "threads": []}


def test_load_open_threads_skeleton_when_broken(tmp_path):
    (tmp_path / "open_threads.json").write_text("[]")  # wrong shape
    assert memory.load_open_threads(tmp_path)["threads"] == []


def test_upsert_thread_new_then_update(tmp_path):
    ot = memory.load_open_threads(tmp_path)
    memory.upsert_thread(ot, thread_id="france-election", label="France election",
                         episode_date="2026-06-22", hooks=["polls tightened"])
    assert len(ot["threads"]) == 1
    t = ot["threads"][0]
    assert t["first_episode"] == "2026-06-22" and t["last_episode"] == "2026-06-22"

    memory.upsert_thread(ot, thread_id="france-election", label="France election",
                         episode_date="2026-06-25", hooks=["Mélenchon refused"],
                         next_milestone="Runoff July 7")
    assert len(ot["threads"]) == 1  # updated, not duplicated
    t = ot["threads"][0]
    assert t["first_episode"] == "2026-06-22"  # preserved
    assert t["last_episode"] == "2026-06-25"   # advanced
    assert t["next_milestone"] == "Runoff July 7"
    assert "polls tightened" in t["hooks"] and "Mélenchon refused" in t["hooks"]


def test_upsert_thread_hook_dedup_and_cap(tmp_path):
    ot = {"updated": "", "threads": []}
    for i in range(10):
        memory.upsert_thread(ot, thread_id="t", label="T",
                             episode_date="2026-06-25", hooks=[f"hook{i}", "hook0"])
    hooks = ot["threads"][0]["hooks"]
    assert len(hooks) <= 6
    assert len(hooks) == len(set(hooks))  # no dupes


def test_close_thread(tmp_path):
    ot = {"updated": "", "threads": [
        {"thread_id": "a", "status": "developing", "last_episode": "2026-06-25"}]}
    assert memory.close_thread(ot, "a") is True
    assert ot["threads"][0]["status"] == "closed"
    assert memory.close_thread(ot, "missing") is False


def test_active_open_threads_excludes_closed():
    ot = {"threads": [
        {"thread_id": "a", "status": "developing"},
        {"thread_id": "b", "status": "closed"},
        {"thread_id": "c", "status": "dormant"}]}
    ids = {t["thread_id"] for t in memory.active_open_threads(ot)}
    assert ids == {"a", "c"}


def test_mark_dormant_threads():
    today = dt.date(2026, 6, 25)
    ot = {"threads": [
        {"thread_id": "old", "status": "developing", "last_episode": "2026-06-01"},
        {"thread_id": "fresh", "status": "developing", "last_episode": "2026-06-24"},
        {"thread_id": "closed", "status": "closed", "last_episode": "2026-05-01"}]}
    n = memory.mark_dormant_threads(ot, today, dormant_after_days=14)
    assert n == 1
    by_id = {t["thread_id"]: t["status"] for t in ot["threads"]}
    assert by_id == {"old": "dormant", "fresh": "developing", "closed": "closed"}


def test_mark_dormant_ignores_bad_dates():
    ot = {"threads": [{"thread_id": "x", "status": "developing", "last_episode": ""}]}
    assert memory.mark_dormant_threads(ot, dt.date(2026, 6, 25)) == 0


# ─── real repo fixtures ──────────────────────────────────────────────────────

@pytest.mark.skipif(not REPO_RECAPS.exists(), reason="repo recaps/ not present")
def test_real_recaps_load_and_have_expected_shape():
    dates = memory.daily_recap_dates(REPO_RECAPS)
    assert dates, "expected at least one daily recap in repo"
    rec = memory.load_daily_recap(REPO_RECAPS, dates[-1])
    assert isinstance(rec, dict)
    for key in ("date", "topics", "entities", "summary"):
        assert key in rec
