"""raw_sources.py — aggregate real, dated content from public feeds (no LLM).

The old pipeline asked Sonar to "find today's tech / science / culture / essay
news," which hallucinates and resurfaces stale items. This module instead pulls
REAL, timestamped items straight from primary feeds — outlet RSS, Hacker News,
Substack, and Reddit — and hands them to the OpenAI curator to judge what's worth
keeping. It complements news_feeds.py (which owns the hard-news wire) by covering
the show's topical + discussion + essay beats.

Same output shape as podcast.gather_research(): ``{beat: [item, ...]}`` where each
item is research-shaped (headline / detail / url / source / published / beat).

Parsing reuses news_feeds' stdlib RSS/Atom parser; Hacker News uses the keyless
Algolia JSON API. The network fetch is dependency-injected and best-effort — an
unreachable or bot-blocked feed (Reddit sometimes 403s from CI) is skipped, never
fatal.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

import news_feeds
from news_feeds import _norm_title, _parse_date  # reuse the tested primitives

FetchFn = Callable[[str], str]


def _reddit_rss(sub: str) -> str:
    # RSS is more permissive than the .json endpoint, which hard-blocks CI IPs.
    return f"https://www.reddit.com/r/{sub}/top/.rss?t=day"


# beat -> list of RSS/Atom feed URLs. Beat names match the show's research beats so
# they flow through the existing curator / news-floor routing unchanged.
RSS_FEEDS: dict[str, list[str]] = {
    "tech_news": [
        "https://www.theverge.com/rss/index.xml",
        "http://feeds.arstechnica.com/arstechnica/index",
        "https://www.theguardian.com/technology/rss",
        "https://techcrunch.com/feed/",
    ],
    "ai_frontier": [
        "https://www.technologyreview.com/feed/",
        "https://news.google.com/rss/search?q=artificial%20intelligence%20when:2d&hl=en-US&gl=US&ceid=US:en",
    ],
    "science": [
        "https://www.sciencedaily.com/rss/top/science.xml",
        "https://www.quantamagazine.org/feed/",
        "https://www.theguardian.com/science/rss",
        "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    ],
    "biotech": [
        "https://www.statnews.com/feed/",
        "https://www.sciencedaily.com/rss/top/health.xml",
    ],
    "economics": [
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "https://www.theguardian.com/business/economics/rss",
    ],
    "culture_buzz": [
        "https://www.theguardian.com/culture/rss",
        "https://variety.com/feed/",
        "https://www.npr.org/rss/rss.php?id=1008",          # NPR Arts
    ],
    "sports": [
        "https://www.espn.com/espn/rss/news",
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://www.theguardian.com/sport/rss",
    ],
    "substack": [                                            # essays / analysis
        "https://www.noahpinion.blog/feed",
        "https://www.slowboring.com/feed",
        "https://www.astralcodexten.com/feed",
        "https://www.construction-physics.com/feed",
    ],
    "reddit": [                                              # discussion (best-effort)
        _reddit_rss("worldnews"),
        _reddit_rss("technology"),
        _reddit_rss("science"),
        _reddit_rss("Economics"),
    ],
}

# Hacker News via the keyless Algolia API — front-page-quality stories only.
HN_SEARCH = "https://hn.algolia.com/api/v1/search?tags=front_page"
HN_BEAT = "tech_news"
HN_MIN_POINTS = 50


def parse_hn(json_text: str) -> list[dict[str, Any]]:
    """Parse a Hacker News Algolia response into feed-item dicts."""
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []
    out: list[dict[str, Any]] = []
    for hit in data.get("hits", []) or []:
        title = (hit.get("title") or "").strip()
        url = (hit.get("url") or "").strip()
        if not title:
            continue
        if not url:  # Ask HN / text posts -> link to the discussion
            url = f"https://news.ycombinator.com/item?id={hit.get('objectID','')}"
        pts = hit.get("points") or 0
        ncomments = hit.get("num_comments") or 0
        out.append({
            "title": title,
            "link": url,
            "summary": f"{pts} points, {ncomments} comments on Hacker News",
            "published": _parse_date(hit.get("created_at")),
            "source": "Hacker News",
            "points": pts,
        })
    return out


def _item_from_entry(e: dict[str, Any], beat: str, pub: dt.datetime | None,
                     now: dt.datetime) -> dict[str, Any]:
    return {
        "headline": e["title"][:200],
        "detail": e["summary"][:280],
        "angle": "",
        "source": (e.get("source") or "")[:80],
        "url": e["link"],
        "published": pub.date().isoformat() if pub else "",
        "buzz": f"raw feed: {beat}",
        "beat": beat,
        "_sort": pub or now,
    }


def gather_raw_sources(fetch_fn: FetchFn, *, today: dt.date | None = None,
                       lookback_hours: int = 72, per_beat_cap: int = 12,
                       log: Callable[[str], None] | None = None
                       ) -> dict[str, list[dict[str, Any]]]:
    """Fetch + parse all raw feeds, keep recent items, return research-shaped beats.

    Best-effort: unreachable/garbled/blocked feeds are skipped. Items older than
    ``lookback_hours`` (by their real publish date) are dropped; undated items are
    kept (many feeds omit dates) so a beat is never needlessly emptied.
    """
    now = dt.datetime.now(dt.timezone.utc) if today is None else \
        dt.datetime.combine(today, dt.time(12, tzinfo=dt.timezone.utc))
    cutoff = now - dt.timedelta(hours=lookback_hours)
    results: dict[str, list[dict[str, Any]]] = {}

    # 1) RSS/Atom beats
    for beat, urls in RSS_FEEDS.items():
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for url in urls:
            try:
                entries = news_feeds.parse_feed(fetch_fn(url))
            except Exception as e:  # noqa: BLE001
                if log:
                    log(f"[raw] feed failed ({beat}): {url.split('?')[0]} — {type(e).__name__}")
                continue
            for e in entries:
                pub = e["published"]
                if pub is not None and pub < cutoff:
                    continue
                key = _norm_title(e["title"])[:80]
                if not key or key in seen:
                    continue
                seen.add(key)
                items.append(_item_from_entry(e, beat, pub, now))
        items.sort(key=lambda it: it["_sort"], reverse=True)
        for it in items:
            it.pop("_sort", None)
        results[beat] = items[:per_beat_cap]
        if log:
            log(f"[raw] {beat}: {len(results[beat])} items")

    # 2) Hacker News (folded into the tech beat)
    try:
        hits = [h for h in parse_hn(fetch_fn(HN_SEARCH)) if (h.get("points") or 0) >= HN_MIN_POINTS]
    except Exception as e:  # noqa: BLE001
        hits = []
        if log:
            log(f"[raw] hacker news failed — {type(e).__name__}")
    if hits:
        bucket = results.setdefault(HN_BEAT, [])
        seen_tech = {_norm_title(it["headline"])[:80] for it in bucket}
        hn_items: list[dict[str, Any]] = []
        for h in hits:
            pub = h["published"]
            if pub is not None and pub < cutoff:
                continue
            key = _norm_title(h["title"])[:80]
            if not key or key in seen_tech:
                continue
            seen_tech.add(key)
            it = _item_from_entry(h, HN_BEAT, pub, now)
            it.pop("_sort", None)
            it["buzz"] = h["summary"]
            hn_items.append(it)
        bucket.extend(hn_items[:per_beat_cap])
        if log:
            log(f"[raw] hacker_news: +{len(hn_items[:per_beat_cap])} into {HN_BEAT}")

    total = sum(len(v) for v in results.values())
    if log:
        log(f"[raw] {total} items across {len(results)} beats")
    return results
