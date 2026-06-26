"""curator.py — two-pass curation + digest generation (v7, Tier 2).

Pass 1 (routing) stays in podcast.curate_topics — it already assigns research
items to segments and drops junk. This module adds:

  * Pass 2 (memory review): label each Pass-1 pick as
    new / update / repeat / thread_continuation against recent memory, drop
    repeats, and carry update_note + relates_to_thread_id forward so the
    existing programmatic_dedupe keeps genuine updates.
  * Weekly / monthly digest generation from daily recaps.

LLM access is dependency-injected as ``chat_fn`` (same signature as
podcast.pplx_chat) so the module imports and unit-tests without API keys. The
pure pieces — JSON parsing and label application — are exercised directly.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

# chat_fn(prompt, *, model, max_tokens, system, temperature) -> str
ChatFn = Callable[..., str]

VALID_STATUSES = {"new", "update", "repeat", "thread_continuation"}

# Sonar-pro hard cap.
_MAX_TOKENS = 8000


# ─── JSON helpers ────────────────────────────────────────────────────────────

def _strip_to_json(raw: str, opener: str, closer: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    s, e = cleaned.find(opener), cleaned.rfind(closer)
    if s != -1 and e != -1 and e > s:
        cleaned = cleaned[s:e + 1]
    return cleaned


def parse_labels(raw: str) -> dict[str, dict[str, Any]]:
    """Parse the memory-review response into ``{headline_lower -> label dict}``.

    Tolerant of fences / surrounding prose. Returns {} on failure so the caller
    can no-op safely.
    """
    cleaned = _strip_to_json(raw, "{", "}")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in labels:
        if not isinstance(item, dict):
            continue
        h = (item.get("headline") or "").strip().lower()[:200]
        status = (item.get("status") or "").strip().lower()
        if not h or status not in VALID_STATUSES:
            continue
        out[h] = {
            "status": status,
            "relates_to_thread_id": (item.get("relates_to_thread_id") or "").strip(),
            "update_note": (item.get("update_note") or "").strip(),
        }
    return out


def _match_label(headline: str, labels: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    h = (headline or "").strip().lower()[:200]
    if not h:
        return None
    if h in labels:
        return labels[h]
    short = h[:60]
    for k, v in labels.items():
        # Match if either headline is a prefix/substring of the other — the model
        # sometimes truncates or pads the headline it echoes back. Containment is
        # length-guarded to avoid spurious cross-topic matches on short fragments.
        if k.startswith(short) or short.startswith(k[:60]):
            return v
        if len(k) >= 15 and (k in short or short in k):
            return v
    return None


def apply_labels(curated: dict[str, Any],
                 labels: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Attach status labels to each topic and drop ``repeat`` items. Pure.

    For ``update`` / ``thread_continuation`` we set ``update_note`` (synthesizing
    one if the model omitted it) and ``relates_to_thread_id`` so the downstream
    programmatic_dedupe treats them as genuine updates rather than dropping them.
    Returns (curated, log_lines).
    """
    log: list[str] = []
    segs_in = curated.get("segments", {})
    segs_out: dict[str, list[dict[str, Any]]] = {}
    counts = {s: 0 for s in VALID_STATUSES}
    counts["unlabeled"] = 0

    for seg_name, items in segs_in.items():
        kept: list[dict[str, Any]] = []
        for it in (items or []):
            label = _match_label(it.get("headline", ""), labels)
            if label is None:
                counts["unlabeled"] += 1
                it.setdefault("status", "new")
                kept.append(it)
                continue
            status = label["status"]
            counts[status] += 1
            if status == "repeat":
                log.append(f"  [pass2 drop repeat] {seg_name}: "
                           f"'{it.get('headline', '')[:70]}'")
                continue
            it["status"] = status
            if label.get("relates_to_thread_id"):
                it["relates_to_thread_id"] = label["relates_to_thread_id"]
            if status in ("update", "thread_continuation"):
                # Guarantee an update_note so programmatic_dedupe keeps it.
                note = label.get("update_note") or "continuing coverage of an ongoing story"
                it["update_note"] = note
            kept.append(it)
        segs_out[seg_name] = kept

    curated["segments"] = segs_out
    log.insert(0, "[pass2] labels — " + ", ".join(
        f"{k}:{v}" for k, v in counts.items() if v))
    return curated, log


# ─── PASS 2 — MEMORY REVIEW ──────────────────────────────────────────────────

MEMORY_REVIEW_SYSTEM_PROMPT = """You are the MEMORY EDITOR for a daily podcast briefing.
You do NOT pick or write topics. You ONLY judge each already-selected topic against what
the show has recently covered, so we don't bore the listener with repeats.

For EACH candidate topic you receive, decide exactly one status:
  "new"                  — a story we have NOT covered in recent episodes.
  "update"               — a story we covered recently that has a CONCRETE new development
                           (new data, ruling, actor, number, outcome). Provide "update_note"
                           naming the new fact, and "relates_to_thread_id" if it maps to an
                           open thread.
  "thread_continuation"  — part of a running narrative we are deliberately tracking across
                           episodes (e.g. an election, a war, an ongoing trial). Provide
                           "relates_to_thread_id".
  "repeat"               — the SAME story with NO real new development. We will DROP these.

Use the RECENT EPISODES, OPEN THREADS, and WEEKLY DIGEST blocks as your memory. Be strict:
when in doubt between "update" and "repeat", and there is no concrete new fact, choose "repeat".

OUTPUT — STRICT JSON ONLY, no prose, no fences:
{
  "labels": [
    {"headline": "<exact headline copied verbatim from input>",
     "status": "new|update|repeat|thread_continuation",
     "relates_to_thread_id": "<thread_id or omit>",
     "update_note": "<one sentence naming the new fact, for 'update' only>"}
  ]
}
The "headline" MUST match an input headline verbatim. Label EVERY topic you are given."""


def _compact_picks(curated: dict[str, Any]) -> list[dict[str, str]]:
    picks: list[dict[str, str]] = []
    for seg, items in curated.get("segments", {}).items():
        for it in (items or []):
            picks.append({
                "segment": seg,
                "headline": (it.get("headline") or "")[:200],
                "detail": (it.get("detail") or "")[:200],
            })
    return picks


def _compact_open_threads(open_threads: dict[str, Any]) -> str:
    threads = [t for t in open_threads.get("threads", []) if t.get("status") != "closed"]
    if not threads:
        return "(no open threads)"
    lines = []
    for t in threads:
        hooks = "; ".join(t.get("hooks", []) or [])
        lines.append(f"- [{t.get('thread_id')}] {t.get('label')} "
                     f"(status={t.get('status')}, last={t.get('last_episode')})"
                     + (f" hooks: {hooks}" if hooks else ""))
    return "\n".join(lines)


def _compact_weekly(weekly: dict[str, Any] | None) -> str:
    if not weekly:
        return "(no weekly digest yet)"
    lines = [f"Week {weekly.get('iso_week', '?')} "
             f"({weekly.get('start_date', '?')}..{weekly.get('end_date', '?')}):"]
    for th in weekly.get("threads", []) or []:
        lines.append(f"- {th.get('label')} [{th.get('status', '?')}]: "
                     f"{(th.get('summary') or '')[:200]}")
    return "\n".join(lines)


def memory_review(curated: dict[str, Any], *,
                  recaps_block: str,
                  open_threads: dict[str, Any],
                  weekly_digest: dict[str, Any] | None,
                  date_str: str,
                  chat_fn: ChatFn,
                  model: str,
                  preamble: str = "",
                  log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run Pass 2 and return the curated dict with status labels applied.

    Non-fatal: on any error the input ``curated`` is returned unchanged (every
    topic implicitly treated as ``new``), so the pipeline never dies here.
    """
    def _log(m: str) -> None:
        if log:
            log(m)

    picks = _compact_picks(curated)
    if not picks:
        return curated

    user = (
        f"{preamble}\n"
        f"Today: {date_str}\n\n"
        f"RECENT EPISODES (daily recaps):\n{recaps_block}\n\n"
        f"OPEN THREADS:\n{_compact_open_threads(open_threads)}\n\n"
        f"WEEKLY DIGEST:\n{_compact_weekly(weekly_digest)}\n\n"
        f"CANDIDATE TOPICS TO LABEL ({len(picks)}):\n"
        f"{json.dumps(picks, ensure_ascii=False)}\n\n"
        "Label every topic now. Output the JSON object only."
    )
    try:
        raw = chat_fn(user, model=model, max_tokens=_MAX_TOKENS,
                      system=MEMORY_REVIEW_SYSTEM_PROMPT, temperature=0.2)
    except Exception as e:  # noqa: BLE001 — non-fatal by design
        _log(f"[pass2] memory review call failed (non-fatal): {e}")
        return curated

    labels = parse_labels(raw)
    if not labels:
        _log("[pass2] no usable labels parsed; treating all topics as new")
        return curated

    curated, lines = apply_labels(curated, labels)
    for ln in lines:
        _log(ln)
    return curated


# ─── WEEKLY / MONTHLY DIGESTS ────────────────────────────────────────────────

DIGEST_SYSTEM_PROMPT = """You roll up a set of daily podcast recaps into a threaded digest.
Group stories that belong to the SAME ongoing narrative into "threads". A thread is a story
that recurs or develops across days (an election, a war, a company saga, a sports tournament).
One-off stories that appeared on a single day go in "single_day_stories". Stories that clearly
resolved go in "closed_stories".

OUTPUT — STRICT JSON ONLY, no prose, no fences:
{
  "threads": [
    {"thread_id": "kebab-case-id", "label": "Human readable label",
     "entities": ["..."], "first_seen": "YYYY-MM-DD", "last_seen": "YYYY-MM-DD",
     "episodes": ["YYYY-MM-DD", ...],
     "summary": "Three lines max: what happened across the period, where it stands now, what's unresolved.",
     "status": "developing|closed|dormant"}
  ],
  "closed_stories": ["short phrase", ...],
  "single_day_stories": ["short phrase", ...]
}"""


def _recaps_as_prompt(recaps: list[dict[str, Any]]) -> str:
    parts = []
    for r in recaps:
        topics = "; ".join(r.get("topics", []) or [])
        ents = ", ".join((r.get("entities", []) or [])[:40])
        parts.append(f"--- {r.get('date', '?')} ---\nTopics: {topics}\nEntities: {ents}")
    return "\n".join(parts)


def _parse_digest(raw: str) -> dict[str, Any]:
    cleaned = _strip_to_json(raw, "{", "}")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"threads": [], "closed_stories": [], "single_day_stories": []}
    if not isinstance(data, dict):
        return {"threads": [], "closed_stories": [], "single_day_stories": []}
    data.setdefault("threads", [])
    data.setdefault("closed_stories", [])
    data.setdefault("single_day_stories", [])
    return data


def generate_weekly_digest(recaps: list[dict[str, Any]], *, iso_week: str,
                           start_date: str, end_date: str,
                           chat_fn: ChatFn, model: str,
                           min_thread_episodes: int = 1) -> dict[str, Any]:
    """LLM-roll-up of a week's daily recaps into the threaded weekly-digest shape."""
    user = (
        f"Roll up these daily recaps for ISO week {iso_week} "
        f"({start_date}..{end_date}). A thread must appear on at least "
        f"{min_thread_episodes} day(s).\n\n{_recaps_as_prompt(recaps)}\n\n"
        "Output the JSON object only."
    )
    raw = chat_fn(user, model=model, max_tokens=_MAX_TOKENS,
                  system=DIGEST_SYSTEM_PROMPT, temperature=0.2)
    digest = _parse_digest(raw)
    digest.update({"iso_week": iso_week, "start_date": start_date, "end_date": end_date})
    return digest


def generate_monthly_digest(recaps: list[dict[str, Any]], *, month: str,
                            chat_fn: ChatFn, model: str,
                            min_thread_episodes: int = 4) -> dict[str, Any]:
    """LLM-roll-up of a month's daily recaps; looser threading (4+ days default)."""
    user = (
        f"Roll up these daily recaps for the month {month}. Only create a thread "
        f"for narratives that appeared on at least {min_thread_episodes} different "
        f"days that month; everything else is a single_day_story.\n\n"
        f"{_recaps_as_prompt(recaps)}\n\nOutput the JSON object only."
    )
    raw = chat_fn(user, model=model, max_tokens=_MAX_TOKENS,
                  system=DIGEST_SYSTEM_PROMPT, temperature=0.2)
    digest = _parse_digest(raw)
    digest["month"] = month
    return digest
