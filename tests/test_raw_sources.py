"""Unit tests for raw_sources.py — RSS/HN aggregation (no network)."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import raw_sources  # noqa: E402

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>The Verge</title>
  <item><title>New chip breaks a benchmark record</title>
    <link>https://ex.com/chip</link><description>Fast.</description>
    <pubDate>Wed, 01 Jul 2026 09:30:00 GMT</pubDate></item>
  <item><title>Ancient gadget resurfaces from last year</title>
    <link>https://ex.com/old</link><description>stale</description>
    <pubDate>Mon, 01 Jul 2025 09:30:00 GMT</pubDate></item>
</channel></rss>"""

HN = json.dumps({"hits": [
    {"title": "Show HN: a tiny database", "url": "https://ex.com/db",
     "points": 220, "num_comments": 40, "objectID": "111", "created_at": "2026-07-01T10:00:00Z"},
    {"title": "Low-score post nobody read", "url": "https://ex.com/meh",
     "points": 5, "num_comments": 0, "objectID": "222", "created_at": "2026-07-01T10:00:00Z"},
    {"title": "Ask HN: text post with no url", "url": "",
     "points": 300, "num_comments": 99, "objectID": "333", "created_at": "2026-07-01T11:00:00Z"},
]})


def test_parse_hn_shapes_and_bad_json():
    items = raw_sources.parse_hn(HN)
    assert len(items) == 3
    first = items[0]
    assert first["title"] == "Show HN: a tiny database" and first["points"] == 220
    assert first["published"].year == 2026
    # text post with no url -> links to the HN discussion
    textpost = next(i for i in items if i["title"].startswith("Ask HN"))
    assert textpost["link"] == "https://news.ycombinator.com/item?id=333"
    assert raw_sources.parse_hn("<not json>") == []


def test_gather_raw_sources_filters_dedupes_and_folds_hn():
    def fake_fetch(url):
        if url == raw_sources.HN_SEARCH:
            return HN
        return RSS  # every RSS feed returns the same two items (one fresh, one year-old)

    out = raw_sources.gather_raw_sources(fake_fetch, today=dt.date(2026, 7, 2),
                                         lookback_hours=72)
    # every configured beat is present
    assert set(raw_sources.RSS_FEEDS).issubset(out)
    tech = out["tech_news"]
    heads = [it["headline"] for it in tech]
    # year-old item filtered by its real date
    assert all("Ancient gadget" not in h for h in heads)
    # fresh RSS item kept and deduped across the beat's repeated feeds
    assert sum("New chip breaks" in h for h in heads) == 1
    # HN folded into tech_news, but only the >= HN_MIN_POINTS stories
    assert any("a tiny database" in h for h in heads)
    assert all("Low-score post" not in h for h in heads)
    # research-shaped
    it = next(i for i in tech if "New chip" in i["headline"])
    assert it["beat"] == "tech_news" and it["url"] == "https://ex.com/chip"
    assert it["published"] == "2026-07-01"


def test_gather_raw_sources_skips_failing_feeds():
    def boom(url):
        raise RuntimeError("blocked")
    out = raw_sources.gather_raw_sources(boom, today=dt.date(2026, 7, 2))
    # all beats present but empty; no crash
    assert set(raw_sources.RSS_FEEDS).issubset(out)
    assert all(v == [] for v in out.values())
