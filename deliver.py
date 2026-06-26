#!/usr/bin/env python3
"""deliver.py — upload today's briefing to Google Drive and email a link.

Reads JSON result from podcast.py stdout (passed via --result-json) and:
  1. Uploads MP3, recap.json, and curated.json to a Google Drive folder
  2. Builds chapter markers from script.json word counts at ~150 wpm
  3. Sends an HTML email with the listen link, chapters, and editor note

Env vars required:
  GOOGLE_OAUTH_CLIENT_ID      — OAuth client ID (Desktop app) from Google Cloud
  GOOGLE_OAUTH_CLIENT_SECRET  — OAuth client secret for that client
  GOOGLE_OAUTH_REFRESH_TOKEN  — refresh token from one-time `oauth_setup.py` run
  GOOGLE_DRIVE_FOLDER_ID      — Drive folder ID in YOUR My Drive (uploads use your quota)
  GMAIL_ADDRESS               — your Gmail address (used as SMTP user + From)
  GMAIL_APP_PASSWORD          — 16-char Gmail App Password
  EMAIL_TO                    — recipient address

Uploads authenticate as you via OAuth (not a service account), so files land in
your own Drive and consume your storage quota. Service accounts have no Drive
quota of their own, which is why the SA approach 403s with `storageQuotaExceeded`
unless you have a Workspace Shared Drive. OAuth delegation works on free Gmail.

Optional:
  EMAIL_SUBJECT_PREFIX     — default "Your daily briefing"
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


WPM = 150  # for chapter timestamping

# drive.file scope = per-file access to files this app creates/opens. Enough to
# upload episodes into a folder you point at; does not grant broad Drive access.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _drive_service():
    # Authenticate as the user via a stored OAuth refresh token. The token is
    # long-lived; we exchange it for a short-lived access token on each run.
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=DRIVE_SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_to_drive(path: Path, folder_id: str, mime: str | None = None) -> dict[str, str]:
    """Upload a file and return {id, web_view_link}."""
    svc = _drive_service()
    body = {"name": path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(path), mimetype=mime, resumable=False)
    created = svc.files().create(
        body=body, media_body=media, fields="id, webViewLink, webContentLink"
    ).execute()
    return {
        "id": created["id"],
        "web_view_link": created.get("webViewLink", ""),
        "web_content_link": created.get("webContentLink", ""),
    }


def build_chapters(script_path: Path) -> list[dict[str, Any]]:
    """Return [{label, start_seconds, start_hms}] using cumulative word counts."""
    lines = json.loads(script_path.read_text())
    chapters: list[dict[str, Any]] = []
    cumulative_words = 0
    current_seg: str | None = None
    for line in lines:
        seg = line.get("segment", "")
        label = line.get("segment_label", seg)
        if seg != current_seg:
            secs = int(cumulative_words / WPM * 60)
            chapters.append({
                "label": label or seg,
                "start_seconds": secs,
                "start_hms": _hms(secs),
            })
            current_seg = seg
        cumulative_words += len(line.get("text", "").split())
    return chapters


def _hms(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _format_email_html(
    *,
    date_str: str,
    duration_min: float,
    word_count: int,
    mp3_link: str,
    editor_note: str,
    chapters: list[dict[str, Any]],
) -> str:
    chapter_rows = "\n".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#666;font-variant-numeric:tabular-nums;">{c["start_hms"]}</td>'
        f'<td style="padding:4px 0;">{c["label"]}</td></tr>'
        for c in chapters
    )
    return f"""<!doctype html><html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#222;">
<p style="font-size:18px;margin:0 0 16px;"><a href="{mp3_link}" style="color:#0066cc;text-decoration:none;font-weight:600;">▶ Listen on Google Drive</a></p>
<p style="color:#666;margin:0 0 24px;">~{duration_min:.0f} min · {word_count:,} words</p>
{f'<blockquote style="border-left:3px solid #ddd;padding:8px 16px;margin:0 0 24px;color:#444;font-style:italic;">{editor_note}</blockquote>' if editor_note else ''}
<h3 style="margin:0 0 8px;font-size:14px;color:#666;text-transform:uppercase;letter-spacing:0.05em;">Chapters</h3>
<table style="border-collapse:collapse;font-size:14px;">
{chapter_rows}
</table>
<p style="color:#999;font-size:12px;margin-top:32px;">Daily 5am ET · {date_str}</p>
</body></html>"""


def send_email(*, subject: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content("HTML email — please view in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-json", required=True,
                    help="Path to the JSON result line written by podcast.py")
    args = ap.parse_args()

    result = json.loads(Path(args.result_json).read_text())
    if result.get("status") != "ok":
        print(f"[deliver] podcast run did not succeed: {result}", file=sys.stderr)
        return 2

    mp3_path = Path(result["mp3_path"])
    recap_path = Path(result["recap_path"])
    curated_path = Path(result["curated_path"])
    script_path = Path(result["script_path"])
    date_str = result["date"]
    folder_id = os.environ["GOOGLE_DRIVE_FOLDER_ID"]

    print("[deliver] uploading MP3 + recap + curated to Drive...", flush=True)
    mp3_info = upload_to_drive(mp3_path, folder_id, mime="audio/mpeg")
    upload_to_drive(recap_path, folder_id, mime="application/json")
    upload_to_drive(curated_path, folder_id, mime="application/json")
    print(f"[deliver] MP3 at {mp3_info['web_view_link']}", flush=True)

    chapters = build_chapters(script_path)
    editor_note = ""
    try:
        curated = json.loads(curated_path.read_text())
        editor_note = curated.get("editor_note", "") or ""
    except Exception:
        pass

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    duration_min = result.get("duration_minutes", result.get("word_count", 0) / WPM)
    subject_prefix = os.environ.get("EMAIL_SUBJECT_PREFIX", "Your daily briefing")
    subject = f"{subject_prefix} - {dt.strftime('%a %b %d')}, ~{duration_min:.0f} min"

    html = _format_email_html(
        date_str=date_str,
        duration_min=duration_min,
        word_count=result.get("word_count", 0),
        mp3_link=mp3_info["web_view_link"],
        editor_note=editor_note,
        chapters=chapters,
    )
    send_email(subject=subject, html_body=html)
    print(f"[deliver] emailed {os.environ['EMAIL_TO']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
