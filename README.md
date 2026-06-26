# Daily briefing podcast

A self-hosted daily news/analysis podcast that runs on GitHub Actions, writes a
~70-minute script, renders it with OpenAI TTS, uploads the MP3 to Google Drive,
and emails you the listen link with chapter markers.

Architecture (v6.1):

- **Wide-net research** — 15 parallel Sonar beats with explicit time-anchoring
  (today's date, day-of-week, yesterday, last week) so the prompt knows what's
  recent.
- **Real markets data** — Yahoo Finance quotes for Brent, WTI, gold, indices,
  FX, rates, BTC/ETH/SOL, plus an equity watchlist. The LLM is only used for an
  optional one-line market-vibe narrative, eliminating stale-price
  hallucinations.
- **Hard dedupe** — rapidfuzz headline similarity + entity-set Jaccard against
  the last 7 days of recaps. Cold-open gets a stricter threshold with a donor
  chain (science → tech_business → world → arguments → culture) if it would
  otherwise repeat.
- **Buzzing Topics** — inside the arguments segment, a deep-analysis
  subsegment pulls actual source text from named X voices, op-eds, and podcast
  episodes (with Sonar fallback when URLs 404).
- **Numeric fact-check** — extracts dollar amounts, percentages, scorelines,
  and casualty counts from the finished script and verifies each. Stale figures
  are rewritten with the correct value or a qualifier. Economy segment is
  excluded since its numbers come from Yahoo (ground truth).
- **10 content-type segments** target ~10.4k words / ~70 minutes.

## How it runs

1. GitHub Actions cron fires at **09:00 UTC** (5 AM EDT / 4 AM EST).
2. `podcast.py` does research, curation, scripting, fact-check, and TTS,
   writing files into `episodes/YYYY-MM-DD/`.
3. `deliver.py` uploads the MP3 + recap.json + curated.json to Google Drive
   and emails you a link with chapters.
4. The workflow commits today's `recaps/YYYY-MM-DD.json` back to the repo so
   tomorrow's run has dedupe memory.

End-to-end ~10-15 minutes; well under the 6-hour Actions wall-clock limit.

## Setup

### 1. API keys

- **Perplexity API key** — https://www.perplexity.ai/settings/api
- **OpenAI API key** — https://platform.openai.com/api-keys
  (needs access to `gpt-4o-mini-tts` or the model named in `TTS_MODEL` in
  `podcast.py`)

### 2. Google Drive OAuth

The Drive upload authenticates **as you** via an OAuth refresh token, so files
land in your own Drive and use your quota. (A service account has no Drive
storage of its own and returns `403 storageQuotaExceeded` on upload unless you
have a Workspace Shared Drive — OAuth works on free Gmail.)

1. In [Google Cloud Console](https://console.cloud.google.com/), create or
   pick a project and enable the **Google Drive API**.
2. Configure the **OAuth consent screen** (User type: External). Add your own
   Gmail address under **Test users** so the unverified app keeps working for
   you indefinitely.
3. Create **Credentials → OAuth client ID → Application type: Desktop app**.
   Download the client JSON (or note its client ID + secret).
4. Mint a refresh token once, locally:
   ```bash
   pip install google-auth-oauthlib
   python oauth_setup.py --client-secrets-file client_secret.json
   ```
   It opens a browser, you grant access, and it prints
   `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
   `GOOGLE_OAUTH_REFRESH_TOKEN` to paste into your secrets.
5. Pick the destination folder in your My Drive and copy its ID from the URL:
   `drive.google.com/drive/folders/<this part>`.

### 3. Gmail SMTP

1. Enable 2FA on your Gmail account (required).
2. Visit https://myaccount.google.com/apppasswords and generate an App
   Password labeled e.g. "Daily Podcast".
3. The 16-character string (no spaces) is your `GMAIL_APP_PASSWORD`.

### 4. Repository Secrets

Add these under **Settings → Secrets and variables → Actions** in this repo:

| Secret | Value |
| --- | --- |
| `PERPLEXITY_API_KEY` | `pplx-...` |
| `OPENAI_API_KEY` | `sk-...` |
| `GOOGLE_OAUTH_CLIENT_ID` | From `oauth_setup.py` (step 2) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | From `oauth_setup.py` (step 2) |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | From `oauth_setup.py` (step 2) |
| `GOOGLE_DRIVE_FOLDER_ID` | The folder ID from step 2 |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | 16-char app password |
| `EMAIL_TO` | Where the briefing emails go |

### 5. Trigger a first run

Go to the **Actions** tab → "Daily briefing podcast" → "Run workflow" to fire
a one-off run. After it succeeds, the daily cron will keep it running.

## Local development

```bash
cp .env.example .env
# fill in your values
pip install -r requirements.txt
set -a; source .env; set +a
python podcast.py                              # generate full episode
python podcast.py --skip-tts                   # dry run (~5 min, no audio)
python podcast.py --from-script                # resume TTS from existing chunks
python deliver.py --result-json result.json    # upload + email after a run
```

## Files

```
podcast.py              # main pipeline: research → curate → write → TTS
deliver.py              # post-run uploader + emailer
recaps/                 # 7-day dedupe memory (committed; do not delete)
episodes/               # generated; gitignored
.github/workflows/      # daily cron + manual dispatch
```

## Notes

- Sonar can hallucinate URLs for the twitter/substack beats. The buzzing
  fetcher detects this and falls back to a Sonar "find the actual recent take
  from @handle and quote it" call.
- The TTS step uses 10 parallel workers and takes ~7-10 minutes for ~280
  chunks. If you hit OpenAI rate limits, lower `TTS_PARALLELISM` in
  `podcast.py`.
- To change cadence, edit the `cron` line in `.github/workflows/daily.yml`.
- To unsubscribe yourself: delete the workflow file or disable it under the
  Actions tab.
