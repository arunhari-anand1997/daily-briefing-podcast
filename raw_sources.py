"""raw_sources.py — aggregate real, dated content from public feeds (no LLM).

The old pipeline asked Sonar to "find today's tech / science / culture / essay
news," which hallucinates and resurfaces stale items. This module instead pulls
REAL, timestamped items straight from primary feeds — outlet RSS, Hacker News,
Substack, Reddit, and podcast feeds — and hands them to the OpenAI curator to
judge what's worth keeping. It complements news_feeds.py (which owns the hard-news
wire) by covering the show's topical / discussion / essay / entertainment beats.

Same output shape as podcast.gather_research(): ``{beat: [item, ...]}`` where each
item is research-shaped (headline / detail / url / source / published / beat).

Beat names match the show's research beats so items flow through the existing
curator / news-floor / content-floor routing unchanged. Parsing reuses news_feeds'
stdlib RSS/Atom parser; Hacker News uses the keyless Algolia JSON API. Fetches run
concurrently and are best-effort — an unreachable or bot-blocked feed (Reddit often
403s from CI) is skipped, never fatal.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
from typing import Any, Callable

import news_feeds
from news_feeds import _gnews, _norm_title, _parse_date  # reuse tested primitives

FetchFn = Callable[[str], str]


def _reddit_rss(sub: str) -> str:
    # old.reddit's RSS is less aggressively bot-blocked than www; still best-effort.
    return f"https://old.reddit.com/r/{sub}/top/.rss?t=day"


# beat -> list of RSS/Atom feed URLs. Beats map to the show's segments by content.
RSS_FEEDS: dict[str, list[str]] = {
    # ── World / global hard news (supplements the news_feeds wire) ──────────────
    "geopolitics": [
        "https://www.theguardian.com/world/rss",
        "https://www.france24.com/en/rss",
        "https://rss.dw.com/rdf/rss-en-world",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        _gnews("Reuters OR AP world news"),
    ],
    # ── US news & politics ─────────────────────────────────────────────────────
    "us_politics": [
        "https://www.politico.com/rss/politicopicks.xml",
        "https://thehill.com/news/feed/",
        "https://feeds.npr.org/1014/rss.xml",               # NPR Politics
        "https://www.axios.com/feeds/feed.rss",
        _gnews("US politics Congress White House Supreme Court"),
    ],
    # ── Economy / business / markets ───────────────────────────────────────────
    "economics": [
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",   # MarketWatch
        "https://www.theguardian.com/business/economics/rss",
        "https://feeds.npr.org/1006/rss.xml",               # NPR Business
        _gnews("economy inflation Federal Reserve markets"),
    ],
    # ── Tech ───────────────────────────────────────────────────────────────────
    "tech_news": [
        "https://www.theverge.com/rss/index.xml",
        "http://feeds.arstechnica.com/arstechnica/index",
        "https://www.theguardian.com/technology/rss",
        "https://techcrunch.com/feed/",
        "https://www.wired.com/feed/rss",
    ],
    # ── AI ─────────────────────────────────────────────────────────────────────
    "ai_frontier": [
        "https://www.technologyreview.com/feed/",
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
        _gnews("artificial intelligence OpenAI Anthropic Google DeepMind"),
    ],
    # ── Startups / funding ─────────────────────────────────────────────────────
    "startups_funding": [
        "https://techcrunch.com/category/venture/feed/",
        _gnews("startup funding round Series A venture capital"),
    ],
    # ── Science ────────────────────────────────────────────────────────────────
    "science": [
        "https://www.sciencedaily.com/rss/top/science.xml",
        "https://www.quantamagazine.org/feed/",
        "https://www.theguardian.com/science/rss",
        "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
        "https://phys.org/rss-feed/",
        "https://www.sciencealert.com/feed",
    ],
    "biotech": [
        "https://www.statnews.com/feed/",
        "https://www.sciencedaily.com/rss/top/health.xml",
        _gnews("biotech drug trial FDA approval breakthrough"),
    ],
    # ── Essays / analysis (Substack + prestige longform) ───────────────────────
    "substack": [
        "https://www.noahpinion.blog/feed",
        "https://www.slowboring.com/feed",
        "https://www.astralcodexten.com/feed",
        "https://www.construction-physics.com/feed",
        "https://www.thefp.com/feed",                        # The Free Press
        "https://mattyglesias.substack.com/feed",
        "https://www.honest-broker.com/feed",                # Ted Gioia (music/culture)
    ],
    # ── Podcasts & podcast conversations ───────────────────────────────────────
    "podcasts": [
        "https://lexfridman.com/feed/podcast/",
        "https://feeds.simplecast.com/54nAGcIl",             # NYT The Daily
        "https://feeds.simplecast.com/82FI35Px",             # The Ezra Klein Show
        "https://feeds.simplecast.com/l2i9YnTd",             # Hard Fork
        "https://feeds.simplecast.com/dxZsm5kX",             # Pod Save America
        "https://www.dwarkesh.com/feed",                     # Dwarkesh Podcast
        _gnews("podcast episode interview conversation this week"),
    ],
    # ── Entertainment: TV / streaming / film / media ───────────────────────────
    "culture_buzz": [
        "https://www.theguardian.com/culture/rss",
        "https://variety.com/feed/",
        "https://www.hollywoodreporter.com/feed/",
        "https://deadline.com/feed/",
        "https://www.vulture.com/rss/index.xml",
        "https://www.npr.org/rss/rss.php?id=1008",           # NPR Arts
        _gnews("new on Netflix streaming series movie premiere this week"),
    ],
    # ── Music: rap / hip-hop, house / electronic, Latin / Spanish ──────────────
    "music": [
        "https://pitchfork.com/rss/news/",
        "https://www.rollingstone.com/music/feed/",
        "https://www.billboard.com/feed/",
        "https://hiphopdx.com/rss/news",                     # hip-hop / rap
        "https://www.stereogum.com/feed/",
        "https://ra.co/xml/rss.xml",                         # Resident Advisor (house/electronic)
        "https://remezcla.com/feed/",                        # Latin / Spanish-language
        _gnews("new rap album house music Latin urbano reggaeton release"),
    ],
    # ── Sports ─────────────────────────────────────────────────────────────────
    "sports": [
        "https://www.espn.com/espn/rss/news",
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://www.theguardian.com/sport/rss",
        "https://www.cbssports.com/rss/headlines/",
        _gnews("NBA NFL soccer tennis championship result last night"),
    ],
    # ── Reddit discussion (best-effort; CI IPs are often 403'd) ────────────────
    "reddit": [
        _reddit_rss("worldnews"), _reddit_rss("news"), _reddit_rss("politics"),
        _reddit_rss("technology"), _reddit_rss("science"), _reddit_rss("Economics"),
        _reddit_rss("movies"), _reddit_rss("television"), _reddit_rss("Music"),
        _reddit_rss("hiphopheads"), _reddit_rss("popculturechat"),
        _reddit_rss("OutOfTheLoop"), _reddit_rss("todayilearned"),
    ],
    # ── "Buzzing" / weird internet ─────────────────────────────────────────────
    "weird_internet": [
        _gnews("viral trending internet culture this week"),
        _reddit_rss("InternetIsBeautiful"),
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
                       lookback_hours: int = 72, per_beat_cap: int = 14,
                       max_workers: int = 8,
                       log: Callable[[str], None] | None = None
                       ) -> dict[str, list[dict[str, Any]]]:
    """Fetch + parse all raw feeds CONCURRENTLY, keep recent items, return beats.

    Best-effort: unreachable / garbled / blocked feeds are skipped. Items older than
    ``lookback_hours`` (by their real publish date) are dropped; undated items are
    kept (many feeds omit dates) so a beat is never needlessly emptied.
    """
    now = dt.datetime.now(dt.timezone.utc) if today is None else \
        dt.datetime.combine(today, dt.time(12, tzinfo=dt.timezone.utc))
    cutoff = now - dt.timedelta(hours=lookback_hours)

    # Build the full (beat, url) work list, then fetch/parse concurrently.
    jobs: list[tuple[str, str]] = [(beat, url) for beat, urls in RSS_FEEDS.items() for url in urls]

    def _fetch_one(job: tuple[str, str]) -> tuple[str, list[dict[str, Any]], str | None]:
        beat, url = job
        try:
            return beat, news_feeds.parse_feed(fetch_fn(url)), None
        except Exception as e:  # noqa: BLE001
            return beat, [], f"{type(e).__name__}"

    results: dict[str, list[dict[str, Any]]] = {beat: [] for beat in RSS_FEEDS}
    seen: dict[str, set[str]] = {beat: set() for beat in RSS_FEEDS}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for beat, entries, err in pool.map(_fetch_one, jobs):
            if err:
                if log:
                    log(f"[raw] feed failed ({beat}) — {err}")
                continue
            for e in entries:
                pub = e["published"]
                if pub is not None and pub < cutoff:
                    continue
                key = _norm_title(e["title"])[:80]
                if not key or key in seen[beat]:
                    continue
                seen[beat].add(key)
                results[beat].append(_item_from_entry(e, beat, pub, now))

    # Hacker News → tech beat
    try:
        hits = [h for h in parse_hn(fetch_fn(HN_SEARCH)) if (h.get("points") or 0) >= HN_MIN_POINTS]
    except Exception as e:  # noqa: BLE001
        hits, err = [], type(e).__name__
        if log:
            log(f"[raw] hacker news failed — {err}")
    for h in hits:
        pub = h["published"]
        if pub is not None and pub < cutoff:
            continue
        key = _norm_title(h["title"])[:80]
        if not key or key in seen[HN_BEAT]:
            continue
        seen[HN_BEAT].add(key)
        it = _item_from_entry(h, HN_BEAT, pub, now)
        it["buzz"] = h["summary"]
        results[HN_BEAT].append(it)

    # newest-first, capped per beat
    for beat, items in results.items():
        items.sort(key=lambda it: it["_sort"], reverse=True)
        for it in items:
            it.pop("_sort", None)
        results[beat] = items[:per_beat_cap]
        if log:
            log(f"[raw] {beat}: {len(results[beat])} items")

    total = sum(len(v) for v in results.values())
    if log:
        log(f"[raw] {total} items across {len(results)} beats")
    return results
