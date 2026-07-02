"""news_feeds.py — a deterministic, real-source news layer.

Sonar used as an open-ended "find me today's news" generator hallucinates events
(it reported the One Big Beautiful Bill Act — passed July 2025 — as if the House
"muscled it through last Wednesday" in 2026, on its anniversary). This module
instead pulls REAL, timestamped articles from trusted outlets via RSS, exactly
like macro.py/Yahoo pull real numbers: every item has a real headline, a real
URL, and a real publish date, so nothing can be fabricated.

Coverage: major-outlet RSS (BBC / NPR / The Guardian / Al Jazeera / NYT) plus
Google News RSS topic + search feeds, which aggregate Reuters, AP, and the rest
with real article URLs (Reuters/AP dropped their own public RSS; Ground News has
no free feed). No API key required.

Parsing is stdlib-only (xml.etree + email.utils for RFC-822 dates) and fully
unit-tested; the network fetch is dependency-injected and best-effort.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Callable

FetchFn = Callable[[str], str]

# Google News RSS (keyless). "when:2d" limits to the last 2 days; source attribution
# and real article URLs are preserved. hl/gl/ceid pin US English.
def _gnews(query: str) -> str:
    from urllib.parse import quote
    return (f"https://news.google.com/rss/search?q={quote(query)}%20when:2d"
            "&hl=en-US&gl=US&ceid=US:en")


# domain -> list of RSS feed URLs. Domains map to the show's news segments.
FEEDS: dict[str, list[str]] = {
    "world": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.theguardian.com/world/rss",
        "https://feeds.npr.org/1004/rss.xml",            # NPR World
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
        _gnews("Reuters OR AP world news"),
    ],
    "us": [
        "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
        "https://feeds.npr.org/1014/rss.xml",            # NPR Politics
        "https://www.theguardian.com/us-news/rss",
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-US&gl=US&ceid=US:en",
        _gnews("US politics Congress White House Supreme Court"),
    ],
    "business": [
        "https://feeds.npr.org/1006/rss.xml",            # NPR Business
        "https://www.theguardian.com/uk/business/rss",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
        _gnews("Reuters economy markets Federal Reserve"),
    ],
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # strip XML namespace


def _parse_date(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:  # RFC-822 (RSS <pubDate>): "Wed, 01 Jul 2026 12:00:00 GMT"
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:  # ISO-8601 (Atom <updated>/<published>)
            d = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Parse an RSS-2.0 or Atom feed into [{title, link, summary, published, source}].

    ``published`` is a timezone-aware UTC datetime or None. Namespace-agnostic.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    # Feed-level source name (channel/title for RSS, feed/title for Atom).
    feed_source = ""
    for el in root.iter():
        if _localname(el.tag) == "title":
            feed_source = _clean(el.text)
            break

    out: list[dict[str, Any]] = []
    for node in root.iter():
        name = _localname(node.tag)
        if name not in ("item", "entry"):
            continue
        title = link = summary = date_raw = ""
        item_source = ""
        for child in node:
            cn = _localname(child.tag)
            if cn == "title":
                title = _clean(child.text)
            elif cn == "link":
                # RSS: text; Atom: href attribute
                link = (child.text or child.get("href") or "").strip()
            elif cn in ("description", "summary", "content"):
                summary = summary or _clean(child.text)
            elif cn in ("pubDate", "published", "updated", "date"):
                date_raw = date_raw or (child.text or "").strip()
            elif cn == "source":
                item_source = _clean(child.text)
        if not title or not link:
            continue
        out.append({
            "title": title,
            "link": link,
            "summary": summary,
            "published": _parse_date(date_raw),
            "source": item_source or feed_source,
        })
    return out


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (t or "").lower()).strip()


def gather_wire_news(fetch_fn: FetchFn, *, today: dt.date | None = None,
                     lookback_hours: int = 72, per_domain_cap: int = 15,
                     log: Callable[[str], None] | None = None
                     ) -> dict[str, list[dict[str, Any]]]:
    """Fetch + parse all feeds, keep real recent articles, return research-shaped items.

    Returns {f"wire_{domain}": [item,...]} where each item matches the research-beat
    shape (headline/detail/source/url/published/beat) and is marked trusted=True.
    Best-effort: unreachable/garbled feeds are skipped.
    """
    now = dt.datetime.now(dt.timezone.utc) if today is None else \
        dt.datetime.combine(today, dt.time(12, tzinfo=dt.timezone.utc))
    cutoff = now - dt.timedelta(hours=lookback_hours)
    results: dict[str, list[dict[str, Any]]] = {}

    for domain, urls in FEEDS.items():
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for url in urls:
            try:
                entries = parse_feed(fetch_fn(url))
            except Exception as e:  # noqa: BLE001
                if log:
                    log(f"[wire] feed failed ({domain}): {url.split('?')[0]} — {type(e).__name__}")
                continue
            for e in entries:
                pub = e["published"]
                if pub is not None and pub < cutoff:
                    continue  # too old — a real, dated article outside the window
                key = _norm_title(e["title"])[:80]
                if not key or key in seen:
                    continue
                seen.add(key)
                items.append({
                    "headline": e["title"][:200],
                    "detail": e["summary"][:280],
                    "angle": "",
                    "source": e["source"][:80],
                    "url": e["link"],
                    "published": pub.date().isoformat() if pub else "",
                    "buzz": "trusted wire feed",
                    "beat": f"wire_{domain}",
                    "trusted": True,
                    "_sort": pub or now,
                })
        # newest first, capped
        items.sort(key=lambda it: it["_sort"], reverse=True)
        for it in items:
            it.pop("_sort", None)
        results[f"wire_{domain}"] = items[:per_domain_cap]
        if log:
            results_n = len(results[f"wire_{domain}"])
            log(f"[wire] {domain}: {results_n} real articles")
    return results
