"""gmail_sources.py — pull curated newsletter material from a Gmail label via IMAP.

The user funnels newsletters into a Gmail label (a "filter called news"). That's a
hand-curated, high-signal feed we already have credentials for: the pipeline sends
mail with GMAIL_ADDRESS + GMAIL_APP_PASSWORD, and the same app password grants IMAP
read access. This module logs in, reads recent messages under the label, and returns
them as research-shaped items (beat="newsletters") for the OpenAI curator to judge.

The IMAP I/O is isolated behind an injectable ``imap_factory`` and the message
parsing is a pure function, so everything is unit-tested without a network or real
credentials. Entirely best-effort: any failure (bad creds, missing label, IMAP
down) logs and returns nothing — it never breaks a run.
"""
from __future__ import annotations

import datetime as dt
import email
import re
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable

from news_feeds import _clean  # HTML-strip + whitespace-collapse (tested)

GMAIL_IMAP_HOST = "imap.gmail.com"
DEFAULT_LABEL = "news"
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 — malformed MIME headers shouldn't crash us
        return raw


def _body_text(msg: email.message.Message) -> str:
    """Best-effort readable body: prefer text/plain, fall back to stripped HTML."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            if ctype == "text/plain":
                plain += text + "\n"
            else:
                html += text + "\n"
    else:
        try:
            payload = msg.get_payload(decode=True)
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace") \
                if payload is not None else str(msg.get_payload())
        except Exception:  # noqa: BLE001
            text = ""
        if msg.get_content_type() == "text/html":
            html = text
        else:
            plain = text
    return (plain.strip() or _clean(html)).strip()


def parse_email(raw_bytes: bytes) -> dict[str, Any] | None:
    """Parse one RFC-822 message into a research-shaped newsletter item, or None."""
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:  # noqa: BLE001
        return None
    subject = _decode(msg.get("Subject")).strip()
    if not subject:
        return None
    sender_name, sender_addr = parseaddr(_decode(msg.get("From")))
    source = (sender_name or sender_addr or "newsletter").strip()

    pub = None
    try:
        pub = parsedate_to_datetime(msg.get("Date"))
        if pub is not None and pub.tzinfo is not None:
            pub = pub.astimezone(dt.timezone.utc)
    except (TypeError, ValueError, IndexError):
        pub = None

    body = _body_text(msg)
    m = _URL_RE.search(body)
    url = m.group(0).rstrip(".,);]") if m else ""

    return {
        "headline": subject[:200],
        "detail": _clean(body)[:280],
        "angle": "",
        "source": source[:80],
        "url": url,
        "published": pub.date().isoformat() if pub else "",
        "buzz": "from the reader's curated 'news' newsletters",
        "beat": "newsletters",
        "_pub": pub,
    }


def _default_imap_factory(host: str, address: str, app_password: str):
    import imaplib
    imap = imaplib.IMAP4_SSL(host)
    imap.login(address, app_password)
    return imap


def _select_label(imap, label: str, log=None) -> bool:
    """Select the label's mailbox, tolerant of case / quoting differences."""
    for name in (f'"{label}"', label, f'"{label.title()}"', f'"{label.capitalize()}"'):
        typ, _ = imap.select(name, readonly=True)
        if typ == "OK":
            return True
    # Last resort: scan the mailbox list for a case-insensitive match.
    try:
        typ, boxes = imap.list()
        if typ == "OK":
            for b in boxes:
                name = b.decode(errors="replace") if isinstance(b, bytes) else str(b)
                if name.rsplit('"', 2)[-2].lower() == label.lower() if '"' in name else False:
                    if imap.select(name.rsplit('"', 2)[-2], readonly=True)[0] == "OK":
                        return True
    except Exception:  # noqa: BLE001
        pass
    if log:
        log(f"[gmail] label '{label}' not found")
    return False


def fetch_gmail_news(*, address: str | None, app_password: str | None,
                     label: str = DEFAULT_LABEL, since_days: int = 3, cap: int = 25,
                     today: dt.date | None = None,
                     imap_factory: Callable[..., Any] | None = None,
                     log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Read recent messages under a Gmail label and return newsletter items.

    Best-effort: returns [] on any error. ``imap_factory(host, address, app_password)``
    is injectable for tests; it must return an object exposing the imaplib IMAP4 API
    (select / search / fetch / logout).
    """
    if not address or not app_password:
        if log:
            log("[gmail] no GMAIL credentials — skipping newsletter source")
        return []
    factory = imap_factory or _default_imap_factory
    today = today or dt.datetime.now(dt.timezone.utc).date()
    since = (today - dt.timedelta(days=since_days)).strftime("%d-%b-%Y")

    imap = None
    try:
        imap = factory(GMAIL_IMAP_HOST, address, app_password)
        if not _select_label(imap, label, log):
            return []
        typ, data = imap.search(None, "SINCE", since)
        if typ != "OK" or not data or not data[0]:
            if log:
                log(f"[gmail] no messages in '{label}' since {since}")
            return []
        ids = data[0].split()[-cap:]  # most recent `cap`
        items: list[dict[str, Any]] = []
        for mid in reversed(ids):  # newest first
            typ, msg_data = imap.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            raw = next((p[1] for p in msg_data if isinstance(p, tuple) and p[1]), None)
            if not raw:
                continue
            item = parse_email(raw)
            if item:
                item.pop("_pub", None)
                items.append(item)
        if log:
            log(f"[gmail] {len(items)} newsletter item(s) from label '{label}'")
        return items
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"[gmail] fetch failed (non-fatal): {type(e).__name__}: {e}")
        return []
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001
                pass


def gather_gmail_news(**kwargs) -> dict[str, list[dict[str, Any]]]:
    """Return {"newsletters": [...]} shaped like the other gatherers (or {} if empty)."""
    items = fetch_gmail_news(**kwargs)
    return {"newsletters": items} if items else {}
