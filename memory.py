"""memory.py — tiered recap storage for the daily briefing podcast (v7, Tier 2).

Pure data layer. NO network / LLM calls live here so it can be unit-tested
without API keys. Every function that touches disk takes an explicit
``recaps_dir: Path`` so tests can point at a tmp dir or the repo's ``recaps/``.

Storage tiers under ``recaps/``:

  YYYY-MM-DD.json        daily recap (existing v6 shape, last ~7 days hot)
  weekly_YYYY-Www.json   weekly digest — threaded rollup of a week's dailies
  monthly_YYYY-MM.json   monthly digest — same shape, looser threading
  open_threads.json      hot running list of unresolved story arcs

The LLM-driven *generation* of weekly/monthly digests and thread labels lives
in curator.py / podcast.py; this module only loads, saves, and applies the
deterministic bookkeeping (date math, dormancy, upserts).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

# Thread is considered dormant after this many days with no new episode.
DORMANT_AFTER_DAYS = 14

# Valid open-thread statuses.
THREAD_STATUSES = {"developing", "closed", "dormant"}


# ─── KEY HELPERS ─────────────────────────────────────────────────────────────

def iso_week_key(d: dt.date) -> str:
    """Return the ISO week file key for a date, e.g. ``2026-W25``."""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def month_key(d: dt.date) -> str:
    """Return the month file key for a date, e.g. ``2026-06``."""
    return f"{d.year}-{d.month:02d}"


def iso_week_bounds(d: dt.date) -> tuple[dt.date, dt.date]:
    """Return (Monday, Sunday) of the ISO week containing ``d``."""
    monday = d - dt.timedelta(days=d.isoweekday() - 1)
    return monday, monday + dt.timedelta(days=6)


def slugify_thread(label: str) -> str:
    """Derive a stable thread_id slug from a human label.

    'France 2026 presidential election' -> 'france-2026-presidential-election'
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return slug or "thread"


# ─── DAILY RECAPS ────────────────────────────────────────────────────────────

def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def load_daily_recap(recaps_dir: Path, d: dt.date) -> dict[str, Any] | None:
    """Load a single day's recap, or None if missing/unparseable."""
    data = _read_json(recaps_dir / f"{d.isoformat()}.json")
    return data if isinstance(data, dict) else None


def load_daily_recaps(recaps_dir: Path, end_date: dt.date,
                      lookback_days: int = 7) -> list[dict[str, Any]]:
    """Load up to ``lookback_days`` daily recaps ending the day before ``end_date``.

    Mirrors podcast.load_recent_recaps (skips ``end_date`` itself, looks back),
    returns oldest-first so consumers see chronological order.
    """
    out: list[dict[str, Any]] = []
    for delta in range(1, lookback_days + 1):
        rec = load_daily_recap(recaps_dir, end_date - dt.timedelta(days=delta))
        if rec is not None:
            out.append(rec)
    out.sort(key=lambda r: r.get("date", ""))
    return out


def load_daily_recaps_in_range(recaps_dir: Path, start: dt.date,
                               end: dt.date) -> list[dict[str, Any]]:
    """Load every daily recap with date in [start, end], inclusive, oldest-first."""
    out: list[dict[str, Any]] = []
    d = start
    while d <= end:
        rec = load_daily_recap(recaps_dir, d)
        if rec is not None:
            out.append(rec)
        d += dt.timedelta(days=1)
    out.sort(key=lambda r: r.get("date", ""))
    return out


def daily_recap_dates(recaps_dir: Path) -> list[dt.date]:
    """All dates for which a daily recap file exists, sorted oldest-first."""
    dates: list[dt.date] = []
    if not recaps_dir.exists():
        return dates
    for p in recaps_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"):
        try:
            dates.append(dt.date.fromisoformat(p.stem))
        except ValueError:
            continue
    dates.sort()
    return dates


def prune_daily_recaps(recaps_dir: Path, today: dt.date,
                       keep_days: int = 14) -> list[dt.date]:
    """Delete daily recaps older than ``keep_days`` days before ``today``.

    Conservative default of 14 keeps the 7-day dedupe window safe with buffer.
    Returns the list of dates pruned. Weekly/monthly digests preserve the long
    arc, so the daily files are safe to roll off once digested.
    """
    cutoff = today - dt.timedelta(days=keep_days)
    pruned: list[dt.date] = []
    for d in daily_recap_dates(recaps_dir):
        if d < cutoff:
            try:
                (recaps_dir / f"{d.isoformat()}.json").unlink()
                pruned.append(d)
            except OSError:
                pass
    return pruned


# ─── WEEKLY / MONTHLY DIGESTS ────────────────────────────────────────────────

def weekly_digest_path(recaps_dir: Path, iso_week: str) -> Path:
    return recaps_dir / f"weekly_{iso_week}.json"


def monthly_digest_path(recaps_dir: Path, mkey: str) -> Path:
    return recaps_dir / f"monthly_{mkey}.json"


def weekly_digest_exists(recaps_dir: Path, iso_week: str) -> bool:
    return weekly_digest_path(recaps_dir, iso_week).exists()


def monthly_digest_exists(recaps_dir: Path, mkey: str) -> bool:
    return monthly_digest_path(recaps_dir, mkey).exists()


def load_weekly_digest(recaps_dir: Path, iso_week: str) -> dict[str, Any] | None:
    data = _read_json(weekly_digest_path(recaps_dir, iso_week))
    return data if isinstance(data, dict) else None


def load_monthly_digest(recaps_dir: Path, mkey: str) -> dict[str, Any] | None:
    data = _read_json(monthly_digest_path(recaps_dir, mkey))
    return data if isinstance(data, dict) else None


def save_weekly_digest(recaps_dir: Path, digest: dict[str, Any]) -> Path:
    recaps_dir.mkdir(parents=True, exist_ok=True)
    path = weekly_digest_path(recaps_dir, digest["iso_week"])
    path.write_text(json.dumps(digest, indent=2, ensure_ascii=False))
    return path


def save_monthly_digest(recaps_dir: Path, digest: dict[str, Any]) -> Path:
    recaps_dir.mkdir(parents=True, exist_ok=True)
    path = monthly_digest_path(recaps_dir, digest["month"])
    path.write_text(json.dumps(digest, indent=2, ensure_ascii=False))
    return path


def latest_weekly_digest(recaps_dir: Path,
                         before: dt.date | None = None) -> dict[str, Any] | None:
    """Return the most recent weekly digest, optionally one ending on/before a date."""
    best: tuple[str, dict[str, Any]] | None = None
    if not recaps_dir.exists():
        return None
    for p in recaps_dir.glob("weekly_*.json"):
        data = _read_json(p)
        if not isinstance(data, dict):
            continue
        end = data.get("end_date", "")
        if before is not None and end and end > before.isoformat():
            continue
        key = data.get("iso_week", p.stem)
        if best is None or key > best[0]:
            best = (key, data)
    return best[1] if best else None


# ─── OPEN THREADS ────────────────────────────────────────────────────────────

def open_threads_path(recaps_dir: Path) -> Path:
    return recaps_dir / "open_threads.json"


def load_open_threads(recaps_dir: Path) -> dict[str, Any]:
    """Load open_threads.json, returning an empty skeleton if absent/broken."""
    data = _read_json(open_threads_path(recaps_dir))
    if isinstance(data, dict) and isinstance(data.get("threads"), list):
        return data
    return {"updated": "", "threads": []}


def save_open_threads(recaps_dir: Path, data: dict[str, Any]) -> Path:
    recaps_dir.mkdir(parents=True, exist_ok=True)
    path = open_threads_path(recaps_dir)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def active_open_threads(open_threads: dict[str, Any]) -> list[dict[str, Any]]:
    """Threads the curator should still consider — everything not closed."""
    return [t for t in open_threads.get("threads", [])
            if t.get("status") != "closed"]


def upsert_thread(open_threads: dict[str, Any], *, thread_id: str, label: str,
                  episode_date: str, hooks: list[str] | None = None,
                  next_milestone: str | None = None,
                  status: str = "developing") -> dict[str, Any]:
    """Add or update a thread in-place and return the open_threads dict.

    Merges hooks (dedup, keep most recent ~6), advances last_episode, and never
    downgrades a manually-closed thread unless explicitly told to.
    """
    if status not in THREAD_STATUSES:
        status = "developing"
    threads = open_threads.setdefault("threads", [])
    existing = next((t for t in threads if t.get("thread_id") == thread_id), None)
    hooks = [h for h in (hooks or []) if h]
    if existing is None:
        threads.append({
            "thread_id": thread_id,
            "label": label,
            "first_episode": episode_date,
            "last_episode": episode_date,
            "next_milestone": next_milestone or "",
            "hooks": hooks[-6:],
            "status": status,
        })
    else:
        existing["label"] = label or existing.get("label", "")
        existing["last_episode"] = episode_date
        existing.setdefault("first_episode", episode_date)
        if next_milestone:
            existing["next_milestone"] = next_milestone
        merged = list(dict.fromkeys((existing.get("hooks") or []) + hooks))
        existing["hooks"] = merged[-6:]
        existing["status"] = status
    return open_threads


def close_thread(open_threads: dict[str, Any], thread_id: str) -> bool:
    """Mark a thread closed. Returns True if a matching thread was found."""
    for t in open_threads.get("threads", []):
        if t.get("thread_id") == thread_id:
            t["status"] = "closed"
            return True
    return False


def mark_dormant_threads(open_threads: dict[str, Any], today: dt.date,
                         dormant_after_days: int = DORMANT_AFTER_DAYS) -> int:
    """Flip ``developing`` threads with no recent episode to ``dormant``.

    Closed threads are left untouched. Returns the number newly marked dormant.
    """
    n = 0
    for t in open_threads.get("threads", []):
        if t.get("status") != "developing":
            continue
        last = t.get("last_episode", "")
        try:
            last_d = dt.date.fromisoformat(last)
        except (ValueError, TypeError):
            continue
        if (today - last_d).days >= dormant_after_days:
            t["status"] = "dormant"
            n += 1
    return n
