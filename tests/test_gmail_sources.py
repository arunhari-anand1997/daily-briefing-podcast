"""Unit tests for gmail_sources.py — email parsing + IMAP gathering (no network)."""
from __future__ import annotations

import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gmail_sources  # noqa: E402


def _make_email(subject, sender, body, date="Wed, 01 Jul 2026 09:30:00 +0000",
                html=False) -> bytes:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m["Date"] = date
    m.set_content(body, subtype="html" if html else "plain")
    return m.as_bytes()


def test_parse_email_plain():
    raw = _make_email("Morning Brew: markets and AI", "Morning Brew <crew@morningbrew.com>",
                      "Stocks rose today. Read more at https://mb.com/story.\nThanks!")
    it = gmail_sources.parse_email(raw)
    assert it["headline"] == "Morning Brew: markets and AI"
    assert it["source"] == "Morning Brew"
    assert it["beat"] == "newsletters"
    assert it["url"] == "https://mb.com/story"
    assert it["published"] == "2026-07-01"
    assert "Stocks rose today" in it["detail"]


def test_parse_email_html_stripped():
    raw = _make_email("Weekly essay", "Author <a@sub.com>",
                      "<h1>Big Idea</h1><p>The <b>argument</b> is subtle.</p>", html=True)
    it = gmail_sources.parse_email(raw)
    assert "Big Idea" in it["detail"] and "<" not in it["detail"]
    assert "argument is subtle" in it["detail"]


def test_parse_email_no_subject_returns_none():
    raw = _make_email("", "x@y.com", "body")
    assert gmail_sources.parse_email(raw) is None


class _FakeIMAP:
    def __init__(self, messages):
        self._messages = messages  # list[bytes]
        self.selected = None

    def select(self, name, readonly=False):
        # only the (quoted) "news" mailbox exists
        if name.strip('"').lower() == "news":
            self.selected = name
            return ("OK", [b"3"])
        return ("NO", [b"nonexistent"])

    def search(self, charset, *criteria):
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self._messages)))])

    def fetch(self, mid, spec):
        idx = int(mid) - 1
        return ("OK", [(b"%s (RFC822)" % mid, self._messages[idx])])

    def logout(self):
        pass


def test_fetch_gmail_news_reads_label_newest_first():
    msgs = [
        _make_email("First newsletter", "A <a@x.com>", "body one https://x.com/1",
                    date="Mon, 29 Jun 2026 08:00:00 +0000"),
        _make_email("Second newsletter", "B <b@x.com>", "body two",
                    date="Tue, 30 Jun 2026 08:00:00 +0000"),
    ]
    out = gmail_sources.fetch_gmail_news(
        address="me@gmail.com", app_password="app-pass", label="news",
        imap_factory=lambda *a: _FakeIMAP(msgs))
    heads = [i["headline"] for i in out]
    assert heads == ["Second newsletter", "First newsletter"]  # newest first
    assert all(i["beat"] == "newsletters" for i in out)
    assert "_pub" not in out[0]  # internal sort key stripped


def test_fetch_gmail_news_missing_label_is_empty():
    out = gmail_sources.fetch_gmail_news(
        address="me@gmail.com", app_password="pw", label="does-not-exist",
        imap_factory=lambda *a: _FakeIMAP([_make_email("x", "a@x.com", "b")]))
    assert out == []


def test_fetch_gmail_news_no_credentials_is_empty():
    assert gmail_sources.fetch_gmail_news(address=None, app_password=None) == []
    assert gmail_sources.fetch_gmail_news(address="me@gmail.com", app_password="") == []


def test_gather_gmail_news_shape():
    msgs = [_make_email("Hi", "A <a@x.com>", "body")]
    out = gmail_sources.gather_gmail_news(
        address="me@gmail.com", app_password="pw", imap_factory=lambda *a: _FakeIMAP(msgs))
    assert set(out) == {"newsletters"} and len(out["newsletters"]) == 1
    # empty -> no key at all
    assert gmail_sources.gather_gmail_news(address=None, app_password=None) == {}
