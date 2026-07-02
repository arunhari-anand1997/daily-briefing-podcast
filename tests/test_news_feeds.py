"""Unit tests for news_feeds.py — RSS/Atom parsing + wire gathering (no network)."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import news_feeds  # noqa: E402

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Reuters World</title>
  <item>
    <title>House passes major bill after all-night session</title>
    <link>https://ex.com/a</link>
    <description>&lt;p&gt;Lawmakers &lt;b&gt;narrowly&lt;/b&gt; approved the measure.&lt;/p&gt;</description>
    <pubDate>Wed, 01 Jul 2026 09:30:00 GMT</pubDate>
  </item>
  <item>
    <title>Old story from last year</title>
    <link>https://ex.com/old</link>
    <description>stale</description>
    <pubDate>Mon, 01 Jul 2025 09:30:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>NPR World</title>
  <entry>
    <title>Ceasefire holds in border region</title>
    <link href="https://npr.org/x"/>
    <summary>Diplomats meet again.</summary>
    <updated>2026-07-01T10:00:00Z</updated>
  </entry>
</feed>"""


def test_parse_rss_strips_html_and_dates():
    items = news_feeds.parse_feed(RSS)
    assert len(items) == 2
    a = items[0]
    assert a["title"] == "House passes major bill after all-night session"
    assert a["link"] == "https://ex.com/a"
    assert "narrowly approved" in a["summary"] and "<" not in a["summary"]
    assert a["published"].year == 2026 and a["published"].month == 7
    assert a["source"] == "Reuters World"


def test_parse_atom_link_href_and_iso_date():
    items = news_feeds.parse_feed(ATOM)
    assert len(items) == 1
    assert items[0]["link"] == "https://npr.org/x"
    assert items[0]["published"] == dt.datetime(2026, 7, 1, 10, 0, tzinfo=dt.timezone.utc)
    assert items[0]["source"] == "NPR World"


def test_parse_feed_bad_xml_returns_empty():
    assert news_feeds.parse_feed("<not xml") == []


def test_parse_date_rfc822_and_iso():
    assert news_feeds._parse_date("Wed, 01 Jul 2026 09:30:00 GMT").year == 2026
    assert news_feeds._parse_date("2026-07-01T10:00:00Z").hour == 10
    assert news_feeds._parse_date("garbage") is None
    assert news_feeds._parse_date(None) is None


def test_gather_wire_news_filters_old_dedupes_and_shapes():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        # every feed returns the same RSS (one fresh item, one year-old item)
        return RSS

    out = news_feeds.gather_wire_news(fake_fetch, today=dt.date(2026, 7, 2),
                                      lookback_hours=72)
    # domains present as wire_<domain>
    assert set(out) == {"wire_world", "wire_us", "wire_business"}
    world = out["wire_world"]
    # the year-old item is filtered out by its real date; the fresh one is kept
    assert all("Old story" not in it["headline"] for it in world)
    assert any("House passes major bill" in it["headline"] for it in world)
    # dedupe across the repeated feeds -> the fresh headline appears once
    fresh = [it for it in world if "House passes" in it["headline"]]
    assert len(fresh) == 1
    it = fresh[0]
    assert it["beat"] == "wire_world" and it["trusted"] is True
    assert it["url"] == "https://ex.com/a" and it["published"] == "2026-07-01"
    assert it["source"] == "Reuters World"


def test_gather_wire_news_skips_failing_feeds():
    def boom(url):
        raise RuntimeError("timeout")
    out = news_feeds.gather_wire_news(boom, today=dt.date(2026, 7, 2))
    assert out == {"wire_world": [], "wire_us": [], "wire_business": []}
