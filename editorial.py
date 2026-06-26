"""editorial.py — Tier 3 editorial intelligence (v7).

Pure, network-free helpers that give the writer story-arc awareness:

  * detect_running_threads — entities seen in 3+ of the last 7 episodes
  * find_prior_narration   — the actual lines we used last time on a story
  * prompt builders        — render both into writer-prompt blocks

The daily recap gained a ``topic_details`` list in v7 (each item carries its
``segment``, ``entities`` and the real ``narration`` lines). These functions
degrade gracefully on old recaps that lack that field.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

# Generic geo/time/agency tokens that would otherwise dominate the running-thread
# signal (every episode mentions "U.S." or "China"). Mirrors the dedupe stoplist.
_STOP_ENTITIES = {
    "china", "chinese", "u.s.", "us", "united", "states", "america", "american",
    "russia", "russian", "iran", "iranian", "israel", "israeli", "european",
    "europe", "uk", "u.k.", "britain", "british", "india", "indian", "germany",
    "german", "france", "french", "japan", "japanese", "korea", "korean",
    "ukraine", "ukrainian", "gaza", "lebanon", "fed", "ecb", "imf", "un",
    "nato", "eu", "g7", "opec", "washington", "beijing", "moscow", "kyiv",
    "tehran", "brussels", "paris", "london", "tokyo", "ai",
}

# Max running-thread callouts surfaced to the writer per episode. Keeping this
# low is deliberate — more than a few becomes a verbal tic.
MAX_RUNNING_THREADS = 3
# An entity must appear in at least this many distinct episodes to qualify.
MIN_THREAD_EPISODES = 3


def entity_set(text: str) -> set[str]:
    """Cheap proper-noun proxy: capitalized tokens minus generic geo/time words."""
    if not text:
        return set()
    toks = re.findall(r"\b[A-Z][A-Za-z0-9'.&-]{2,}\b", text)
    return {t.lower() for t in toks if t.lower() not in _STOP_ENTITIES}


def _norm_entity(e: str) -> str:
    return (e or "").strip().lower()


def _relative_day(date_str: str, today: dt.date) -> str:
    """Human relative-day label: 'today', 'yesterday', weekday abbr, or ISO date."""
    try:
        d = dt.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return date_str or "?"
    days = (today - d).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return d.strftime("%a")  # Mon, Tue, ...
    return date_str


# ─── RUNNING THREADS ─────────────────────────────────────────────────────────

def detect_running_threads(recaps: list[dict[str, Any]], *,
                           min_episodes: int = MIN_THREAD_EPISODES,
                           max_threads: int = MAX_RUNNING_THREADS) -> list[dict[str, Any]]:
    """Entities appearing in ``min_episodes``+ distinct episodes of ``recaps``.

    Returns up to ``max_threads`` dicts ``{entity, display, episodes, count}``
    sorted by episode count (desc), then most-recent appearance.
    """
    # entity -> {date -> display_form}, keeping a representative display casing
    seen: dict[str, dict[str, str]] = {}
    for r in recaps:
        date = r.get("date", "")
        ents = r.get("entities") or []
        for e in ents:
            key = _norm_entity(e)
            if not key or key in _STOP_ENTITIES:
                continue
            seen.setdefault(key, {}).setdefault(date, e)

    threads: list[dict[str, Any]] = []
    for key, by_date in seen.items():
        if len(by_date) < min_episodes:
            continue
        episodes = sorted(by_date.keys())
        display = by_date[episodes[-1]]  # most-recent casing
        threads.append({"entity": key, "display": display,
                        "episodes": episodes, "count": len(episodes)})

    threads.sort(key=lambda t: (t["count"], t["episodes"][-1]), reverse=True)
    return threads[:max_threads]


def format_running_threads_block(threads: list[dict[str, Any]], today: dt.date) -> str:
    """Render running threads into a writer-prompt instruction block, or ''."""
    if not threads:
        return ""
    lines = []
    for t in threads:
        days = ", ".join(_relative_day(d, today) for d in t["episodes"])
        lines.append(f"- {t['display']} (mentioned {days})")
    return (
        "RUNNING THREADS THIS WEEK\n"
        "================\n"
        "These entities have come up repeatedly this week. If THIS segment touches "
        "one, acknowledge the arc once — e.g. \"OpenAI's been everywhere this week — "
        "here's the next chapter\" — without rehashing what we already said. Do NOT "
        "force a callout for a thread this segment doesn't naturally cover.\n"
        + "\n".join(lines)
    )


# ─── PRIOR NARRATION (timeline awareness) ────────────────────────────────────

def _topic_details(recap: dict[str, Any]) -> list[dict[str, Any]]:
    td = recap.get("topic_details")
    return td if isinstance(td, list) else []


def find_prior_narration(recaps: list[dict[str, Any]], *,
                         entities: set[str], today: dt.date,
                         min_overlap: int = 2, max_lines: int = 4
                         ) -> dict[str, Any] | None:
    """Find the most recent prior episode that covered a story with these entities.

    Searches ``topic_details`` newest-first; returns the best entity-overlap match
    as ``{date, days_ago, topic, segment, narration}`` (narration trimmed to
    ``max_lines``), or None. ``min_overlap`` is the minimum shared-entity count,
    but a single distinctive shared entity also matches when the topic has few.
    """
    norm_target = {_norm_entity(e) for e in entities if _norm_entity(e) not in _STOP_ENTITIES}
    if not norm_target:
        return None

    best: tuple[int, dict[str, Any]] | None = None
    for r in sorted(recaps, key=lambda x: x.get("date", ""), reverse=True):
        for td in _topic_details(r):
            cand_ents = {_norm_entity(e) for e in (td.get("entities") or [])}
            cand_ents -= _STOP_ENTITIES
            if not cand_ents:
                continue
            overlap = len(norm_target & cand_ents)
            threshold = min(min_overlap, len(norm_target))
            if overlap < threshold:
                continue
            narration = [ln for ln in (td.get("narration") or []) if ln][:max_lines]
            if not narration:
                continue
            try:
                days_ago = (today - dt.date.fromisoformat(r.get("date", ""))).days
            except (ValueError, TypeError):
                days_ago = None
            cand = {"date": r.get("date", ""), "days_ago": days_ago,
                    "topic": td.get("topic", ""), "segment": td.get("segment", ""),
                    "narration": narration, "_overlap": overlap}
            if best is None or overlap > best[0]:
                best = (overlap, cand)
        if best is not None:
            # newest-first: first recap that yields a match wins on recency
            break
    if best is None:
        return None
    out = best[1]
    out.pop("_overlap", None)
    return out


def _ago_phrase(days_ago: int | None) -> str:
    if days_ago is None:
        return "recently"
    if days_ago <= 0:
        return "earlier today"
    if days_ago == 1:
        return "yesterday"
    return f"{days_ago} days ago"


def format_timeline_block(updates: list[dict[str, Any]]) -> str:
    """Render per-topic prior-narration lookbacks into a writer-prompt block.

    ``updates`` is a list of ``{topic, update_note, prior}`` where ``prior`` is a
    find_prior_narration result (or None). Topics with no prior match are skipped.
    """
    blocks: list[str] = []
    for u in updates:
        prior = u.get("prior")
        if not prior:
            continue
        ago = _ago_phrase(prior.get("days_ago"))
        quoted = " / ".join(f"“{ln}”" for ln in prior.get("narration", []))
        note = (u.get("update_note") or "").strip()
        blocks.append(
            f"- TOPIC: {u.get('topic', '')}\n"
            f"  We covered this {ago} with: {quoted}\n"
            + (f"  What's new: {note}\n" if note else "")
            + "  Write the follow-up as picking up where we left off. Do NOT "
              "re-introduce who the players are. Open by signaling the gap "
              "(e.g. \"Back to the story we flagged "
            + f"{ago}…\")."
        )
    if not blocks:
        return ""
    return (
        "STORY TIMELINE — CONTINUING COVERAGE\n"
        "================\n"
        "Some topics below are follow-ups to stories we already told. For each, "
        "build on the prior framing instead of starting fresh:\n"
        + "\n".join(blocks)
    )
