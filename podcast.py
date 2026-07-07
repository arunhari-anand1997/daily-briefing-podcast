#!/usr/bin/env python3
"""
Daily Briefing Podcast Generator (v7 — tiered memory + two-pass curator + editorial intelligence)

Pipeline:
  1. Load recent episode recaps (last 7 days) from local store
  2. WIDE-NET research: 15 beats across all domains (geopolitics, economics, science,
     biotech, tech/AI, startups, Twitter discourse, Reddit, Substack essays, podcasts,
     literature, history, culture, sports, wild studies)
  3. Pull live markets data via Perplexity
  4. CURATOR pass — Sonar ranks all candidates, enforces cross-domain balance,
     emits a final topic pool plus a cutting-room-floor list of runners-up
  5. Generate the script SEGMENT BY SEGMENT (multi-pass) using the curated pool
     — includes a "cutting room floor" segment for the runners-up
  6. Extract a structured recap of today's episode for future de-dup
  7. Run OpenAI TTS in chunks
  8. Stitch segments with ffmpeg
  9. Save the recap locally (cron uploads it to Drive afterwards)
 10. Print final JSON result to stdout

Reads credentials from environment via the secure custom-cred proxy:
  - Perplexity: requests go to api.perplexity.ai
  - OpenAI:     requests go to api.openai.com

Both keys are referenced ONLY through api_credentials=['custom-cred:api.perplexity.ai',
'custom-cred:api.openai.com'] on the bash call that runs this script.
The raw keys never appear in this file or any output.

Usage:
  python3 podcast.py                  # full daily run (~60 min episode)
  python3 podcast.py --test           # short test run (3 segments only)
  python3 podcast.py --skip-tts       # research + script + recap only
  python3 podcast.py --recap-only     # regenerate today's recap from existing script.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
try:
    from rapidfuzz import fuzz as _rf_fuzz
except Exception:
    _rf_fuzz = None
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# v7 Tier 2/3 modules — pure data layer + curation/editorial helpers.
import memory
import curator as curator_mod
import editorial
import macro as macro_mod
import news_feeds

# ─── CONFIG ──────────────────────────────────────────────────────────────────

# BASE_DIR can be overridden via PODCAST_BASE_DIR (e.g. on GitHub Actions where the
# repo is checked out into a runner workdir). Defaults to the directory holding this
# file so a fresh `python3 podcast.py` works from any clone.
BASE_DIR = Path(os.environ.get("PODCAST_BASE_DIR", str(Path(__file__).resolve().parent)))
EPISODES_DIR = BASE_DIR / "episodes"
RECAPS_DIR = BASE_DIR / "recaps"

# API keys: in Computer they were injected by a proxy, so any Bearer string worked.
# Outside Computer (GitHub Actions, local), real keys must come from env vars.
PPLX_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "__INJECTED__")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "__INJECTED__")
EPISODES_DIR.mkdir(parents=True, exist_ok=True)
RECAPS_DIR.mkdir(parents=True, exist_ok=True)

PPLX_URL = "https://api.perplexity.ai/chat/completions"
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# ─── LLM ROLES ───────────────────────────────────────────────────────────────
# Generation and every editorial JUDGMENT (curate / dedupe / fact-check / recency
# / memory-review / buzz) run on OpenAI. Perplexity is reduced to a thin, NON-
# CRITICAL base-tier `sonar` live-search supplement for breaking developments and
# X/Twitter buzz that the raw feeds miss — a sonar outage can no longer crash a run.
# The news itself is aggregated from raw sources (RSS / Reddit / Substack); OpenAI
# decides what to keep. All model ids are env-overridable for easy tuning.
SCRIPT_MODEL  = os.environ.get("SCRIPT_MODEL",  "gpt-4o")       # writer (quality-critical)
CURATOR_MODEL = os.environ.get("CURATOR_MODEL", "gpt-4o-mini")  # keep / route / aggregate
JUDGE_MODEL   = os.environ.get("JUDGE_MODEL",   "gpt-4o-mini")  # fact-check / recency / currency / pass-2 / buzz / markets
SEARCH_MODEL  = os.environ.get("SEARCH_MODEL",  "sonar")        # base-tier residual live search (non-critical)
RECAP_MODEL   = os.environ.get("RECAP_MODEL",   "gpt-4o-mini")  # recap / digest extraction
RESEARCH_MODEL = SEARCH_MODEL          # legacy alias: the residual live-search beat

TTS_MODEL = "gpt-4o-mini-tts"
HOST_A_VOICE = "nova"                  # Maya — analytical
HOST_B_VOICE = "onyx"                  # Ben — skeptical/witty

WATCHLIST = ["NVDA", "MSFT", "GOOGL", "META", "AMZN", "AAPL", "TSLA", "PLTR", "COIN", "HOOD"]
CRYPTO = ["BTC", "ETH", "SOL"]

# How many days of recap memory to feed back to the writer
RECAP_LOOKBACK_DAYS = 7

# v7 two-pass curator + editorial intelligence (Tier 2/3) is now the DEFAULT
# path. The v6.1 single-pass behaviour remains available as a kill-switch:
# set TWO_PASS_CURATOR=0 (or pass --no-two-pass-curator) to fall back if v7
# ever misbehaves on the daily cron, without needing a revert.
TWO_PASS_CURATOR_ENV = (
    os.environ.get("TWO_PASS_CURATOR", "1").strip().lower() not in ("0", "false", "no")
)

# OpenAI TTS limit: 4096 chars per call. Keep chunks well under.
TTS_CHUNK_CHAR_BUDGET = 3500

# ─── SEGMENT PLAN ────────────────────────────────────────────────────────────
# Each segment gets its own LLM call with a focused word target.
# This is the key to hitting ~60 min reliably (vs. ~14 min in single-pass).

# Segments map to CONTENT TYPES, not raw research beats. The curator (next stage)
# picks which specific stories land in each segment.
# v8 taste-spec lineup: ~120 min broadcast structure (≈150 wpm → ~18k words).
# Balanced across domains; Tech & AI is deliberately capped (see curator + caps).
SEGMENTS_FULL: list[dict[str, Any]] = [
    {"key": "cold_open",    "label": "Cold open & headlines",            "words": 700},
    {"key": "hard_news",    "label": "Hard news",                        "words": 2600},
    {"key": "politics",     "label": "Politics & policy arguments",      "words": 2000},
    {"key": "economy",      "label": "Economics, business & markets",    "words": 2000},
    {"key": "tech_ai",      "label": "Tech & AI",                        "words": 2000},
    {"key": "science",      "label": "Science & discovery",             "words": 2300},
    {"key": "feature",      "label": "Feature essay / longform",         "words": 2300},
    {"key": "culture",      "label": "Culture",                          "words": 1900},
    {"key": "sports",       "label": "Sports narratives & standings",    "words": 1500},
    {"key": "disagreement", "label": "Room for disagreement",            "words": 850},
    {"key": "signoff",      "label": "Outro",                            "words": 400},
]

# Subset for --test mode (~6 min episode)
SEGMENTS_TEST: list[dict[str, Any]] = [
    {"key": "cold_open", "label": "Cold open & headlines", "words": 120},
    {"key": "feature", "label": "Feature essay / longform", "words": 500},
    {"key": "signoff", "label": "Outro", "words": 100},
]

# ─── RESEARCH BEATS ──────────────────────────────────────────────────────────

# Wide-net research: 15 beats across all domains. Goal: produce far MORE topic
# candidates than will fit in the show. The curator (next stage) ranks & cuts.
# Each beat returns a JSON array of topic objects so the curator can rank them.

BEAT_OUTPUT_INSTRUCTION = """Return ONLY a JSON array. No prose, no markdown, no code fences.

CRITICAL TIME ANCHORING: Today's date is provided to you. Search the live web for current items.
For each item, return a `published` ISO date matching the source's actual publish date.
PREFER items from the last 48-72 hours, but DO accept items up to 14 days old if they are still
the most important story in their domain (better a true 5-day-old story than a fabricated fresh one).
Mark older items honestly with their real date. Never invent dates. If you genuinely cannot find any
items in the requested window for a beat, return fewer items rather than padding with stale ones.

Each element MUST be an object with these exact keys:
  "headline":   short factual claim (one sentence)
  "detail":     2-4 sentence summary with the actual substance, numbers, names
  "angle":      why this is genuinely interesting or perspective-shifting (one sentence)
  "source":     publication / podcast / Substack / handle name
  "url":        REAL working URL to the actual article/thread/episode. REQUIRED.
                If you do not have a verifiable URL, OMIT the item entirely. Do not invent URLs.
  "published":  ISO date YYYY-MM-DD when the source was published. REQUIRED.
                Must be within the last 7 days (last 48 hours strongly preferred).
                If you do not know the publish date, OMIT the item.
  "buzz":       short phrase on where this is getting traction
                (e.g. "viral X thread, 12k retweets", "top of r/slatestarcodex",
                "front page Hacker News", "trending in tech Twitter"). REQUIRED.
Return 6-10 items. Quality and specificity over quantity. Recency and verifiability are MANDATORY.
DO NOT include anything outside the JSON array."""

BEATS: list[tuple[str, str]] = [
    ("geopolitics", f"""Major world & geopolitical stories from the LAST 24-48 HOURS.
Cover wars (Russia/Ukraine, Middle East, any escalations), US/China dynamics, elections in major democracies,
notable diplomatic moves, sanctions, summits, treaties, coups, protests, refugee crises. Skip routine politicking.
Focus on stories where something MATERIAL changed or was revealed. Prefer reporting from Reuters, AP, FT, NYT,
WaPo, Bloomberg, The Economist, BBC. Real URLs published in the last 48 hours.
Return 8-12 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("us_politics", f"""US DOMESTIC politics & policy from the LAST 24-72 HOURS — this is the day's
American political reporting, NOT world news and NOT opinion essays. Cover: the White House and executive
actions/orders, Congress (bills, votes, hearings, leadership fights, shutdown/budget fights), the Supreme
Court and federal courts (rulings, major cases), federal agencies (Fed, DOJ, SEC, FTC, DHS, etc.), the
2026 elections and campaigns, governors and consequential state-level moves, and major political
controversies. Each item must be a CONCRETE development that happened on a specific recent date — name the
date. Prefer hard reporting from NYT, WaPo, Politico, The Hill, Axios, Reuters, AP, Semafor, WSJ, NBC/CNN.
Exclude pure punditry/opinion columns (those belong in essays). Real URLs published in the last 72 hours.
Return 8-12 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("economics", f"""Real economy stories from the LAST 48 HOURS: macro data releases (CPI, jobs, GDP, PCE),
central bank moves, energy prices, trade flows, currency moves, debt markets, commodity moves, housing data,
signs of stress or strength. Include one or two non-obvious indicators (shipping rates, credit spreads, etc.).
Sources: Bloomberg, FT, WSJ, Reuters, CNBC, central bank releases. Real URLs in the last 48 hours.
Return 8-10 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("science", f"""Genuinely interesting science news / preprints / Nature & Science papers from the LAST 7 DAYS.
Physics, chemistry, astronomy, neuroscience, ecology, materials, climate. Prefer counterintuitive findings,
results that change a textbook, or unexpected applications. Skip incremental papers.
Sources: Nature, Science, Quanta Magazine, Ars Technica science desk, Phys.org, MIT Technology Review,
university press rooms, ScienceDaily, New Scientist. Return 8-10 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("biotech", f"""Biotech, medicine, longevity & wild biology stories from the LAST 7 DAYS.
Clinical trial readouts, FDA actions, novel mechanisms, longevity & aging research, neuroscience,
weird biology (microbiome, fungi, gene editing surprises). Skip pharma earnings.
Sources: STAT News, Endpoints, BioPharma Dive, Nature Medicine, Science Translational Medicine.
Return 6-10 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("crazy_studies", f"""The most surprising / counterintuitive academic studies published in the LAST 14 DAYS
that are getting actual social media or press attention. Psychology, behavioral econ, sociology, public health,
epidemiology, history-as-data, economics. Things that would make a thoughtful listener go "huh, really?" —
real findings, not pop-science clickbait. Must include study authors, institution, journal name, AND a URL
to the paper or a credible writeup. Prefer studies getting traction on academic X, r/science, or in newsletters.
Return 6-10 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("tech_news", f"""Tech industry news from the LAST 48 HOURS — broader than just AI.
Cover: big tech moves (Apple/MS/Google/Meta/Amazon/Nvidia), product launches, antitrust, regulation,
hardware/robotics, semiconductors, fintech, climate tech, defense tech, consumer tech, gaming,
crypto, dev tools. EXCLUDE: enterprise B2B SaaS earnings filler, ETL/data-engineering, vendor PR.
Sources: The Information, Bloomberg, FT, Stratechery, TechCrunch (selectively), Reuters tech, Axios Pro,
The Verge, Ars Technica, Wired, 9to5Mac. Return 10-14 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("ai_frontier", f"""Frontier AI news & analysis from the LAST 7 DAYS — model releases, lab news,
research papers, safety/regulation, infra/chips, AI agents, robotics, breakthrough demos.
Sources: Stratechery, Import AI (Jack Clark), Last Week in AI, The Algorithm (MIT Tech Review),
Interconnects (Nathan Lambert), The Rundown AI, AI Realist, Latent Space (swyx), Hard Fork pod,
Dwarkesh Patel pod, plus primary lab announcements (OpenAI, Anthropic, DeepMind, xAI, Mistral, DeepSeek, Meta AI).
Return 10-14 items. Mix product news, research, and industry analysis.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("startups_funding", f"""Notable startup fundraises, IPO filings, M&A, founder moves from the LAST 7 DAYS.
Bias: consumer AI, AI agents, dev tools, robotics, biotech, defense, fintech, climate, hardware, gaming.
Must include company + amount + lead investor when relevant. Sources: The Information, PitchBook,
TechCrunch funding, Axios Pro Rata, Crunchbase News, Strictly VC. Return 8-12 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("twitter", f"""Viral threads / debates / arguments on X (Twitter) RIGHT NOW (LAST 48 HOURS),
across politics, tech, AI, economics, science, culture, sports — any domain.
For each: WHO started it (handle), the core claim, why it's getting traction (RT/like counts),
and the counter-take if there is one. Prefer threads that are perspective-shifting, contrarian, or capture
a real shift in how people are thinking. URL must link directly to the originating tweet/thread.
Return 8-12 items spanning multiple topic areas.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("reddit", f"""High-engagement Reddit threads from the LAST 48 HOURS across
r/slatestarcodex, r/MachineLearning, r/AskHistorians, r/changemyview, r/TrueReddit, r/AskScience,
r/Economics, r/Philosophy, r/AskAcademia, r/neoliberal, r/movies, r/television, r/books, r/nba, r/soccer,
r/nfl, r/baseball, r/Cricket, r/Formula1, r/tennis, r/Olympics.
For each: subreddit, the question or claim, why the discussion is interesting, the top-voted insight,
upvote count if known. URL must link to the actual Reddit thread. Return 8-12 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("substack", f"""Most interesting Substack essays / newsletter posts published in the LAST 7 DAYS that are
actively circulating (X shares, HN front page, group-chat traction). Writers worth checking:
Noah Smith, Matt Yglesias, Tyler Cowen / Marginal Revolution, Ben Thompson / Stratechery,
Byrne Hobart / The Diff, Patrick McKenzie / Bits about Money, Scott Alexander / Astral Codex Ten,
Erik Hoel, Dan Wang, Anton Howes, Not Boring, One Useful Thing, Slow Boring, Construction Physics,
Works in Progress, The Pull Request, Money Stuff (Matt Levine), The Rundown AI, AI Realist,
Import AI, Last Week in AI, Latent Space, Interconnects. Plus Hacker News top long-reads.
For each: title, author, core argument, why it is circulating, working URL.
SKIP essays you cannot link to. SKIP obscure writers with no public traction signal.
Return 8-12 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("podcasts", f"""Most interesting conversations on podcasts that aired in the LAST 7 DAYS and are
being actively discussed. Look at: Dwarkesh Patel, Conversations with Tyler, Ezra Klein, Lex Fridman,
Hard Fork, Pivot, Acquired, Lenny's Podcast, All-In, BG2, Sharp Tech, Decoder, Plain English, The Daily,
Honestly with Bari Weiss, The Rest is History, Hardcore History, 80,000 Hours, Sean Carroll's Mindscape,
Huberman Lab, Founders, Invest Like the Best, The Town, ChinaTalk, Odd Lots, Bg2 Pod, No Priors,
Latent Space, The TWIML AI Podcast, Practical AI.
For each: podcast name + episode title, guest, the most interesting idea, why it is worth knowing.
URL to the actual episode. Return 8-12 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("culture_buzz", f"""What is EVERYONE actually watching, reading, listening to, or arguing about RIGHT NOW
(THE LAST 7 DAYS — TODAY IS 2026 — do NOT return items from 2023 or 2024)? Dinner-party / group-chat beat. Cover:
- TV: shows people are texting about (HBO, Netflix, Apple TV, FX, Hulu, Peacock, Disney+ — new releases, finales)
- Film: theatrical openings, streaming drops with buzz, viral box office stories, Letterboxd top weekly
- Music: charting albums, viral songs (TikTok-driven hits), tour announcements, artist beefs
- Books: NYT bestseller movements, BookTok hits, major releases, viral essays / memoirs
- Internet culture: a meme that broke through, a celebrity moment, a viral video everyone has seen
- Live events: notable concerts, festivals, awards shows, viral moments
Sources: Vulture, The Ringer, Variety, Hollywood Reporter, Letterboxd, Billboard, Pitchfork, NYT Books,
Vanity Fair, Defector, IndieWire, /r/movies, /r/television, /r/popheads hot threads.
For each: title/artist/show, format, why it is the moment, critical or social signal, URL.
Recency and broad relevance matter MORE than highbrow taste. SKIP obscure prestige stuff nobody is discussing.
Return 10-14 items.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("sports", f"""Comprehensive sports coverage from the LAST 48 HOURS — the major story in EVERY active
league or tournament. Be exhaustive across all of these (skip only the ones with literally no news today):
- NBA (Finals, playoffs, draft, trades, free agency)
- NFL (offseason moves, contracts, draft, training camp, suspensions)
- MLB (standings, no-hitters, trades, contract news)
- NHL (Stanley Cup, free agency, blockbuster trades)
- Soccer — Premier League, La Liga, Bundesliga, Serie A, Champions League/Europa League, MLS, transfer window
- INTERNATIONAL SOCCER tournaments — always check FIFA World Cup, Euros, Copa America, AFCON, Asian Cup,
  Nations League, Olympics soccer if any are active or in qualifying. Report group stage results, knockout
  fixtures, upset wins, breakout players. These tournaments are MUST-CHECK every run.
- F1 (race weekends, driver moves, championship standings)
- Tennis (Grand Slams — always check if Australian Open, French Open, Wimbledon, US Open is active; major upsets)
- Cricket (T20 World Cup, IPL, Test series, ODI tournaments)
- Golf (Majors — Masters, PGA, US Open, Open Championship; Ryder Cup)
- Boxing / MMA (UFC events, major title fights)
- Olympics / Paralympics (if in session)
- College sports (March Madness, College World Series, transfer portal drama, NIL stories)
- Off-field: athlete podcasts, off-field beefs, ownership news, league business
Sources: ESPN, The Athletic, Ringer, Defector, BBC Sport, Sky Sports, FIFA.com, sport-specific Twitter handles.
Return 12-16 items spanning AS MANY leagues/tournaments as have real news today.
{BEAT_OUTPUT_INSTRUCTION}"""),

    ("weird_internet", f"""Weird, unexpected, genuinely interesting internet stories from the LAST 7 DAYS.
Viral phenomena with a sharp underlying story, niche communities doing interesting things, a wild Twitter beef,
a surprising data visualization, a forgotten website that resurfaced, a strange court case.
Things that don't fit neatly into other beats but are worth knowing. URL and recency required.
Return 6-10 items.
{BEAT_OUTPUT_INSTRUCTION}"""),
]

MARKETS_PROMPT_TEMPLATE = """Current market snapshot. Give me:
- For each stock: latest price, day change %, market cap if notable. Tickers: {tickers}
- For each crypto: current USD price, 24h change %. Tickers: {crypto}
- Major US indices (S&P 500, Nasdaq, Dow) latest level and day change %
- One key macro story moving markets today (Fed, oil, inflation, geopolitics) — single sentence

Format as concise structured bullets. Include the data timestamp. If markets are closed, say so."""

def today_preamble(date_obj=None) -> str:
    """Build the date/time context block prepended to every Sonar prompt.

    Forces the model to anchor claims temporally and to avoid stale training data.
    """
    d = date_obj or dt.date.today()
    day_name = d.strftime("%A")
    iso = d.isoformat()
    # Compute relative date markers the model can use in output
    yesterday = (d - dt.timedelta(days=1)).strftime("%A %b %d")
    last_week_start = (d - dt.timedelta(days=7)).strftime("%b %d")
    return (
        f"========== TIME CONTEXT — READ FIRST ==========\n"
        f"TODAY IS {day_name}, {iso}.\n"
        f"Yesterday was {yesterday}. The last 7 days started {last_week_start}.\n"
        f"You MUST search the live web for current information.\n"
        f"DO NOT use information from your training data that contradicts the live web.\n"
        f"When you describe events, anchor them in time explicitly:\n"
        f"  - 'on Monday', 'yesterday', 'this week', 'last Friday', 'three days ago'\n"
        f"  - never narrate stale events as if they happened today\n"
        f"  - if an item is more than 7 days old, say so plainly\n"
        f"=================================================\n\n"
    )


# ─── PERPLEXITY API ──────────────────────────────────────────────────────────

def pplx_chat(prompt: str, model: str = RESEARCH_MODEL, max_tokens: int = 2000,
              system: str | None = None, temperature: float = 0.4,
              retries: int = 3) -> str:
    """Call Perplexity Chat Completions. Returns assistant message text."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = httpx.post(
                PPLX_URL,
                json=payload,
                headers={"Authorization": f"Bearer {PPLX_API_KEY}"},
                timeout=180,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            print(f"[pplx_chat] attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"pplx_chat failed: {last_err}")


def openai_chat(prompt: str, model: str = SCRIPT_MODEL, max_tokens: int = 2000,
                system: str | None = None, temperature: float = 0.4,
                retries: int = 3) -> str:
    """Call OpenAI Chat Completions. Same contract as pplx_chat (returns text)."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = httpx.post(
                OPENAI_CHAT_URL,
                json=payload,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                timeout=180,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            print(f"[openai_chat] attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"openai_chat failed: {last_err}")


def llm_chat(prompt: str, model: str = SCRIPT_MODEL, max_tokens: int = 2000,
             system: str | None = None, temperature: float = 0.4,
             retries: int = 3) -> str:
    """Provider-routing LLM entry point.

    Routes by model id: OpenAI models (``gpt-*`` / ``o1``/``o3``/``o4`` reasoning)
    go to OpenAI; everything else (``sonar*``) goes to Perplexity. This lets each
    stage pick a model by role without caring which provider serves it.
    """
    is_openai = model.startswith(("gpt", "o1", "o3", "o4", "chatgpt"))
    fn = openai_chat if is_openai else pplx_chat
    return fn(prompt, model=model, max_tokens=max_tokens, system=system,
              temperature=temperature, retries=retries)


def _parse_beat_json(raw: str, label: str) -> list[dict[str, Any]]:
    """Best-effort parse of a beat's JSON array of topic dicts.

    Strips code fences, extracts the outermost [...] block, salvages partial
    valid items if the tail is truncated.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("headline")]
    except json.JSONDecodeError:
        pass

    # Salvage: extract every top-level {"headline": ...} object via brace matching
    items: list[dict[str, Any]] = []
    depth = 0
    obj_start = -1
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                blob = cleaned[obj_start:i + 1]
                try:
                    obj = json.loads(blob)
                    if isinstance(obj, dict) and obj.get("headline"):
                        items.append(obj)
                except json.JSONDecodeError:
                    pass
                obj_start = -1
    if items:
        print(f"[research] {label} salvaged {len(items)} items from broken JSON", file=sys.stderr)
    return items


# Beats that carry the news half of the show — an empty result here guts the
# episode, so they get extra retry rounds (a 0 here is ~always a transient Sonar
# or JSON-parse failure, not a genuinely empty news day).
CRITICAL_BEATS = {"geopolitics", "us_politics", "economics", "tech_news",
                  "science", "sports", "culture_buzz"}


def _run_one_beat(label: str, prompt: str, preamble: str) -> list[dict[str, Any]]:
    items = _parse_beat_json(llm_chat(preamble + prompt, RESEARCH_MODEL, 4000), label)
    for it in items:
        it["beat"] = label
    return items


def _wire_http_get(url: str) -> str:
    """Fetch an RSS/Atom feed via httpx (gzip/redirects/TLS; proven on the runner)."""
    r = httpx.get(url, timeout=10.0, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (podcast-newsfeeds)"})
    r.raise_for_status()
    return r.text


def gather_wire_news() -> dict[str, list[dict[str, Any]]]:
    """Deterministic, real-source news from RSS feeds — the trusted news backbone.

    Returns wire_world / wire_us / wire_business beats of REAL, dated articles
    (best-effort). These are grounded primary sources — no LLM discovery — so they
    can't fabricate events like the Sonar beats do.
    """
    print("[wire] fetching real news from trusted RSS feeds…", file=sys.stderr)
    try:
        wire = news_feeds.gather_wire_news(
            _wire_http_get, log=lambda m: print(m, file=sys.stderr))
    except Exception as e:  # noqa: BLE001
        print(f"[wire] fetch failed (non-fatal): {e}", file=sys.stderr)
        return {}
    total = sum(len(v) for v in wire.values())
    print(f"[wire] {total} real articles across {len(wire)} feeds", file=sys.stderr)
    return wire


def gather_research(max_retry_rounds: int = 2) -> dict[str, list[dict[str, Any]]]:
    """Run all wide-net research beats in parallel, RETRYING any that come back empty.

    A beat returning 0 items is almost always a transient API hiccup or an
    unparseable response — not a real lack of news — so we re-run empty beats for
    up to ``max_retry_rounds`` extra rounds. Critical news beats get the same
    retries; without this, a bad Sonar day silently guts hard news / US politics.
    """
    preamble = today_preamble()
    results: dict[str, list[dict[str, Any]]] = {}
    pending: list[tuple[str, str]] = list(BEATS)

    for round_i in range(max_retry_rounds + 1):
        if not pending:
            break
        if round_i:
            labels = ", ".join(l for l, _ in pending)
            print(f"[research] retry round {round_i} for {len(pending)} empty beat(s): {labels}",
                  file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            future_map = {pool.submit(_run_one_beat, label, prompt, preamble): label
                          for label, prompt in pending}
            for fut in concurrent.futures.as_completed(future_map):
                label = future_map[fut]
                try:
                    results[label] = fut.result()
                except Exception as e:
                    print(f"[research] {label} FAILED: {e}", file=sys.stderr)
                    results.setdefault(label, [])
                print(f"[research] {label} ✓ {len(results.get(label, []))} items", file=sys.stderr)
        # Retry whatever is still empty (critical beats first in the log).
        pending = [(l, p) for l, p in BEATS if not results.get(l)]

    still_empty = [l for l in CRITICAL_BEATS if not results.get(l)]
    if still_empty:
        print(f"[research] WARNING: critical beats still empty after retries: {still_empty}",
              file=sys.stderr)
    return results



# ─── REAL MARKET DATA (Yahoo Finance) ────────────────────────────────────────
# Sonar-pro hallucinates commodity prices even at temp=0 with grounding instructions.
# We pull real prices ourselves and feed them into the script as ground truth.

_YAHOO_TICKERS: list[tuple[str, str]] = [
    # (yahoo_symbol, display_label)
    ("^GSPC",     "S&P 500"),
    ("^DJI",      "Dow Jones"),
    ("^IXIC",     "Nasdaq"),
    ("^VIX",      "VIX"),
    ("^TNX",      "10Y Treasury yield"),
    ("DX-Y.NYB",  "Dollar Index"),
    ("BZ=F",      "Brent crude"),
    ("CL=F",      "WTI crude"),
    ("GC=F",      "Gold"),
    ("EURUSD=X",  "EUR/USD"),
    ("USDJPY=X",  "USD/JPY"),
    ("BTC-USD",   "Bitcoin"),
    ("ETH-USD",   "Ethereum"),
    ("SOL-USD",   "Solana"),
]

# Equity watchlist gets the same Yahoo treatment (no =F suffix needed).
_EQUITY_TICKERS = [(t, t) for t in WATCHLIST]


# Build a single proxy-bypassing opener for Yahoo Finance.
# The agent sandbox routes HTTPS through a credential proxy that doesn't
# intercept Yahoo, so we must explicitly disable proxying for these calls.
import urllib.request as _urlreq
_YAHOO_OPENER = _urlreq.build_opener(_urlreq.ProxyHandler({}))


def _yahoo_quote(symbol: str, timeout: float = 8.0) -> dict | None:
    """Fetch one Yahoo quote with chart endpoint. Returns price + pct change or None."""
    import json as _json
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    req = _urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with _YAHOO_OPENER.open(req, timeout=timeout) as r:
            data = _json.loads(r.read())
    except Exception:
        return None
    try:
        meta = data["chart"]["result"][0]["meta"]
    except Exception:
        return None
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        return None
    pct = None
    if prev:
        try:
            pct = (price - prev) / prev * 100.0
        except Exception:
            pct = None
    return {"symbol": symbol, "price": float(price), "prev_close": prev, "pct_change": pct}


def fetch_yahoo_quotes() -> dict[str, dict]:
    """Pull all watchlist + macro tickers from Yahoo Finance in a small thread pool."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pairs = _YAHOO_TICKERS + _EQUITY_TICKERS
    quotes: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        fut_to_label = {ex.submit(_yahoo_quote, sym): (sym, label) for sym, label in pairs}
        for fut in as_completed(fut_to_label):
            sym, label = fut_to_label[fut]
            try:
                q = fut.result()
            except Exception:
                q = None
            if q:
                q["label"] = label
                quotes[sym] = q
    return quotes


def _format_quote(q: dict) -> str:
    """One-line formatted quote for the markets block."""
    price = q["price"]
    pct = q.get("pct_change")
    label = q.get("label", q.get("symbol", "?"))
    # Choose precision: equities/futures w/ small price → 2 decimals; large indices → comma
    if price >= 1000:
        price_str = f"{price:,.2f}"
    elif price >= 10:
        price_str = f"{price:.2f}"
    else:
        price_str = f"{price:.4f}"
    pct_str = f"{pct:+.2f}%" if pct is not None else "(no change data)"
    return f"- {label}: {price_str} ({pct_str})"


def build_real_markets_snapshot() -> str:
    """Live Yahoo-backed market snapshot. Replaces LLM-fabricated prices.

    Sonar-pro consistently invents oil and crypto prices; Yahoo is the source of truth.
    We still call Sonar for ONE optional narrative sentence about today's market mood.
    """
    quotes = fetch_yahoo_quotes()
    if not quotes:
        # Hard fail to Sonar only if Yahoo is totally unreachable.
        return "(live market data unavailable — Yahoo Finance unreachable)"

    # Group sections for readability
    sections: list[tuple[str, list[str]]] = []

    # Indices
    idx_keys = ["^GSPC", "^DJI", "^IXIC", "^VIX"]
    idx_lines = [_format_quote(quotes[k]) for k in idx_keys if k in quotes]
    if idx_lines:
        sections.append(("US indices", idx_lines))

    # Rates / FX
    rate_keys = ["^TNX", "DX-Y.NYB", "EURUSD=X", "USDJPY=X"]
    rate_lines = [_format_quote(quotes[k]) for k in rate_keys if k in quotes]
    if rate_lines:
        sections.append(("Rates & FX", rate_lines))

    # Commodities
    comm_keys = ["BZ=F", "CL=F", "GC=F"]
    comm_lines = [_format_quote(quotes[k]) for k in comm_keys if k in quotes]
    if comm_lines:
        sections.append(("Commodities", comm_lines))

    # Crypto
    crypto_keys = ["BTC-USD", "ETH-USD", "SOL-USD"]
    crypto_lines = [_format_quote(quotes[k]) for k in crypto_keys if k in quotes]
    if crypto_lines:
        sections.append(("Crypto", crypto_lines))

    # Equities (watchlist)
    eq_lines = [_format_quote(quotes[t]) for t in WATCHLIST if t in quotes]
    if eq_lines:
        sections.append(("Equity watchlist", eq_lines))

    body = ""
    for header, lines in sections:
        body += f"\n{header}:\n" + "\n".join(lines) + "\n"

    # Add today date so the writer knows these are fresh
    timestamp = dt.date.today().isoformat()
    header_block = (
        f"LIVE MARKETS SNAPSHOT (Yahoo Finance, fetched {timestamp}):\n"
        f"All prices below are REAL and CURRENT. Use these numbers verbatim. "
        f"Do NOT substitute Sonar/training-data values for these.\n"
    )

    # Optional macro narrative from Sonar — one sentence, sandboxed
    narrative = ""
    try:
        preamble = today_preamble()
        prompt = (
            preamble
            + "Give me ONE short sentence (<=25 words) describing today's overall market mood.\n"
            + "Examples: 'Tech rallied on Nvidia earnings while bonds sold off.'\n"
            + "Do NOT cite specific numbers — those are handled separately. Just the vibe.\n"
            + "Return only the sentence, no prefix, no quotes."
        )
        narrative = llm_chat(prompt, JUDGE_MODEL, 200, temperature=0.3).strip()
        # Strip surrounding quotes if present
        narrative = narrative.strip('"\'').strip()
        if narrative:
            narrative = f"\nMarket vibe (one-liner):\n- {narrative}\n"
    except Exception as e:
        print(f"[markets] narrative call failed (non-fatal): {e}", file=sys.stderr)
        narrative = ""

    return header_block + body + narrative


def gather_markets() -> str:
    """Real market snapshot from Yahoo Finance. Replaces v5 LLM-based fetch.

    Previous version used Sonar-pro which hallucinated commodity prices
    (e.g. Brent at $87 when reality was $73). Yahoo Finance is the source of truth.
    """
    print("[markets] fetching real quotes from Yahoo Finance...", file=sys.stderr)
    snapshot = build_real_markets_snapshot()
    if "unreachable" in snapshot.lower():
        print(f"[markets] WARN: Yahoo Finance unreachable, fallback to Sonar", file=sys.stderr)
        # Fallback to the old Sonar-based fetch
        preamble = today_preamble()
        prompt = (
            preamble
            + MARKETS_PROMPT_TEMPLATE.format(
                tickers=", ".join(WATCHLIST), crypto=", ".join(CRYPTO),
            )
            + "\n\nCRITICAL: For EVERY number you return, it MUST come from a source\n"
            + "published in the last 24 hours. Cite source + time after every number.\n"
        )
        try:  # live-search fallback stays on base sonar (needs the web)
            return llm_chat(prompt, SEARCH_MODEL, 1500, temperature=0.2)
        except Exception as e:
            print(f"[markets] fallback also FAILED: {e}", file=sys.stderr)
            return "(markets data unavailable today)"
    return snapshot


# ─── RECAP MEMORY ────────────────────────────────────────────────────────────

def load_recent_recaps(lookback_days: int = RECAP_LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """Load the last N days of episode recaps from local store.

    Each recap is a JSON file with keys: date, headline, topics, entities, summary.
    Returns sorted oldest-first so the model sees chronological order.
    """
    today = dt.date.today()
    recaps: list[dict[str, Any]] = []
    for delta in range(1, lookback_days + 1):  # skip today, look back
        d = today - dt.timedelta(days=delta)
        path = RECAPS_DIR / f"{d.isoformat()}.json"
        if path.exists():
            try:
                recaps.append(json.loads(path.read_text()))
            except Exception as e:
                print(f"[recap] failed to load {path.name}: {e}", file=sys.stderr)
    recaps.sort(key=lambda r: r.get("date", ""))
    print(f"[recap] loaded {len(recaps)} prior recap(s)", file=sys.stderr)
    return recaps


def format_recaps_for_writer(recaps: list[dict[str, Any]]) -> str:
    """Render recent recaps as a compact context block for the script writer."""
    if not recaps:
        return "(no prior episodes — this is the first one or memory is empty)"

    lines: list[str] = []
    for r in recaps:
        date = r.get("date", "?")
        lines.append(f"\n--- {date} ---")
        headline = r.get("headline")
        if headline:
            lines.append(f"Headline: {headline}")
        topics = r.get("topics") or []
        if topics:
            lines.append("Topics covered:")
            for t in topics:
                lines.append(f"  - {t}")
        entities = r.get("entities") or []
        if entities:
            lines.append(f"Entities mentioned: {', '.join(entities)}")
        summary = r.get("summary")
        if summary:
            lines.append(f"Summary: {summary}")
    return "\n".join(lines)


RECAP_SYSTEM_PROMPT = """You extract structured recaps from podcast scripts for future de-duplication.

Output STRICT JSON only — no prose, no code fences. Schema:
{{
  "headline": "<one-sentence summary of the day's most important thread>",
  "topics": ["<5-12 short topic phrases that capture every distinct story covered, 4-10 words each>"],
  "entities": ["<flat list of all proper nouns mentioned: people, companies, products, tickers, places>"],
  "summary": "<2-4 sentence prose summary of the whole episode>"
}}

Be precise. Topics should be granular enough that another agent could ask 'was X covered?' and get a yes/no answer.
Entities should be canonical names ('OpenAI' not 'open ai', 'NVDA' not 'Nvidia stock')."""


def extract_recap(dialogue: list[dict[str, str]], date_str: str,
                  curated_segments: dict[str, list[dict[str, Any]]] | None = None
                  ) -> dict[str, Any]:
    """Generate a structured recap from today's full script.

    When ``curated_segments`` is supplied (v7 two-pass mode) the recap also gains
    a ``topic_details`` list pairing each topic with the actual narration lines
    that covered it, so future episodes can quote prior framing. The high-level
    headline/topics/entities/summary fields are unchanged.
    """
    # Compact the dialogue into plain prose for the extraction prompt
    transcript_parts: list[str] = []
    for line in dialogue:
        speaker = "Maya" if line["speaker"] == "kore" else "Ben"
        text = re.sub(r"\[[^\]]+\]", "", line["text"]).strip()
        if text:
            transcript_parts.append(f"{speaker}: {text}")
    transcript = "\n".join(transcript_parts)

    # Truncate if absurdly long (Sonar context is generous but we don't need every word)
    if len(transcript) > 60000:
        transcript = transcript[:60000] + "\n\n[transcript truncated]"

    prompt = f"Date: {date_str}\n\nTRANSCRIPT:\n{transcript}\n\nExtract the recap JSON now."
    raw = llm_chat(prompt, model=RECAP_MODEL, max_tokens=3500,
                    system=RECAP_SYSTEM_PROMPT, temperature=0.1)

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start:end + 1]

    try:
        recap = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"[recap] parse failed, returning skeleton: {e}", file=sys.stderr)
        recap = {"headline": "", "topics": [], "entities": [], "summary": "",
                 "_parse_error": str(e), "_raw": raw[:2000]}

    recap["date"] = date_str
    if curated_segments:
        try:
            recap["topic_details"] = editorial.build_topic_details(dialogue, curated_segments)
        except Exception as e:  # noqa: BLE001 — enrichment is non-fatal
            print(f"[recap] topic_details build failed (non-fatal): {e}", file=sys.stderr)
    return recap



# CURATOR (rank -> balance -> assign segments) ------------------------------

# Maps content-type segments to which raw research beats are eligible to land there.
# v5: SEGMENT_BEAT_AFFINITY removed. The curator now routes items by CONTENT fit, not by beat tag.
# Beats are still tagged in the candidate pool for the curator's information, but cross-beat assignment
# is fine (e.g. a tech_news item belongs in tech_business; a podcast about geopolitics can land in
# arguments OR world). Affinity-based reroute logic in curator was brittle and is gone.


# Target topic count per segment. The curator tries to hit these.
SEGMENT_TARGET_TOPICS: dict[str, int] = {
    "hard_news": 5,           # major US + world stories with context
    "politics":  4,           # competing views + implications
    "economy":   4,           # macro, markets, companies (real Yahoo prices, see overlay)
    "tech_ai":   4,           # CAPPED — builders/infra/policy, not model-release chatter
    "science":   5,           # two deep dives (one practical, one mind-bending) + extras
    "feature":   4,           # longform essays / interviews / podcast ideas (buzzing routes here)
    "culture":   5,           # TV/film/music/internet that crossed into the conversation
    "sports":    5,           # narrative arcs across leagues, concise
}
# cold_open (headlines scan), disagreement (one real adversarial pair), and the
# outro are synthesized from the curated picks in generate_script, not curated
# directly, so they are intentionally absent here.

CURATOR_SYSTEM_PROMPT = """You are the EDITOR of a daily 2-hour, broadly-knowledgeable podcast briefing.
Your ONLY job is curation, ranking, and cross-domain balance. You do NOT write dialogue.

THE LISTENER
A smart generalist who wants to be conversationally fluent and argument-literate across politics,
business, economics, technology, AI, science, culture, and sports. The goal is to be able to talk
with almost anyone about almost anything — not to go deep in one vertical. He wants to know not just
WHAT happened but what thoughtful people are DEBATING, which cultural objects people are talking about,
and which narratives matter. He is already saturated with AI/tech at work, so the show must be a
DIVERSIFIED media diet, explicitly NOT dominated by AI or technology.
He does NOT want: enterprise SaaS / ETL bias, endless model-release chatter, OR obscure literary
essays from outlets like NYRB / LRB / Aeon / niche academic journals.

YOUR JOB
You receive ~140 candidate topics across many beats. Pick the BEST ones with strict cross-domain
balance and route them to the right segment BY CONTENT, not by which beat they came from.
The show has these segments with target topic counts:
{segment_targets}

CONTENT ROUTING (the most important rule)
Route topics to segments by what they ARE ABOUT, not by which beat returned them. For example:
- A major breaking US/world story belongs in HARD_NEWS.
- A piece arguing competing views on a policy/election belongs in POLITICS.
- A markets/macro/company/earnings story belongs in ECONOMY.
- A builders/infra/chips/AI-policy/AI-safety story belongs in TECH_AI.
- A new paper or mind-bending finding belongs in SCIENCE.
- A standout longform essay, interview, or podcast idea (the day's best single argument) belongs in FEATURE.
- A TV/film/music/internet-culture story everyone is talking about belongs in CULTURE.
- A sports narrative (rivalry, star, standings, strategy) belongs in SPORTS.
The `beat` tag on each item is INFORMATIONAL ONLY — cross-routing is expected and encouraged.

TRUSTED WIRE ITEMS ARE THE NEWS BACKBONE
Some candidates are marked `"trusted": true` (beats starting with `wire_`). These are REAL,
timestamped articles pulled directly from major newsrooms' feeds (BBC, NPR, Guardian, NYT, Al Jazeera,
plus Reuters/AP via aggregation) — each has a genuine headline, working URL, and `published` date, so
its event is GUARANTEED to have actually happened on that date. The other candidates come from an
open-ended AI research pass and can occasionally hallucinate or resurface a year-old event as if fresh.
Therefore:
- For HARD_NEWS, POLITICS, and ECONOMY, PREFER trusted wire items. When a wire item and a non-trusted
  item cover the same story, keep the WIRE one (its URL/date are verified).
- Build HARD_NEWS primarily from `wire_*` items. Only fall back to non-trusted research for a hard-news
  slot when no wire item covers that story.
- A non-trusted item making a strong dated claim ("passed Wednesday", "overnight", "this week") that NO
  wire item corroborates is suspect — deprioritize or drop it rather than lead with it.
- You may still use non-trusted research freely for TECH_AI, SCIENCE, FEATURE, CULTURE, and SPORTS.

NO FORUM FILLER IN THE NEWS SEGMENTS
Reddit / forum discussion threads and "Ask<X>" threads (e.g. r/changemyview, AskHistorians, AskAcademia)
are NOT news and NOT political reporting. NEVER place them in HARD_NEWS or POLITICS. They may appear in
CULTURE (if a genuinely viral internet moment) or FEATURE (if a substantive argument) — otherwise drop them.
HARD_NEWS and POLITICS must be built from real REPORTING (named outlets, datelines), not chatter.

ONGOING MAJOR STORIES GET DAILY UPDATES
An active war, conflict, or fast-moving crisis (e.g. the ongoing Iran war / Strait of Hormuz, Ukraine)
is the OPPOSITE of a repeat — each day it develops, give it a FRESH HARD_NEWS slot with the latest
development, framed as continuing coverage. Do not drop a still-escalating story just because it appeared
in a recent episode.

DON'T RE-LEAD THE SAME DATA RELEASE
A single macro release (a given month's PCE / CPI / jobs report) should be covered ONCE. If a recent
episode already led ECONOMY with this month's inflation print, do NOT make the same print the economy
lead again — pick a fresh economy angle (a different release, a company/sector story, a market move).

BALANCE RULES — non-negotiable
DAILY MINIMUMS: at least 2 substantial politics/world stories (across HARD_NEWS + POLITICS);
at least 1 economics/business/markets story; at least 1 tech/AI story; at least 2 science stories
when possible (one practical/near-term, one mind-bending); at least 1 substantial culture story;
at least 1 sports narrative arc.
DAILY CAPS — enforce these strictly:
- TECH & AI is CAPPED. Pick at most 4 TECH_AI topics, and NO MORE THAN 3 of them may be AI stories.
  Do not let AI/tech dominate the episode — when in doubt, cut an AI item, not a non-AI one.
- SPORTS stays concise (≤5 topics) but engaging; spread across DIFFERENT leagues/tournaments.
- FEATURE is ONE day's best longform argument plus a few alternates — depth over breadth.

HARD RULES — non-negotiable
- VERIFIABILITY: Every kept item must have a real working URL (`url` field non-empty). If the item
  also has a `published` date, it must be within the last 14 days. DROP fabricated/unverifiable items.
- NO OBSCURE OUTLETS: skip NYRB, LRB, Aeon, Public Humanities, niche academic journals, obscure
  literary blogs. Mainstream-prestige is fine (Atlantic, New Yorker, Stratechery, Bloomberg, FT, NYT,
  WaPo, Economist, Reuters, AP, BBC, Vox, Foreign Affairs, Semafor, Politico, Substack-with-traction).
- IDEOLOGICAL PLURALITY: for POLITICS and FEATURE, prefer a mix of institutional/mainstream reporting
  AND serious heterodox voices from both the left and center-right. Avoid one-note framing.
- DEDUPLICATE against recent recaps. The "RECENT EPISODES" block lists every topic phrase and
  headline covered in the last 7 days. If a candidate item refers to the SAME story, person, study,
  research finding, or company as a prior topic — even if the candidate's wording differs — DROP IT
  unless there is a CONCRETE NEW DEVELOPMENT (new data, new actor, new ruling, new price, new fix).
  If you keep an item as an update, you MUST add an "update_note" field naming the specific new fact.
  Items WITHOUT an update_note that overlap a prior recap WILL be removed by a downstream check.

RANKING CRITERIA (in order)
1. Impact: does the story matter in the real world?
2. Conversation value: are many smart/influential people talking about it (the `buzz` field)?
3. Diversity: does it expand the topical and ideological mix of the episode?
4. Novelty: genuinely fresh, or framed in an original way?
5. Narrative quality: conflict, stakes, personality, beauty, surprise, strategic intrigue?
6. Source quality: grounded in strong reporting, thoughtful essays, or credible expert interpretation?

OUTPUT FORMAT — STRICT JSON ONLY
Return ONLY this JSON object, no prose, no fences:
{{
  "segments": {{
    "hard_news": [<pick objects>],
    "politics":  [<pick objects>],
    "economy":   [<pick objects>],
    "tech_ai":   [<pick objects>],
    "science":   [<pick objects>],
    "feature":   [<pick objects>],
    "culture":   [<pick objects>],
    "sports":    [<pick objects>]
  }},
  "editor_note": "<1-2 sentences on the day's overall vibe / theme>"
}}

Each pick object is SMALL — just identify the topic by its headline:
{{
  "headline":    "<exact headline copied verbatim from input>",
  "rank":        <integer 1=top of segment>,
  "update_note": "<short note ONLY if this is a meaningful update to a prior episode topic; else OMIT this key>"
}}

CRITICAL: The "headline" field must MATCH verbatim against one of the candidate headlines so we can look it up.
DO NOT paraphrase headlines. DO NOT make up new headlines.
"""


def _normalize_pool(research: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Flatten beat-keyed research to a single list. Detail/angle truncated for curator efficiency."""
    flat: list[dict[str, Any]] = []
    for beat, items in research.items():
        for it in items:
            entry = {
                "headline": (it.get("headline") or "")[:200],
                "detail":   (it.get("detail") or "")[:280],
                "angle":    (it.get("angle") or "")[:160],
                "source":   (it.get("source") or "")[:80],
                "url":      it.get("url") or "",
                "beat":     beat,
            }
            # Surface the grounded-source markers so the editor can prefer wire items.
            if it.get("trusted"):
                entry["trusted"] = True
            if it.get("published"):
                entry["published"] = it["published"]
            flat.append(entry)
    return flat


# Cap the curator's candidate pool. An oversized prompt makes Perplexity 400 (seen
# at ~122 candidates + a big recap; adding the wire beats pushed it to 169 and the
# curator silently fell back to deterministic routing). Keep ALL trusted wire items
# (the grounded backbone) and cap the AI-discovery beats per-beat, then bound total.
CURATOR_POOL_MAX = 120
CURATOR_PER_BEAT_CAP = 7


def _cap_pool(pool: list[dict[str, Any]], *, max_total: int = CURATOR_POOL_MAX,
              per_beat_cap: int = CURATOR_PER_BEAT_CAP) -> list[dict[str, Any]]:
    """Trim the candidate pool for the curator prompt without dropping wire items."""
    if len(pool) <= max_total:
        return pool
    wire = [p for p in pool if p.get("trusted")]
    rest = [p for p in pool if not p.get("trusted")]
    seen: dict[str, int] = {}
    capped_rest: list[dict[str, Any]] = []
    for p in rest:  # keep each AI beat's first N (research returns them roughly ranked)
        b = p.get("beat", "")
        if seen.get(b, 0) < per_beat_cap:
            capped_rest.append(p)
            seen[b] = seen.get(b, 0) + 1
    combined = wire + capped_rest  # wire first so the global cap never trims it
    return combined[:max_total]


def curate_topics(research: dict[str, list[dict[str, Any]]],
                  recap_block: str,
                  date_str: str) -> dict[str, Any]:
    """Run the curator. Returns {segments: {seg_key: [topic, ...]}, editor_note: str}.

    On failure, falls back to a deterministic rule-based curation so the show still ships.
    """
    pool = _normalize_pool(research)
    raw_n = len(pool)
    pool = _cap_pool(pool)
    if len(pool) < raw_n:
        n_wire = sum(1 for p in pool if p.get("trusted"))
        print(f"[curator] pool capped {raw_n}→{len(pool)} (kept all {n_wire} wire items) "
              "to stay under the prompt-size limit", file=sys.stderr)
    print(f"[curator] {len(pool)} candidate topics from {len(research)} beats", file=sys.stderr)
    if not pool:
        return {"segments": {k: [] for k in SEGMENT_TARGET_TOPICS}, "editor_note": "(no research)"}

    target_lines = "\n".join(
        f"  - {seg}: target {n} topics"
        for seg, n in SEGMENT_TARGET_TOPICS.items()
    )
    system = CURATOR_SYSTEM_PROMPT.format(segment_targets=target_lines)

    # The recap block is only a dedup hint here; cap it so the curator prompt stays
    # within the model's input budget (an oversized prompt 400s and, before this
    # fallback existed, crashed the whole run). The full block still goes to the writer.
    recap_hint = recap_block if len(recap_block) <= 6000 else recap_block[:6000] + "\n…(truncated)"

    user = (
        today_preamble()
        + f"Today's date for routing/dedupe purposes: {date_str}\n\n"
        f"RECENT EPISODES (last {RECAP_LOOKBACK_DAYS} days) - use for de-dup / updates:\n"
        f"{recap_hint}\n\n"
        f"CANDIDATE TOPIC POOL ({len(pool)} items):\n"
        f"{json.dumps(pool, ensure_ascii=False)}\n\n"
        "Curate now. Output the JSON object only."
    )

    try:
        raw = llm_chat(user, model=CURATOR_MODEL, max_tokens=8000,
                        system=system, temperature=0.4)
    except Exception as e:  # noqa: BLE001 — the show must ship even if the curator API fails
        print(f"[curator] API call failed ({e}); using deterministic fallback curation",
              file=sys.stderr)
        return _fallback_curate(research)

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e != -1:
        cleaned = cleaned[s:e + 1]

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as ex:
        print(f"[curator] JSON parse failed: {ex}. Falling back to deterministic curation.",
              file=sys.stderr)
        return _fallback_curate(research)

    # Build headline -> full topic lookup from the original pool (with full detail intact)
    lookup: dict[str, dict[str, Any]] = {}
    for beat, items in research.items():
        for it in items:
            h = (it.get("headline") or "").strip()
            if h:
                lookup[h.lower()[:200]] = {**it, "beat": beat}

    def hydrate(pick: dict[str, Any]) -> dict[str, Any] | None:
        h = (pick.get("headline") or "").strip().lower()[:200]
        if not h:
            return None
        # Try exact, then prefix (60 chars), then fuzzy substring
        if h in lookup:
            base = lookup[h]
        else:
            base = None
            short = h[:60]
            for k, v in lookup.items():
                if k.startswith(short) or short in k:
                    base = v
                    break
        if base is None:
            return None
        hydrated = {**base}
        if "rank" in pick:
            hydrated["rank"] = pick["rank"]
        if pick.get("update_note"):
            hydrated["update_note"] = pick["update_note"]
        return hydrated

    raw_segs = parsed.get("segments", {})
    segs: dict[str, list[dict[str, Any]]] = {}
    for k in SEGMENT_TARGET_TOPICS:
        picks = raw_segs.get(k, [])
        hydrated_list: list[dict[str, Any]] = []
        for p in picks:
            h = hydrate(p)
            if h:
                hydrated_list.append(h)
            else:
                print(f"[curator] warning: {k} pick '{(p.get('headline') or '')[:60]}' did not match pool",
                      file=sys.stderr)
        segs[k] = hydrated_list

    # Hard cap: TECH_AI must not dominate. Trim to 4 (target) even if the LLM over-picks.
    if len(segs.get("tech_ai", [])) > 4:
        segs["tech_ai"] = segs["tech_ai"][:4]
        print("[curator] enforced tech_ai cap (trimmed to 4)", file=sys.stderr)

    # URL verification — the ONLY hard filter. Date is preferred but not required.
    import datetime as _dt
    today = _dt.date.today()
    max_age = _dt.timedelta(days=14)
    for k in list(segs.keys()):
        kept = []
        dropped = 0
        for it in segs[k]:
            url = (it.get("url") or "").strip()
            pub = (it.get("published") or "").strip()
            if not url.startswith("http"):
                dropped += 1
                continue
            # If date is present, it must be within max_age. If absent, trust the URL.
            if pub:
                try:
                    d = _dt.date.fromisoformat(pub[:10])
                    if today - d > max_age:
                        dropped += 1
                        continue
                except Exception:
                    pass  # unparseable date — keep on URL strength
            kept.append(it)
        if dropped:
            print(f"[curator] {k} dropped {dropped} item(s) for missing URL or stale date",
                  file=sys.stderr)
        segs[k] = kept

    total = sum(len(v) for v in segs.values())
    print(f"[curator] curated {total} topics across {len(segs)} segments", file=sys.stderr)
    for k, v in segs.items():
        print(f"[curator]   {k}: {len(v)} topics", file=sys.stderr)
    return {"segments": segs, "editor_note": parsed.get("editor_note", "")}


# v5 fallback uses content-keyword heuristics since affinity mapping is gone.
_FALLBACK_SEGMENT_KEYWORDS: dict[str, list[str]] = {
    "hard_news":  ["breaking", "killed", "attack", "war", "russia", "ukraine", "israel", "gaza",
                   "iran", "summit", "sanctions", "nato", "treaty", "coup", "protest", "disaster"],
    "politics":   ["election", "senate", "congress", "president", "policy", "supreme court", "ruling",
                   "campaign", "vote", "legislation", "governor", "primary", "immigration"],
    "economy":    ["inflation", "gdp", "fed", "central bank", "jobs", "unemployment", "cpi", "rates",
                   "yields", "treasury", "currency", "trade", "tariff", "oil", "energy", "earnings", "ipo"],
    "tech_ai":    ["apple", "google", "microsoft", "meta", "amazon", "nvidia", "openai", "anthropic",
                   "deepmind", "xai", "ai", "model", "chip", "semiconductor", "startup", "funding",
                   "raises", "acquires", "antitrust", "data center"],
    "science":    ["study", "researchers", "paper", "nature", "science", "physics", "neuroscience",
                   "climate", "astronomy", "discovery", "experiment", "biotech", "clinical", "fda", "space"],
    "feature":    ["argues", "essay", "thread", "discusses", "debate", "podcast", "interviews",
                   "conversation", "critique", "case for", "case against", "longform", "profile"],
    "culture":    ["movie", "film", "tv", "series", "season", "album", "song", "book", "tiktok",
                   "viral", "netflix", "hbo", "billboard", "bestseller", "memoir", "meme"],
    "sports":     ["nba", "nfl", "mlb", "nhl", "soccer", "football", "world cup", "euros", "champions",
                   "tennis", "wimbledon", "grand slam", "f1", "ferrari", "olympics", "cricket", "golf",
                   "draft", "signed", "free agent"],
}

def _fallback_curate(research: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Deterministic curation if the LLM curator fails.

    Routes each item by simple keyword match on headline+detail. Best-effort only —
    designed to avoid total failure, not be smart.
    """
    segs: dict[str, list[dict[str, Any]]] = {k: [] for k in SEGMENT_TARGET_TOPICS}
    used: set[str] = set()

    # Flatten all items with beat tag
    all_items: list[dict[str, Any]] = []
    for beat, items in research.items():
        for it in items:
            all_items.append({**it, "beat": beat})

    # Score each item against each segment by keyword overlap
    for it in all_items:
        h = (it.get("headline","") + " " + it.get("detail","")).lower()
        best_seg = None
        best_score = 0
        for seg_key, kws in _FALLBACK_SEGMENT_KEYWORDS.items():
            score = sum(1 for kw in kws if kw in h)
            if score > best_score:
                best_score = score
                best_seg = seg_key
        if best_seg is None:
            best_seg = "hard_news"
        key = it.get("headline","")[:80]
        if key in used:
            continue
        target = SEGMENT_TARGET_TOPICS.get(best_seg, 4)
        if len(segs[best_seg]) < target:
            used.add(key)
            segs[best_seg].append({**it, "rank": len(segs[best_seg]) + 1})

    # Top up the feature segment with leftovers so longform has alternates.
    leftovers = [it for it in all_items if it.get("headline","")[:80] not in used][:4]
    for it in leftovers:
        segs["feature"].append({**it, "rank": len(segs["feature"]) + 1})

    return {"segments": segs, "editor_note": "(fallback curation - LLM curator failed)"}


# ─── PROGRAMMATIC DEDUPE (post-curator hard check) ───────────────────────────

def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


# Common country / region / agency tokens that overlap across unrelated stories.
# Dedupe must NOT treat shared country names as evidence of duplicate content.
_DEDUPE_STOP_ENTITIES = {
    "china", "chinese", "u.s.", "us", "united", "states", "america", "american",
    "russia", "russian", "iran", "iranian", "israel", "israeli", "european",
    "europe", "uk", "u.k.", "britain", "british", "india", "indian", "germany",
    "german", "france", "french", "japan", "japanese", "korea", "korean",
    "ukraine", "ukrainian", "gaza", "lebanon", "fed", "ecb", "imf", "un",
    "nato", "eu", "g7", "opec", "washington", "beijing", "moscow", "kyiv",
    "tehran", "brussels", "paris", "london", "tokyo", "the", "this",
    "today", "yesterday", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "january", "february", "march", "april",
    "may", "june", "july", "august", "september", "october", "november", "december",
}


def _entity_set(text: str) -> set[str]:
    """Cheap entity proxy: capitalized words and quoted tokens — minus generic geo/time tokens
    that would create spurious overlap between unrelated stories."""
    if not text:
        return set()
    toks = re.findall(r"\b[A-Z][A-Za-z0-9'.-]{2,}\b", text)
    return {t.lower() for t in toks if t.lower() not in _DEDUPE_STOP_ENTITIES}


def _similarity(a: str, b: str) -> float:
    """0..1 similarity between two strings using rapidfuzz when available,
    else a cheap token-overlap fallback."""
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return 0.0
    if _rf_fuzz is not None:
        return max(_rf_fuzz.token_set_ratio(a, b), _rf_fuzz.partial_ratio(a, b)) / 100.0
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta | tb), 1)


def programmatic_dedupe(curated: dict[str, Any],
                        recaps: list[dict[str, Any]],
                        date_str: str,
                        sim_threshold: float = 0.55,
                        entity_threshold: float = 0.5,
                        cold_open_strict_days: int = 3) -> dict[str, Any]:
    """Hard, deterministic dedupe against the last N days of recaps.

    Drops items that overlap a prior topic UNLESS they carry an `update_note`.
    For cold_open we use a stricter threshold and a shorter recency window —
    cold open repeats are the most visible failure mode.
    """
    if not recaps:
        return curated

    today_obj = dt.date.fromisoformat(date_str)

    # Build prior-topic index with per-item recency
    prior_topics: list[tuple[str, set[str], int]] = []  # (text, entities, days_ago)
    for r in recaps:
        try:
            r_date = dt.date.fromisoformat(r.get("date", ""))
            days_ago = (today_obj - r_date).days
        except Exception:
            days_ago = 99
        if r.get("headline"):
            prior_topics.append((r["headline"], _entity_set(r["headline"]), days_ago))
        for t in r.get("topics", []) or []:
            prior_topics.append((t, _entity_set(t), days_ago))

    segments_in = curated.get("segments", {})
    segments_out: dict[str, list[dict[str, Any]]] = {}
    dropped_log: list[str] = []

    for seg_name, items in segments_in.items():
        keepers: list[dict[str, Any]] = []
        is_cold = seg_name == "cold_open"
        for it in (items or []):
            headline = it.get("headline", "")
            update_note = (it.get("update_note") or "").strip()
            item_ents = _entity_set(headline)
            worst_match = None
            for prior_text, prior_ents, days_ago in prior_topics:
                if is_cold and days_ago > cold_open_strict_days:
                    continue
                sim = _similarity(headline, prior_text)
                ent_overlap = (
                    len(item_ents & prior_ents) / max(len(item_ents | prior_ents), 1)
                    if (item_ents or prior_ents) else 0.0
                )
                # Require BOTH headline similarity AND entity overlap to avoid
                # false positives where two unrelated stories share a country name.
                # Cold open uses lower thresholds because repeats there are highly visible.
                if is_cold:
                    hit = (sim >= 0.45 and ent_overlap >= 0.30) or sim >= 0.70
                else:
                    hit = (sim >= sim_threshold and ent_overlap >= entity_threshold) or sim >= 0.80
                if hit:
                    if (worst_match is None) or sim > worst_match[1]:
                        worst_match = (prior_text, sim, days_ago, ent_overlap)
            if worst_match and not update_note:
                pt, sim, days_ago, eo = worst_match
                dropped_log.append(
                    f"  [dropped {seg_name}] '{headline[:80]}' "
                    f"~ '{pt[:60]}' (sim={sim:.2f} ent={eo:.2f} {days_ago}d ago)"
                )
                continue
            if is_cold and worst_match and update_note:
                # cold open even with update_note: kick to feature as safety
                pt, sim, days_ago, eo = worst_match
                dropped_log.append(
                    f"  [demoted cold_open→feature] '{headline[:80]}' "
                    f"~ '{pt[:60]}' sim={sim:.2f} {days_ago}d ago"
                )
                segments_out.setdefault("feature", []).append(it)
                continue
            keepers.append(it)
        segments_out[seg_name] = keepers

    if dropped_log:
        print(f"[hard-dedupe] dropped/demoted {len(dropped_log)} items:", file=sys.stderr)
        for line in dropped_log:
            print(line, file=sys.stderr)
    else:
        print("[hard-dedupe] no overlaps found", file=sys.stderr)

    # Cold-open rescue: if dedupe emptied cold_open, promote the highest-ranked
    # un-deduped item from a content segment so the writer has something fresh.
    if not segments_out.get("cold_open"):
        for donor in ("hard_news", "science", "tech_ai", "feature", "culture"):
            picks = segments_out.get(donor) or []
            if picks:
                lifted = dict(picks[0])
                lifted["lifted_from"] = donor
                segments_out["cold_open"] = [lifted]
                print(f"[hard-dedupe] cold_open empty after dedupe — "
                      f"promoted top {donor} item: '{lifted.get('headline','')[:80]}'",
                      file=sys.stderr)
                break

    curated["segments"] = segments_out
    return curated


# ─── NEWS FLOOR (hard_news must never be empty/filler) ───────────────────────
# Beats that produce real reporting vs. discussion-thread filler. hard_news and
# politics must be built from reporting, never from Reddit/forum chatter.
# wire_* beats (real RSS articles) come FIRST so the news floor prefers grounded,
# primary-source reporting over Sonar-discovered items when topping up hard news.
_NEWS_BEATS = ["wire_world", "wire_us", "wire_business",
               "geopolitics", "us_politics", "economics", "tech_news"]
_DISCUSSION_BEATS = {"reddit", "weird_internet"}
MIN_HARD_NEWS = 3


def enforce_news_floor(curated: dict[str, Any], research: dict[str, list[dict[str, Any]]],
                       min_hard_news: int = MIN_HARD_NEWS) -> dict[str, Any]:
    """Guarantee hard_news/politics are real reporting, never empty or forum filler.

    Two failure modes seen in production: (1) research beats came back empty so
    the curator backfilled hard_news with weak business items and politics with
    Reddit threads; (2) the curator routed all political stories into politics and
    left hard_news empty → a "no hard news today" skip line. This strips
    discussion-beat filler from hard_news, tops it up from unused news-beat items,
    and reinforces politics with real political reporting if it went empty.
    """
    segs = curated.setdefault("segments", {})
    log: list[str] = []

    def hl(it: dict[str, Any]) -> str:
        return (it.get("headline") or "").strip().lower()[:80]

    def placed_set() -> set[str]:
        return {hl(it) for items in segs.values() for it in (items or []) if hl(it)}

    def unused_news(beats: list[str], placed: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for beat in beats:
            for it in research.get(beat, []) or []:
                if hl(it) and hl(it) not in placed:
                    out.append({**it, "beat": beat})
        return out

    # 1) hard_news: drop discussion filler, then top up from real news beats.
    hn = [it for it in (segs.get("hard_news") or []) if it.get("beat") not in _DISCUSSION_BEATS]
    segs["hard_news"] = hn
    placed = placed_set()
    if len(hn) < min_hard_news:
        for cand in unused_news(_NEWS_BEATS, placed):
            if len(hn) >= min_hard_news:
                break
            if hl(cand) in placed:
                continue
            hn.append(cand)
            placed.add(hl(cand))
        log.append(f"[news-floor] hard_news topped up to {len(hn)} from news beats")

    # 2) politics: if empty or all-discussion, inject real political reporting.
    pol = segs.get("politics") or []
    real_pol = [it for it in pol if it.get("beat") not in _DISCUSSION_BEATS]
    if not real_pol:
        injected = 0
        for cand in unused_news(["wire_us", "wire_world", "us_politics", "geopolitics"], placed_set()):
            pol.insert(injected, cand)  # real reporting goes first, filler stays as backup
            injected += 1
            if injected >= 3:
                break
        segs["politics"] = pol
        if injected:
            log.append(f"[news-floor] politics reinforced with {injected} real-reporting item(s)")

    if not segs.get("hard_news"):
        log.append("[news-floor] WARNING: hard_news still empty — no news-beat items available")
    for line in log:
        print(line, file=sys.stderr)
    return curated


# ─── CURRENCY CHECK (drop stale/anniversary items before writing) ─────────────
# The research beats occasionally hallucinate a prior-year event as brand-new —
# e.g. Sonar reported the One Big Beautiful Bill Act (passed July 2025) as if the
# House "muscled it through late Wednesday" in 2026, on its anniversary. This
# verifies the top news items are actually recent and drops confirmed-stale ones
# BEFORE they're written (the news floor then backfills).

CURRENCY_SYSTEM_PROMPT = """You guard a daily news show against STALE or ANNIVERSARY stories being
presented as brand-new. For each numbered news item, use live web search to find when the described
event ACTUALLY happened, then judge it against today's date.

THE KEY FAILURE TO CATCH: a famous law, ruling, election, or deal from a PRIOR YEAR resurfacing today
(often on its ANNIVERSARY) and mistaken for a fresh event. Check the ORIGINAL date and YEAR. Example:
the "One Big Beautiful Bill Act" passed/was signed in JULY 2025 — a 2026 claim that it just passed is STALE.

Output STRICT JSON only — no prose, no fences:
{"items": [{"id": <n>, "actually_happened": "<YYYY-MM, 'last few days', or 'unknown'>",
            "verdict": "recent" | "stale" | "uncertain", "reason": "<one sentence>"}]}
"recent" = the core event happened within ~10 days of today. "stale" = it actually happened materially
earlier (a prior month or YEAR / an anniversary). Only say "stale" with web evidence — but a wrong YEAR
is always stale. When unsure, say "uncertain" (we keep those)."""


def currency_check_news(curated: dict[str, Any], date_str: str,
                        segments: tuple[str, ...] = ("hard_news", "politics")) -> dict[str, Any]:
    """Drop curated news items whose core event is actually from a prior period/year.

    Best-effort and non-fatal. Only drops items the verifier marks 'stale' with
    confidence; 'uncertain' items are kept.
    """
    segs = curated.get("segments", {})
    items: list[dict[str, Any]] = []
    for seg in segments:
        # Trusted wire items carry a real publish date, so the anniversary/stale
        # trap can't apply — skip them (they're the antidote, not a suspect).
        items.extend(it for it in (segs.get(seg, []) or []) if not it.get("trusted"))
    if not items:
        return curated

    numbered = "\n".join(
        f'{n+1}. "{it.get("headline", "")}" — {(it.get("detail") or "")[:160]}'
        for n, it in enumerate(items))
    prompt = (today_preamble(dt.date.fromisoformat(date_str)) +
              f"Today is {date_str}. For each news item decide if its core event is actually from the "
              f"last few days or is older / an anniversary / a prior-year event:\n{numbered}\n\n"
              "Return the JSON now.")
    try:
        raw = llm_chat(prompt, model=JUDGE_MODEL, max_tokens=2000,
                        system=CURRENCY_SYSTEM_PROMPT, temperature=0.1)
    except Exception as e:  # noqa: BLE001
        print(f"[currency] check failed (non-fatal): {e}", file=sys.stderr)
        return curated

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e != -1:
        cleaned = cleaned[s:e + 1]
    try:
        verdicts = json.loads(cleaned).get("items", [])
    except json.JSONDecodeError:
        return curated

    stale_headlines: set[str] = set()
    for v in verdicts:
        try:
            idx = int(v.get("id", 0)) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items) and (v.get("verdict") or "").lower() == "stale":
            hl = items[idx].get("headline", "")
            stale_headlines.add(hl)
            print(f"[currency] DROP stale: '{hl[:70]}' — actually {v.get('actually_happened','?')}"
                  f" ({v.get('reason','')[:80]})", file=sys.stderr)
    if stale_headlines:
        for seg in segments:
            segs[seg] = [it for it in (segs.get(seg, []) or [])
                         if it.get("headline", "") not in stale_headlines]
        curated["segments"] = segs
    else:
        print("[currency] all news items check out as recent", file=sys.stderr)
    return curated


# ─── SCRIPT NUMERIC FACT-CHECK ───────────────────────────────────────────────

_NUM_CLAIM_PATTERNS = [
    # $X.XX, $X billion, $X trillion, etc.
    r"\$\d{1,4}(?:[,.]\d+)*\s*(?:billion|trillion|million|m|bn|b|t|k|per barrel|/bbl)?",
    # X.X%   X percent
    r"\d{1,4}(?:\.\d+)?\s*(?:%|percent)",
    # plain price-like with unit: "87 a barrel", "142 per share"
    r"\d{1,4}(?:\.\d+)?\s+(?:a\s+)?(?:barrel|per barrel|per share|cents|bps|basis points)",
    # Sports scorelines: "3-0", "2-1", "108-102" — surfaces both sides of the dash
    # Bound with word boundaries so we don't match things like "2026-06-25" dates.
    # Negative lookbehind/lookahead for digits/dashes avoids ISO dates like 2026-06-25
    r"(?<![\d-])\d{1,3}\u2011\d{1,3}(?![\d-])",   # unicode non-breaking hyphen
    r"(?<![\d-])\d{1,3}-\d{1,3}(?![\d-])",         # ascii hyphen
    # Counts with explicit "X people / X dead / X killed / X injured / X arrested"
    r"\b\d{1,3}(?:,\d{3})*\s+(?:people|dead|killed|injured|wounded|arrested|deaths|casualties)\b",
    # Ages and tenure: "42-year-old", "served 18 years"
    r"\b\d{1,3}-year-old\b",
]


# We used to skip the whole economy segment from fact-check to protect the live
# Yahoo prices — but that also shielded hallucinated MACRO claims (e.g. core PCE
# "2.6%" when it was really 3.4%). Instead we now fact-check every segment and
# protect only the exact ground-truth NUMBERS we injected (Yahoo prices + FRED
# macro), so prices are safe while PCE/CPI/Fed claims still get verified.

def _norm_num(tok: str) -> str:
    """Normalize a numeric token for comparison: drop $, commas, %, sign, spaces."""
    return re.sub(r"[^0-9.]", "", tok)


def _to_float(tok: str) -> float | None:
    n = _norm_num(tok)
    if not n or n == ".":
        return None
    try:
        return float(n)
    except ValueError:
        return None


def _ground_truth_numbers(*texts: str) -> tuple[set[str], list[float]]:
    """Numbers in the injected ground-truth blocks (Yahoo prices + FRED macro).

    Returns (exact_set, large_floats). exact_set protects decimals/4+ digit values
    by literal match. large_floats (values >= 10 — prices, index levels) are also
    fuzzy-matched, because the writer rounds them ("$74" for 73.50, "$4,045" for
    4045.30) and an exact-only check let the verifier hallucinate "corrections"
    (it once turned real gold $4,045 into $2,300). Small numbers stay exact-only
    so a macro % like 2.6 can't be shielded by an unrelated price.
    """
    exact: set[str] = set()
    large: list[float] = []
    for text in texts:
        for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""):
            n = _norm_num(m)
            if "." in n or len(n) >= 4:
                exact.add(n)
            f = _to_float(m)
            if f is not None and f >= 10:
                large.append(f)
    return exact, sorted(large)


def _is_protected(tok: str, protected: tuple[set[str], list[float]]) -> bool:
    exact, large = protected
    if _norm_num(tok) in exact:
        return True
    f = _to_float(tok)
    if f is not None and f >= 10:
        # within ~1.5% of a real price/level the writer likely rounded
        return any(abs(f - g) <= max(0.015 * g, 0.01) for g in large)
    return False


# In the ECONOMY segment, only these macro-DATA claims get fact-checked. Everything
# else there is a live market price/level/%-change (indexes, stocks, crypto,
# commodities, yields) that comes from the Yahoo snapshot — the verifier can't
# price those in real time and hallucinates "corrections" (it turned a correct 10Y
# 4.42% into 4.53%, real BTC -2.28% into -3.6%). Leave those to Yahoo ground truth.
_MACRO_CLAIM_RE = re.compile(
    r"\b(inflation|pce|cpi|deflation|disinflation|unemployment|jobless|payroll|jobs? report|"
    r"gdp|growth rate|fed funds|funds rate|rate (?:cut|hike|projection|path)|dot plot|"
    r"median .*rate|basis points|core (?:inflation|prices)|wage growth)\b",
    re.IGNORECASE)


def _extract_numeric_claims(lines: list[dict[str, str]],
                            protected: tuple[set[str], list[float]] | None = None
                            ) -> list[tuple[int, str, str]]:
    """Return (line_idx, full_sentence, matched_token) for every numeric claim.

    Skips tokens that match a protected ground-truth number (Yahoo price / FRED
    macro), and in the ECONOMY segment only keeps claims that are about a macro
    data release — live market prices there are Yahoo ground truth, not for the
    verifier to second-guess.
    """
    protected = protected or (set(), [])
    out: list[tuple[int, str, str]] = []
    pattern = re.compile("|".join(_NUM_CLAIM_PATTERNS), re.IGNORECASE)
    for i, ln in enumerate(lines):
        text = ln.get("text", "")
        is_econ = ln.get("segment") == "economy"
        for m in pattern.finditer(text):
            tok = m.group(0)
            if _is_protected(tok, protected):
                continue  # genuine injected price/value — don't second-guess it
            # Carve out the surrounding sentence for context
            start = max(0, text.rfind(".", 0, m.start()) + 1)
            end = text.find(".", m.end())
            sent = text[start: end if end != -1 else len(text)].strip()
            if is_econ and not _MACRO_CLAIM_RE.search(sent):
                continue  # economy market price → leave to Yahoo, don't fact-check
            out.append((i, sent, tok))
    return out


def fact_check_script(lines: list[dict[str, str]],
                      date_str: str,
                      markets: str = "",
                      macro: str = "",
                      max_claims: int = 25) -> tuple[list[dict[str, str]], list[str]]:
    """Extract numeric claims, send a batch to Sonar for verification, rewrite stale claims.

    Numbers we fed the writer as ground truth (Yahoo prices + FRED macro) are
    protected from rewriting; everything else — including macro claims like core
    PCE — is verified. Returns (updated_lines, log_messages). Best-effort.
    """
    protected = _ground_truth_numbers(markets, macro)
    claims = _extract_numeric_claims(lines, protected)
    if not claims:
        return lines, ["[fact-check] no numeric claims found"]
    # Cap to avoid token blowups; prioritize claims that look like prices/percentages
    claims = claims[:max_claims]
    numbered = "\n".join(
        f"{i+1}. \"{sent}\" (token: {tok})"
        for i, (_, sent, tok) in enumerate(claims)
    )
    preamble = today_preamble(dt.date.fromisoformat(date_str))
    prompt = (
        preamble
        + "I'm fact-checking numeric claims in a podcast script before publication.\n"
        + "Claims include prices, percentages, sports scorelines, casualty counts, ages, and similar facts.\n"
        + "For each numbered claim below, VERIFY against the live web RIGHT NOW.\n"
        + "Return STRICT JSON: a list of objects with keys:\n"
        + '  {"id": <number>, "status": "VERIFIED"|"STALE"|"UNVERIFIABLE", "correct_value": "<short replacement phrase if STALE>", "note": "<one short sentence>"}\n'
        + "STALE means the number is materially wrong:\n"
        + "  - For prices/percentages: off by >=10%\n"
        + "  - For sports scorelines: ANY digit differs from the real final score\n"
        + "  - For casualty counts / ages: off by more than ~20% or by a clearly wrong category\n"
        + "VERIFIED means the number matches the current real-world value within tolerance.\n"
        + "UNVERIFIABLE means you cannot find a current source. If unsure, choose UNVERIFIABLE — never invent.\n"
        + "For sports scores, when STALE, correct_value MUST be the actual final score in the same X-Y format.\n\n"
        + "CLAIMS:\n" + numbered + "\n\nReturn only the JSON array."
    )
    log: list[str] = []
    try:
        raw = llm_chat(prompt, JUDGE_MODEL, 3000, temperature=0.0)
    except Exception as e:
        log.append(f"[fact-check] verification call FAILED: {e}")
        return lines, log

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    start, end = cleaned.find("["), cleaned.rfind("]")
    verdicts: list[dict] = []
    if start != -1 and end != -1:
        try:
            verdicts = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            verdicts = []
    if not verdicts:
        # Brace-walk salvage: extract every top-level {...} object as a verdict
        depth = 0
        obj_start = -1
        for i, ch in enumerate(cleaned):
            if ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and obj_start != -1:
                    blob = cleaned[obj_start:i + 1]
                    try:
                        obj = json.loads(blob)
                        if isinstance(obj, dict) and "id" in obj and "status" in obj:
                            verdicts.append(obj)
                    except Exception:
                        pass
                    obj_start = -1
    if not verdicts:
        log.append("[fact-check] verifier returned unparseable response — skipped")
        return lines, log

    out_lines = [dict(l) for l in lines]
    stale_count = 0
    for v in verdicts:
        try:
            idx = int(v.get("id", 0)) - 1
            if idx < 0 or idx >= len(claims):
                continue
            status = (v.get("status") or "").upper()
            if status != "STALE":
                continue
            line_idx, sent, tok = claims[idx]
            correct = (v.get("correct_value") or "").strip()
            note = (v.get("note") or "").strip()
            if not correct:
                # Just flag with a softening qualifier — don't invent
                replacement = re.sub(re.escape(tok), tok + " (note: figure may be stale)",
                                     out_lines[line_idx]["text"], count=1, flags=re.IGNORECASE)
            else:
                replacement = re.sub(re.escape(tok), correct,
                                     out_lines[line_idx]["text"], count=1, flags=re.IGNORECASE)
            out_lines[line_idx]["text"] = replacement
            log.append(f"[fact-check] line {line_idx}: '{tok}' → '{correct or '+softened'}' ({note})")
            stale_count += 1
        except Exception as e:
            log.append(f"[fact-check] verdict parse error: {e}")
    log.append(f"[fact-check] checked {len(claims)} claims, fixed {stale_count} stale")
    return out_lines, log


# ─── RECENCY AUDIT (is the news actually CURRENT?) ───────────────────────────
# The numeric fact-check catches wrong numbers; this catches wrong TIMING — Sonar
# routinely surfaces stale events (a months-old trade, a past election, last
# season's title) and narrates them as if they happened "Thursday". This pass
# verifies datable claims against live search relative to today and rewrites the
# time framing (or softens) so nothing old masquerades as breaking.

# Verbs/phrases that mark a datable, recency-sensitive event.
_RECENCY_EVENT_RE = re.compile(
    r"\b(won|beat|defeated|clinched|crowned|advanced|eliminated|knocked out|"
    r"traded|signed|hired|fired|resigned|stepped down|elected|sworn in|appointed|"
    r"launched|released|unveiled|announced|acquired|merged|filed|indicted|"
    r"passed|passes|approved|approves|voted|enacted|ratified|muscled through|"
    r"struck down|overturned|upheld|ruled|sentenced|convicted|acquitted|ousted|"
    r"died|passed away|premiered|dropped|debuted|set a record|broke the record)\b"
    r"|\b(today|yesterday|tonight|last night|overnight|pre-?dawn|"
    r"this (?:week|month|year|morning|weekend)|last (?:week|month|year)|"
    r"(?:a few |several )?days ago|weeks ago|earlier this (?:week|month)|"
    r"(?:late |early )?(?:on )?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))\b",
    re.IGNORECASE)

# Numbers in the live-markets/macro blocks are ground truth — don't recency-audit them.
_RECENCY_SKIP_SEGMENTS = {"economy"}


def _extract_recency_claims(lines: list[dict[str, str]],
                            max_claims: int = 30) -> list[tuple[int, str]]:
    """Return (line_idx, text) for lines asserting a datable, recency-sensitive event.

    Prioritizes the segments most prone to stale events (sports/news/culture).
    """
    priority = {"sports": 0, "hard_news": 1, "politics": 1, "culture": 2,
                "tech_ai": 3, "feature": 3, "science": 4}
    cands: list[tuple[int, int, str]] = []
    for i, ln in enumerate(lines):
        if ln.get("is_intro") or ln.get("segment") in _RECENCY_SKIP_SEGMENTS:
            continue
        text = ln.get("text", "")
        if _RECENCY_EVENT_RE.search(text):
            cands.append((priority.get(ln.get("segment", ""), 5), i, text))
    cands.sort(key=lambda c: c[0])
    return [(i, t) for _, i, t in cands[:max_claims]]


RECENCY_SYSTEM_PROMPT = """You verify whether described events are CURRENT relative to a given date.
You will get today's date and a numbered list of statements from a news podcast. For EACH statement,
use live web search to find when the described event ACTUALLY happened, then judge it.

WATCH FOR THE ANNIVERSARY TRAP — the most important thing you catch:
A famous law, ruling, election, deal, or event from a PRIOR YEAR often resurfaces in coverage (often on
its ANNIVERSARY) and gets mistaken for a brand-new event. ALWAYS check the event's ORIGINAL date and
YEAR, not just the month. Example: the "One Big Beautiful Bill Act" was passed and signed in JULY 2025 —
so any 2026 statement that it "passed this week" / "late Wednesday" is STALE by a full year. Be
especially skeptical of well-known named legislation, court rulings, and elections described as if new.

Output STRICT JSON only — no prose, no fences:
{"verdicts": [
  {"id": <n>, "verdict": "current" | "stale" | "unknown",
   "actual_timing": "<the event's real date/year, e.g. 'July 2025' or 'unknown'>",
   "rewrite": "<if stale: rewrite the FULL statement so the timing is accurate and explicit
               (e.g. 'A year ago, in July 2025, the House passed…' / 'back in April'); keep all other
               facts and the speaking style identical. If current or unknown: empty string>"}
]}
"current" = the core event happened within roughly the last 10 days of today's date.
"stale" = it actually happened materially earlier (a prior month or YEAR, an anniversary) but is framed
as recent/just-happened.
Only mark "stale" when web evidence shows the event is genuinely older — but a wrong YEAR is always stale."""


def recency_audit_script(lines: list[dict[str, str]], date_str: str,
                         max_claims: int = 30) -> tuple[list[dict[str, str]], list[str]]:
    """Flag/rewrite stale-timing claims so old events aren't narrated as current.

    Best-effort and non-fatal — mirrors fact_check_script. Returns (lines, log).
    """
    claims = _extract_recency_claims(lines, max_claims)
    if not claims:
        return lines, ["[recency] no datable event claims found"]
    numbered = "\n".join(f"{n+1}. \"{txt}\"" for n, (_, txt) in enumerate(claims))
    prompt = (today_preamble(dt.date.fromisoformat(date_str)) +
              f"Today is {date_str}. Verify the timing of these statements:\n{numbered}\n\n"
              "Return the verdicts JSON now.")
    try:
        raw = llm_chat(prompt, model=JUDGE_MODEL, max_tokens=4000,
                        system=RECENCY_SYSTEM_PROMPT, temperature=0.1)
    except Exception as e:  # noqa: BLE001
        return lines, [f"[recency] verification call failed (non-fatal): {e}"]

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e != -1:
        cleaned = cleaned[s:e + 1]
    try:
        verdicts = json.loads(cleaned).get("verdicts", [])
    except json.JSONDecodeError as ex:
        return lines, [f"[recency] parse failed (non-fatal): {ex}"]

    out_lines = [dict(l) for l in lines]
    log: list[str] = []
    fixed = 0
    for v in verdicts:
        try:
            idx = int(v.get("id", 0)) - 1
            if idx < 0 or idx >= len(claims):
                continue
            if (v.get("verdict") or "").lower() != "stale":
                continue
            line_idx, _ = claims[idx]
            rewrite = (v.get("rewrite") or "").strip()
            timing = (v.get("actual_timing") or "").strip()
            if rewrite:
                out_lines[line_idx]["text"] = rewrite
            else:
                continue
            log.append(f"[recency] line {line_idx}: stale → re-anchored ({timing})")
            fixed += 1
        except Exception as ex:  # noqa: BLE001
            log.append(f"[recency] verdict parse error: {ex}")
    log.append(f"[recency] checked {len(claims)} event claims, re-anchored {fixed} stale")
    return out_lines, log


# ─── SCRIPT GENERATION (MULTI-PASS) ──────────────────────────────────────────

SCRIPT_SEGMENT_SYSTEM_PROMPT = """You are writing one segment of a daily podcast briefing.

THE SHOW
Two hosts with distinct but good-faith worldviews:
- Maya = HOST A, the Technocratic Systems Explainer. Institutional, analytical, framework-oriented.
  Trusts mainstream reporting and evidence/policy logic. Leads on politics, economics, business, tech
  infrastructure, AI policy, medicine, climate, and applied science.
- Ben = HOST B, the Heterodox Narrative Skeptic. Skeptical of consensus narratives, attuned to human
  stories and cultural dynamics, willing to channel heterodox/contrarian-but-serious viewpoints. Leads
  on culture, sports narratives, internet discourse, AI criticism, social impact, and mind-bending
  science (physics, space, cognition, surprising results).
They CHALLENGE each other, but the disagreement is intellectually serious, never performative.
Vibe: a smart, curious, broad conversation — witty and casual by default, but able to turn sober and
serious when the topic demands it (wars, constitutional stakes, consequential science/health).
Listener: a smart generalist who wants to converse fluently across every domain — NOT an AI specialist.

OUTPUT FORMAT — STRICT
Return ONLY a JSON array. No prose before or after. No code fences. No commentary.
Each element: {{"speaker": "kore" | "charon", "text": "<line>"}}
- "kore" = Maya. "charon" = Ben.
- Lines: conversational, 15-60 words each, natural overlap, callbacks, mild disagreement.
- Cite specifics: company names, numbers, headlines. No vague "experts say".
- Do NOT include speaker name in the text field. Just the spoken line.
- Each text field MUST be under 800 characters.
- Avoid stage directions in brackets ([pause], [laughs], etc.) — they get spoken aloud by the TTS.

RECAP MEMORY USAGE
You will receive a "RECENT EPISODES" block. Use it as follows:
- If a story was already covered in a recent episode AND has no real new development, SKIP IT.
- If a story has a genuine update, reference what was said before and lead with what's new.
  Example: "Yesterday we flagged Anthropic's federal ban as day ten. Today: Mythos six has been served."
- Do not re-cover identical ground. The listener heard it.

TIME ANCHORING — CRITICAL
For every news event you describe, anchor it in time using natural relative phrases:
- "On Monday", "yesterday", "this week", "Friday last week", "three days ago".
- Do NOT narrate events as if they just happened today unless they actually did.
- If a topic's source date is more than 3 days old, acknowledge it: "From earlier this week..."
- If you're following up on a prior episode's coverage, say so: "We flagged this on Tuesday — here's the update."
- Never invent dates. If timing is ambiguous, prefer vague but honest phrasing ("recently") over false precision.

EDITORIAL TONE
- No data-engineering/ETL/B2B-SaaS bias — only include if a story is top-3 in tech that day.
- Broad, smart, occasionally funny. Not a trade publication.
"""

SCRIPT_SEGMENT_USER_TEMPLATE = """{today_preamble}

Today's date: {date} ({day_name})

EDITOR'S NOTE FOR TODAY
================
{editor_note}

SEGMENT TO WRITE
================
Segment: {label}
Word target: {words} words (hit within ±15%)
This segment's purpose: {purpose}
Freshness rule for this segment: {freshness}

RECENT EPISODES (last {lookback} days)
================
{recap_block}

CURATED TOPICS FOR THIS SEGMENT (these have been pre-selected and ranked by the editor; use ALL of them, don't add more)
================
{research_block}
{timeline_block}{running_threads_block}
PREVIOUS SEGMENTS WRITTEN TODAY (for tone + callback continuity, do NOT repeat content)
================
{prior_segments_block}

Write this segment now per the system instructions. Cover every curated topic in roughly its ranked order. Output the JSON array only."""

SEGMENT_PURPOSE = {
    "cold_open":     "A fast cross-domain scan of the stories the show ACTUALLY covers today — one crisp line each on the biggest 4-6, then tease what's coming. The CURATED TOPICS block contains the opening lines of each finished segment; summarize THOSE faithfully (same facts, same numbers — do NOT introduce different figures than the segments use). Keep it punchy; do NOT analyze yet. No explicit 'welcome to the briefing' — the intro tag handles that.",
    "hard_news":     "The day's major US and world stories WITH CONTEXT, not wire copy. Maya (Host A) leads with the institutional read; Ben (Host B) presses on what's uncertain or under-covered. Explain why each story matters.",
    "politics":      "Politics & policy with COMPETING VIEWS. Present the strongest case on each side of one or two live disputes — institutional/mainstream AND serious heterodox (left and center-right). Focus on what happened, why it matters, and how thoughtful people disagree. Maya leads the policy logic; Ben channels the heterodox skeptic.",
    "economy":       "Economics, business & markets in plain English. The LIVE MARKETS SNAPSHOT (Yahoo) and LIVE MACRO SNAPSHOT (FRED) blocks hold the REAL, current numbers — use THOSE verbatim for prices, inflation, the Fed funds rate, unemployment, and GDP, and NEVER use a different figure from a research headline. MARKET PRICES RULE: for ANY index, stock, crypto, commodity, or bond-yield number (e.g. oil/Brent, the S&P, Bitcoin, the 10-year yield), quote ONLY the LIVE MARKETS SNAPSHOT value — if a research topic cites a different price (e.g. a headline says oil is '$90' but the snapshot says $72), the headline is stale: use the snapshot's $72 and ignore the headline number. CRITICAL on rates: derive the rate-path story from the real data — if inflation is still elevated and the Fed funds rate is being held, do NOT claim rate cuts are imminent or 'likely soon'; say plainly that the Fed is on hold / leaning hawkish if that's what the numbers show. Pair the movers with one macro or company-strategy thread; if this month's inflation print was already covered recently, lead with a different angle. Maya (Host A) leads. Narrative and implication over ticker updates.",
    "tech_ai":       "Tech & AI, deliberately BOUNDED. At most 3-4 stories; emphasize builders, products, infra, chips/geopolitics, and safety/labor/policy implications over model-release hype. Maya leads on infra/policy; Ben brings the hype-skeptical, critic's read. Do NOT let this run long.",
    "science":       "Science & discovery — TWO deep dives: one practical/near-term (health, climate, applied tech — Maya leads) and one mind-bending (physics, space, neuroscience, a surprising result — Ben leads). Translate the finding, then 'so what?'. Curiosity over hype.",
    "feature":       "The day's single best LONGFORM ARGUMENT. If a BUZZING TOPICS — DEEP MATERIAL block appears, it has the actual source text from a key essay, interview, X thread, or podcast. Spend real time on ONE or TWO: name the author/publication, QUOTE specific lines, then analyze — what they nailed, what they missed, where each host pushes back. Maya emphasizes econ/policy/tech/institutional angles; Ben emphasizes culture/psychology/society/status. Depth over breadth.",
    "culture":       "What everyone is watching, streaming, listening to, and talking about — bias toward thrillers, buzzy shows/films, and music (house/electronic, hip-hop/rap, pop, Latin/Spanish urban). Ben (Host B) leads. Internet culture enters when it crosses into mainstream conversation. Talk like two people who actually saw the thing. NO obscure literary essays.",
    "sports":        "Concise but vivid sports narratives across DIFFERENT leagues/tournaments — rivalries, stars, strategy, standings, stakes, historical significance. Ben leads. Assume basic familiarity; explain why this athlete/match/season MATTERS. Not stat-heavy recaps. Keep it tight.",
    "disagreement":  "ONE real, concrete disagreement from this week — ideally two named writers/experts with directly opposing takes on the same story (use the POLITICS / FEATURE / HARD NEWS topics provided). Maya (Host A) takes the institutional side, Ben (Host B) the heterodox side, and they genuinely argue it out — serious, good-faith, not performative. Name who actually holds each view.",
    "signoff":       "Brief warm wrap. Recap the 2-3 threads most worth remembering and tease tomorrow. Do NOT add another intro tag.",
}

# Deterministic intro lines (NOT generated each run — fixed copy for predictable transitions).
# Inserted BEFORE the LLM-generated segment dialogue. Voiced by alternating hosts.
# `kore` = Maya (Host A), `charon` = Ben (Host B).
SEGMENT_INTROS: dict[str, dict[str, str]] = {
    "hard_news":     {"speaker": "kore",   "text": "Let's start with the big stories — the hard news that actually moved today, with the context that makes it make sense."},
    "politics":      {"speaker": "charon", "text": "Now into politics — and this is where we tend to disagree. Let's lay out the arguments, not just the headlines."},
    "economy":       {"speaker": "kore",   "text": "On to the economy. Markets, macro, and the companies worth tracking — with the real numbers."},
    "tech_ai":       {"speaker": "charon", "text": "Tech and AI now — and we're keeping this tight on purpose. The stuff that actually matters for builders and policy, not the hype cycle."},
    "science":       {"speaker": "kore",   "text": "Science time. One thing that could change your life, and one thing that might bend your brain."},
    "feature":       {"speaker": "charon", "text": "Here's the piece we want to really sit with today — the one argument worth chewing on."},
    "culture":       {"speaker": "charon", "text": "Culture break. What everyone's watching, streaming, and arguing about right now."},
    "sports":        {"speaker": "kore",   "text": "Quick sports detour — the narratives and the stakes, not the box scores."},
    "disagreement":  {"speaker": "charon", "text": "And now the part of the show where we actually disagree. One real fight, both sides."},
    "signoff":       {"speaker": "kore",   "text": "That's the show. Let's close it out."},
}

# Per-segment freshness windows (v8). today_preamble() supplies the actual dates;
# this tells the writer how old material may be and to time-anchor explicitly.
_FRESH_DEFAULT = "Prefer the last 7 days; time-anchor anything older ('earlier this week')."
SEGMENT_FRESHNESS: dict[str, str] = {
    "cold_open":    "Headlines must be from the last 24-36 hours.",
    "hard_news":    "Must be from the last 24-36 hours. If older, say so plainly.",
    "politics":     "Last 7 days. Time-anchor the originating event.",
    "economy":      "Markets numbers are live (today). Macro/company stories from the last 24-48 hours.",
    "tech_ai":      "Last 7 days; prefer the last 48 hours for product/launch news.",
    "science":      "Paper releases within ~14 days. A mind-bending finding may be older ONLY if explicitly framed as resurfaced ('a 2024 result that's back in conversation because…').",
    "feature":      "Up to 14 days old, but REQUIRE an explicit time anchor ('last weekend's Atlantic piece by…').",
    "culture":      "Last 7 days for buzz; an ongoing show/film/album season can be older if currently airing.",
    "sports":       "Last 7 days; a narrative arc may reference the broader season, time-anchored.",
    "disagreement": "The disagreement should be from THIS week's stories.",
    "signoff":      "n/a — wrap-up.",
}


def render_curated_topics(topics: list[dict[str, Any]]) -> str:
    """Render the curator's picks for one segment into a writer-friendly block."""
    if not topics:
        return "(no curated topics for this segment - keep it brief or skip)"
    parts: list[str] = []
    for i, t in enumerate(topics, 1):
        beat = t.get("beat", "")
        src = t.get("source", "")
        url = t.get("url", "")
        block = [f"[{i}] ({beat}) {t.get('headline','')}"]
        if t.get("detail"):
            block.append(f"    Detail: {t['detail']}")
        if t.get("angle"):
            block.append(f"    Angle:  {t['angle']}")
        if src or url:
            block.append(f"    Source: {src}" + (f" - {url}" if url else ""))
        if t.get("update_note"):
            block.append(f"    UPDATE on prior coverage: {t['update_note']}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def build_markets_overlay(markets: str) -> str:
    """Compact markets data to feed into the economy segment alongside curated topics."""
    return f"LIVE MARKETS SNAPSHOT (use this for the markets sub-block):\n{markets}"


def build_macro_overlay(macro: str) -> str:
    """Ground-truth macro block (FRED) for the economy segment — real PCE/CPI/Fed/etc."""
    return macro


def _macro_http_get(url: str) -> str:
    """Fetch a FRED CSV via httpx. Short timeout — FRED is blocked on some CI
    runners, so we want to fail fast and fall back rather than stall the run."""
    r = httpx.get(url, timeout=6.0, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (podcast-macro)"})
    r.raise_for_status()
    return r.text


def gather_macro() -> str:
    """Real macro snapshot from FRED (best-effort, non-fatal)."""
    print("[macro] fetching real macro data from FRED...", file=sys.stderr)
    try:
        snap = macro_mod.build_macro_snapshot(
            fetch_fn=_macro_http_get, log=lambda m: print(m, file=sys.stderr))
    except Exception as e:  # noqa: BLE001
        print(f"[macro] FRED fetch failed (non-fatal): {e}", file=sys.stderr)
        return ""
    if snap:
        n = snap.count("\n- ")
        print(f"[macro] FRED ok — {n} series grounded", file=sys.stderr)
    else:
        print("[macro] no macro data (FRED unreachable) — falling back to research", file=sys.stderr)
    return snap



def compact_prior_segments(prior: list[list[dict[str, str]]]) -> str:
    """Compact already-written segments into a short bullet summary for context."""
    if not prior:
        return "(this is the first segment)"
    lines: list[str] = []
    for seg in prior:
        if not seg:
            continue
        # Just show first and last line of each prior segment for tone continuity
        first = seg[0]["text"][:120]
        last = seg[-1]["text"][:120]
        speaker_first = "Maya" if seg[0]["speaker"] == "kore" else "Ben"
        speaker_last = "Maya" if seg[-1]["speaker"] == "kore" else "Ben"
        lines.append(f"- Started with {speaker_first}: \"{first}...\"  Ended with {speaker_last}: \"...{last}\"")
    return "\n".join(lines)


def _salvage_dialogue_lines(text: str) -> list[dict[str, Any]]:
    """Brace-walk a possibly broken JSON array to recover valid {speaker, text} objects."""
    items: list[dict[str, Any]] = []
    depth = 0
    obj_start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                blob = text[obj_start:i + 1]
                try:
                    obj = json.loads(blob)
                    if (isinstance(obj, dict)
                            and obj.get("speaker") in ("kore", "charon")
                            and isinstance(obj.get("text"), str)):
                        items.append(obj)
                except json.JSONDecodeError:
                    pass
                obj_start = -1
    return items


# Segments where empty curated_topics should produce a brief honest skip line rather than
# hallucinated content. cold_open is included so the writer can't free-style off the recap.
CONTENT_SEGMENTS = {"cold_open", "hard_news", "politics", "economy", "tech_ai", "science",
                    "feature", "culture", "sports", "disagreement"}

SKIP_LINES: dict[str, list[dict[str, str]]] = {
    "cold_open": [
        {"speaker": "kore",   "text": "Big day. Lots moving — let's get into it."},
        {"speaker": "charon", "text": "We'll jump straight into the news."},
    ],
    "culture": [
        {"speaker": "charon", "text": "Honestly, nothing in culture jumped out at us today that everyone is talking about. We'll skip ahead rather than force it."},
    ],
    "sports": [
        {"speaker": "kore", "text": "Quiet day in sports — nothing big enough to break down. Moving on."},
    ],
    "disagreement": [
        {"speaker": "charon", "text": "No sharp, clean disagreement jumped out at us in today's stories — so rather than fake a fight, we'll let it go and come back to it tomorrow."},
    ],
}



# ─── BUZZING TOPICS — DEEP SOURCE FETCH ──────────────────────────────────────
# For items in the arguments segment from twitter/substack/podcasts beats, we
# fetch the full source content (tweet text, essay body, episode summary) so
# the writer can DEEPLY analyze rather than restate the curator's headline.

import urllib.request as _urlreq_bt
import html as _html_bt

_BUZZ_OPENER = _urlreq_bt.build_opener(_urlreq_bt.ProxyHandler({}))
_TWITTER_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)")


def _fetch_tweet_text(handle: str, tid: str, timeout: float = 12.0) -> dict[str, str] | None:
    """Pull a tweet's full body via fxtwitter.com (no auth required).

    Returns {author, text, url} or None on failure.
    """
    url = f"https://fxtwitter.com/{handle}/status/{tid}"
    req = _urlreq_bt.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; podcast-fetcher)"})
    try:
        with _BUZZ_OPENER.open(req, timeout=timeout) as r:
            page = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[buzz] fxtwitter fetch failed for {handle}/{tid}: {e}", file=sys.stderr)
        return None

    def meta(prop: str) -> str | None:
        m = re.search(rf'<meta property="{re.escape(prop)}" content="([^"]+)"', page)
        return _html_bt.unescape(m.group(1)) if m else None

    author = meta("og:title") or handle
    text = meta("og:description") or ""
    canonical = meta("og:url") or f"https://x.com/{handle}/status/{tid}"
    if not text:
        return None
    return {"author": author, "text": text, "url": canonical}


def _fetch_article_text(url: str, timeout: float = 15.0, max_chars: int = 6000) -> str | None:
    """Pull a public article body. Returns plaintext snippet (paywall-aware fallback).

    Uses a proxy-bypassing opener so this works under the credential proxy.
    Best-effort: returns None if the page is paywalled, errors, or has no extractable text.
    """
    req = _urlreq_bt.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; podcast-fetcher)",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with _BUZZ_OPENER.open(req, timeout=timeout) as r:
            page = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[buzz] article fetch failed for {url}: {e}", file=sys.stderr)
        return None

    # Strip scripts and styles
    page = re.sub(r"<script[^>]*>.*?</script>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<style[^>]*>.*?</style>", " ", page, flags=re.S | re.I)
    # Prefer <article> body if present
    art = re.search(r"<article[^>]*>(.*?)</article>", page, flags=re.S | re.I)
    body = art.group(1) if art else page
    # Strip tags
    text = re.sub(r"<[^>]+>", " ", body)
    text = _html_bt.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 400:
        # Probably JS-rendered or paywalled
        return None
    return text[:max_chars]


def _fetch_podcast_context(headline: str, source: str, url: str | None) -> str | None:
    """For podcast items, ask Sonar to summarize the actual idea/quote from the named episode.

    The curator already gave us a headline; we want the writer to have a fuller paragraph.
    Best-effort: 200-400 words; returns None on failure.
    """
    if not (source or headline):
        return None
    prompt = (
        f"Find the most interesting specific idea or argument from this podcast episode:\n"
        f"  Source: {source}\n  Headline / claim: {headline}\n"
        f"  URL: {url or '(none)'}\n\n"
        f"Return ONE paragraph (150-250 words) describing the actual argument made on the "
        f"episode, including who said it, what they argued, and any specific examples or "
        f"numbers they cited. If you cannot find the episode, return 'UNKNOWN' exactly."
    )
    try:
        out = llm_chat(prompt, SEARCH_MODEL, 700, temperature=0.2).strip()
    except Exception as e:
        print(f"[buzz] podcast context fetch failed: {e}", file=sys.stderr)
        return None
    if not out or out.strip().upper().startswith("UNKNOWN"):
        return None
    return out[:2000]

def _fetch_essay_context(headline: str, source: str, url: str | None) -> str | None:
    """Sonar-based fallback for essays/op-eds when direct HTTP fetch fails (404, paywall).

    Asks Sonar to find the actual argument and quote a few key passages from the named
    author/publication on this topic. Returns 200-300 word paragraph or None.
    """
    if not (source or headline):
        return None
    prompt = (
        f"{today_preamble()}\n"
        f"Find the strongest specific argument from this recent essay or op-ed:\n"
        f"  Publication / author: {source}\n  Claim: {headline}\n"
        f"  Hinted URL (may be wrong): {url or '(none)'}\n\n"
        f"Return ONE paragraph (200-300 words) that includes: (1) the author's central\n"
        f"argument in their own framing, (2) at least one direct quote or near-quote of a\n"
        f"specific line from the piece if available, (3) any concrete numbers or examples\n"
        f"they cite. If you cannot find a real piece matching this claim from this author,\n"
        f"return 'UNKNOWN' exactly."
    )
    try:
        out = llm_chat(prompt, SEARCH_MODEL, 800, temperature=0.2).strip()
    except Exception as e:
        print(f"[buzz] essay context fetch failed: {e}", file=sys.stderr)
        return None
    if not out or out.strip().upper().startswith("UNKNOWN"):
        return None
    return out[:2400]


def _fetch_tweet_context(headline: str, source: str, url: str | None) -> str | None:
    """Sonar-based fallback for X posts when fxtwitter fetch fails (deleted, bad ID).

    Asks Sonar to find the actual take from the named handle on this topic.
    Returns 150-250 word paragraph or None.
    """
    if not (source or headline):
        return None
    handle = source if source.startswith("@") else f"from {source}"
    prompt = (
        f"{today_preamble()}\n"
        f"Find the actual recent take {handle} has made on this topic:\n"
        f"  Claim: {headline}\n"
        f"  Hinted URL (may be wrong): {url or '(none)'}\n\n"
        f"Return ONE paragraph (150-250 words) capturing: (1) the substance of the\n"
        f"take from that person/account, (2) at least one direct quote or near-quote\n"
        f"if available, (3) any specific numbers or examples they used. If you cannot\n"
        f"verify that person has publicly said this recently, return 'UNKNOWN' exactly."
    )
    try:
        out = llm_chat(prompt, SEARCH_MODEL, 700, temperature=0.2).strip()
    except Exception as e:
        print(f"[buzz] tweet context fetch failed: {e}", file=sys.stderr)
        return None
    if not out or out.strip().upper().startswith("UNKNOWN"):
        return None
    return out[:2000]


def fetch_buzzing_material(arguments_topics: list[dict[str, Any]],
                            max_items: int = 8) -> list[dict[str, Any]]:
    """For each curated arguments topic, attach a 'source_text' field with deep material.

    Skips items that don't have a fetchable URL. Returns the input list with new fields:
      - source_text: str   (when successfully fetched)
      - source_kind: str   (one of: tweet, essay, podcast, unknown)
    """
    out: list[dict[str, Any]] = []
    for t in arguments_topics[:max_items]:
        beat = (t.get("beat") or "").lower()
        url = t.get("url") or ""
        enriched = dict(t)
        kind = "unknown"
        text: str | None = None

        m = _TWITTER_URL_RE.search(url) if url else None
        if m:
            kind = "tweet"
            tweet = _fetch_tweet_text(m.group(1), m.group(2))
            if tweet:
                text = f"@{tweet['author'].split(' (')[0].strip()}: \"{tweet['text']}\""
            else:
                # fxtwitter failed (deleted/hallucinated ID) -> Sonar fallback
                text = _fetch_tweet_context(t.get("headline", ""), t.get("source", ""), url)
        elif beat == "twitter" or (t.get("source") or "").startswith("@"):
            # twitter beat but URL not pattern-matchable (or missing) -> Sonar fallback
            kind = "tweet"
            text = _fetch_tweet_context(t.get("headline", ""), t.get("source", ""), url)
        elif beat == "podcasts" or "podcast" in (t.get("source") or "").lower():
            kind = "podcast"
            text = _fetch_podcast_context(t.get("headline", ""), t.get("source", ""), url)
        elif beat in {"substack"} or url:
            kind = "essay"
            if url and url.startswith(("http://", "https://")):
                text = _fetch_article_text(url)
            if not text:
                # 404 / paywall / JS-rendered -> Sonar fallback
                text = _fetch_essay_context(t.get("headline", ""), t.get("source", ""), url)

        if text:
            enriched["source_text"] = text
            enriched["source_kind"] = kind
            print(f"[buzz] enriched {kind}: '{t.get('headline','')[:60]}' "
                  f"({len(text)} chars)", file=sys.stderr)
        else:
            enriched["source_kind"] = kind
            print(f"[buzz] no source_text for {kind}: '{t.get('headline','')[:60]}'",
                  file=sys.stderr)
        out.append(enriched)
    return out


def render_buzzing_block(enriched: list[dict[str, Any]]) -> str:
    """Render the deep buzzing material into a writer-facing block."""
    parts: list[str] = []
    parts.append("=" * 60)
    parts.append("BUZZING TOPICS — DEEP MATERIAL TO ANALYZE")
    parts.append("=" * 60)
    parts.append("These are the actual source texts from the highest-signal X posts,")
    parts.append("essays, and podcast conversations the curator picked for arguments.")
    parts.append("DO NOT just paraphrase the headline — quote specific lines, name the")
    parts.append("author, then push back, agree, or build on the argument. This is the")
    parts.append("ANALYTICAL HEART of the show.")
    parts.append("")
    any_with_source = False
    for i, t in enumerate(enriched, 1):
        if not t.get("source_text"):
            continue
        any_with_source = True
        kind = t.get("source_kind", "unknown")
        head = t.get("headline", "")
        src = t.get("source", "")
        url = t.get("url", "")
        parts.append(f"--- BUZZ {i} [{kind.upper()}] ---")
        parts.append(f"Headline: {head}")
        if src:
            parts.append(f"Source:   {src}")
        if url:
            parts.append(f"URL:      {url}")
        parts.append("Full source text:")
        parts.append(t["source_text"])
        parts.append("")
    if not any_with_source:
        parts.append("(no deep buzzing material could be fetched today — work from "
                     "headlines in CURATED TOPICS)")
    return "\n".join(parts)


def write_segment(seg: dict[str, Any],
                  curated_topics: list[dict[str, Any]],
                  markets: str,
                  recap_block: str,
                  prior_segments: list[list[dict[str, str]]],
                  date_str: str,
                  editor_note: str = "",
                  buzzing_block: str = "",
                  running_threads_block: str = "",
                  timeline_block: str = "",
                  macro: str = "") -> list[dict[str, str]]:
    """Generate one segment of dialogue from CURATED topics."""
    # Short-circuit: empty topics in a content segment — emit honest skip line
    # rather than letting the model hallucinate.
    if seg["key"] in CONTENT_SEGMENTS and not curated_topics:
        skip = SKIP_LINES.get(seg["key"], [
            {"speaker": "kore", "text": f"Nothing strong in {seg['label']} today. Moving on."},
        ])
        out = [{**l, "segment": seg["key"], "segment_label": seg["label"]} for l in skip]
        words = sum(len(line["text"].split()) for line in out)
        print(f"[segment {seg['key']}] EMPTY topics — emitted {len(out)} skip line ({words} words)",
              file=sys.stderr)
        return out

    topics_block = render_curated_topics(curated_topics)
    if seg["key"] == "economy":
        topics_block = topics_block + "\n\n" + build_markets_overlay(markets)
        if macro:
            topics_block = topics_block + "\n\n" + build_macro_overlay(macro)
    if seg["key"] == "feature" and buzzing_block:
        topics_block = topics_block + "\n\n" + buzzing_block
    prior_block = compact_prior_segments(prior_segments)

    _today_obj = dt.date.fromisoformat(date_str) if isinstance(date_str, str) else dt.date.today()
    user_prompt = SCRIPT_SEGMENT_USER_TEMPLATE.format(
        today_preamble=today_preamble(_today_obj).strip(),
        date=date_str,
        day_name=_today_obj.strftime("%A"),
        label=seg["label"],
        words=seg["words"],
        purpose=SEGMENT_PURPOSE.get(seg["key"], ""),
        freshness=SEGMENT_FRESHNESS.get(seg["key"], _FRESH_DEFAULT),
        lookback=RECAP_LOOKBACK_DAYS,
        recap_block=recap_block,
        research_block=topics_block,
        prior_segments_block=prior_block,
        editor_note=editor_note or "(no editor note)",
        # v7 blocks render as their own sections only when populated.
        timeline_block=(f"\n{timeline_block}\n" if timeline_block else ""),
        running_threads_block=(f"\n{running_threads_block}\n" if running_threads_block else ""),
    )

    max_tokens = max(2500, int(seg["words"] * 4.0))
    raw = llm_chat(user_prompt, model=SCRIPT_MODEL, max_tokens=max_tokens,
                    system=SCRIPT_SEGMENT_SYSTEM_PROMPT, temperature=0.7)

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        # SALVAGE valid lines from partial output - this was the v2 bug that lost culture/fodder
        debug_path = EPISODES_DIR / date_str / f"segment_raw_{seg['key']}.txt"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(raw)
        data = _salvage_dialogue_lines(cleaned)
        if data:
            print(f"[segment {seg['key']}] JSON parse failed ({e}); SALVAGED {len(data)} valid lines",
                  file=sys.stderr)
        else:
            print(f"[segment {seg['key']}] JSON parse failed: {e}. Raw saved to {debug_path}",
                  file=sys.stderr)
            return []

    valid = []
    for line in data:
        if (isinstance(line, dict)
                and line.get("speaker") in ("kore", "charon")
                and isinstance(line.get("text"), str)):
            valid.append({
                "speaker": line["speaker"],
                "text": line["text"],
                "segment": seg["key"],
                "segment_label": seg["label"],
            })
    words = sum(len(line["text"].split()) for line in valid)
    print(f"[segment {seg['key']}] {len(valid)} lines, {words} words (target {seg['words']})",
          file=sys.stderr)
    return valid


# Show order for the cold-open headline scan (one top item per content segment).
_HEADLINE_SCAN_ORDER = ["hard_news", "politics", "economy", "tech_ai",
                        "science", "feature", "culture", "sports"]


def _headline_scan_topics(seg_picks: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Top curated item from each content segment, in show order — feeds the cold open."""
    out: list[dict[str, Any]] = []
    for k in _HEADLINE_SCAN_ORDER:
        picks = seg_picks.get(k) or []
        if picks:
            out.append(picks[0])
    return out


def _finished_show_digest(results: dict[str, list[dict[str, str]]],
                          segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build cold-open source material from the ALREADY-WRITTEN body segments.

    The cold open is a scan of what the show actually covered, so it must derive
    from finished segments (not raw curated headlines) — otherwise the headline
    can contradict the body (e.g. PCE "2.6%" up top vs "3.4%" in the economy
    segment). One pseudo-topic per content segment, from its first substantive line.
    """
    label_by_key = {s["key"]: s["label"] for s in segments}
    digest: list[dict[str, Any]] = []
    for seg in segments:
        key = seg["key"]
        if key in ("cold_open", "signoff", "disagreement"):
            continue
        body = [l for l in results.get(key, []) if not l.get("is_intro")]
        if not body:
            continue
        lead = next((l for l in body if len(l["text"].split()) >= 12), body[0])
        digest.append({
            "headline": lead["text"][:220],
            "detail": " ".join(l["text"] for l in body[:2])[:400],
            "beat": label_by_key.get(key, key),
        })
    return digest


def generate_script(curated: dict[str, Any],
                    markets: str,
                    recap_block: str,
                    segments: list[dict[str, Any]],
                    date_str: str,
                    recaps: list[dict[str, Any]] | None = None,
                    two_pass: bool = False,
                    macro: str = "") -> list[dict[str, str]]:
    """Multi-pass script generation: write each body segment separately from CURATED
    topics, THEN synthesize the cold open from the finished bodies, then assemble in
    show order with deterministic intro lines.
    """
    # Pre-fetch deep source material for the FEATURE (longform) segment — the
    # essays/threads/podcasts the curator routed there. X is a quote layer here,
    # not a source of truth (the Sonar fallback handles 404'd URLs).
    _feature_topics = (curated.get("segments") or {}).get("feature") or []
    buzzing_block_str = ""
    if _feature_topics:
        try:
            buzzing_block_str = render_buzzing_block(fetch_buzzing_material(_feature_topics, max_items=8))
            (EPISODES_DIR / date_str).mkdir(parents=True, exist_ok=True)
            (EPISODES_DIR / date_str / "buzzing.txt").write_text(buzzing_block_str)
        except Exception as e:
            print(f"[buzz] fetch_buzzing_material FAILED (non-fatal): {e}", file=sys.stderr)

    seg_picks = curated.get("segments", {})
    editor_note = curated.get("editor_note", "")
    _today_obj = dt.date.fromisoformat(date_str) if isinstance(date_str, str) else dt.date.today()

    # v7 editorial: running threads computed once for the whole episode.
    running_block = ""
    if two_pass and recaps:
        try:
            running_block = editorial.format_running_threads_block(
                editorial.detect_running_threads(recaps), _today_obj)
        except Exception as e:  # noqa: BLE001
            print(f"[editorial] running-thread detection failed (non-fatal): {e}", file=sys.stderr)

    results: dict[str, list[dict[str, str]]] = {}

    def _write(seg: dict[str, Any], topics: list[dict[str, Any]]) -> list[dict[str, str]]:
        key = seg["key"]
        timeline_block = ""
        if two_pass and recaps:
            try:
                timeline_block = _build_timeline_block(topics, recaps, _today_obj)
            except Exception as e:  # noqa: BLE001
                print(f"[editorial] timeline build failed for {key} (non-fatal): {e}", file=sys.stderr)
        # Prior context for tone continuity = bodies already written, in show order.
        prior = [results[s["key"]] for s in segments if s["key"] in results]
        return write_segment(seg, topics, markets, recap_block, prior, date_str,
                             editor_note, buzzing_block=(buzzing_block_str if key == "feature" else ""),
                             running_threads_block=running_block,
                             timeline_block=timeline_block, macro=macro)

    # 1) Write every body segment first (everything except the cold open).
    for seg in segments:
        key = seg["key"]
        if key == "cold_open":
            continue
        if key == "disagreement":
            topics = ((seg_picks.get("politics") or [])
                      + (seg_picks.get("feature") or [])
                      + (seg_picks.get("hard_news") or []))
        else:
            topics = seg_picks.get(key, [])
        results[key] = _write(seg, topics)

    # 2) Synthesize the cold open from the FINISHED bodies so it can't contradict them.
    cold_seg = next((s for s in segments if s["key"] == "cold_open"), None)
    if cold_seg is not None:
        results["cold_open"] = _write(cold_seg, _finished_show_digest(results, segments))

    # 3) Assemble in show order, prepending deterministic intro lines.
    all_lines: list[dict[str, str]] = []
    for seg in segments:
        lines = results.get(seg["key"], [])
        intro = SEGMENT_INTROS.get(seg["key"])
        if intro and lines:
            all_lines.append({"speaker": intro["speaker"], "text": intro["text"],
                              "segment": seg["key"], "segment_label": seg["label"],
                              "is_intro": True})
        all_lines.extend(lines)
    return all_lines




# ─── v7 EDITORIAL / MEMORY WIRING ────────────────────────────────────────────
# Thin glue between podcast.py and the pure memory/curator/editorial modules.
# Everything here is best-effort: a failure logs and degrades to v6.1 behaviour
# rather than killing the run.

def _build_timeline_block(topics: list[dict[str, Any]],
                          recaps: list[dict[str, Any]],
                          today: dt.date) -> str:
    """For topics the curator labelled update/thread_continuation, look up the
    prior episode's actual narration and render a writer-prompt timeline block."""
    updates: list[dict[str, Any]] = []
    for t in topics or []:
        if t.get("status") not in ("update", "thread_continuation"):
            continue
        ents = {e.lower() for e in (t.get("entities") or [])}
        ents |= editorial.entity_set(f"{t.get('headline', '')} {t.get('detail', '')}")
        prior = editorial.find_prior_narration(recaps, entities=ents, today=today)
        if prior:
            updates.append({"topic": t.get("headline", ""),
                            "update_note": t.get("update_note", ""), "prior": prior})
    return editorial.format_timeline_block(updates)


def run_two_pass_curation(curated: dict[str, Any], recap_block: str,
                          recaps: list[dict[str, Any]], date_str: str) -> dict[str, Any]:
    """Pass 2: label Pass-1 picks against memory and drop repeats (non-fatal)."""
    open_threads = memory.load_open_threads(RECAPS_DIR)
    weekly = memory.latest_weekly_digest(RECAPS_DIR)
    return curator_mod.memory_review(
        curated, recaps_block=recap_block, open_threads=open_threads,
        weekly_digest=weekly, date_str=date_str, chat_fn=llm_chat,
        model=CURATOR_MODEL, preamble=today_preamble(),
        log=lambda m: print(m, file=sys.stderr))


def update_open_threads_file(curated: dict[str, Any],
                             recaps: list[dict[str, Any]], date_str: str) -> None:
    """Evolve open_threads.json: age out dormant threads, upsert the arcs the
    curator flagged as updates/continuations this episode."""
    today = dt.date.fromisoformat(date_str)
    ot = memory.load_open_threads(RECAPS_DIR)
    memory.mark_dormant_threads(ot, today)
    for items in (curated.get("segments") or {}).values():
        for it in items or []:
            if it.get("status") not in ("update", "thread_continuation"):
                continue
            label = (it.get("headline") or "")[:90]
            tid = (it.get("relates_to_thread_id") or "").strip() or memory.slugify_thread(label)
            hooks = [it["update_note"]] if it.get("update_note") else []
            memory.upsert_thread(ot, thread_id=tid, label=label,
                                 episode_date=date_str, hooks=hooks)
    ot["updated"] = date_str
    path = memory.save_open_threads(RECAPS_DIR, ot)
    print(f"[open-threads] {len(ot.get('threads', []))} tracked → {path.name}", file=sys.stderr)


def maybe_generate_digests(date_str: str) -> None:
    """Generate weekly/monthly digests when due (Sunday / 1st) or missing.

    Each is independently guarded and non-fatal so digest generation can never
    block delivery of the episode.
    """
    today = dt.date.fromisoformat(date_str)

    # Weekly — for the week that just finished (or is finishing today if Sunday).
    try:
        is_sunday = today.isoweekday() == 7
        ref = today if is_sunday else today - dt.timedelta(days=today.isoweekday())
        mon, sun = memory.iso_week_bounds(ref)
        iso_wk = memory.iso_week_key(ref)
        if is_sunday or not memory.weekly_digest_exists(RECAPS_DIR, iso_wk):
            week_recaps = memory.load_daily_recaps_in_range(RECAPS_DIR, mon, sun)
            if week_recaps:
                digest = curator_mod.generate_weekly_digest(
                    week_recaps, iso_week=iso_wk, start_date=mon.isoformat(),
                    end_date=sun.isoformat(), chat_fn=llm_chat, model=RECAP_MODEL)
                path = memory.save_weekly_digest(RECAPS_DIR, digest)
                print(f"[digest] weekly {path.name}: {len(digest.get('threads', []))} threads",
                      file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[digest] weekly generation failed (non-fatal): {e}", file=sys.stderr)

    # Monthly — for the previous, just-finished month.
    try:
        last_prev = today.replace(day=1) - dt.timedelta(days=1)
        mkey = memory.month_key(last_prev)
        if today.day == 1 or not memory.monthly_digest_exists(RECAPS_DIR, mkey):
            month_recaps = memory.load_daily_recaps_in_range(
                RECAPS_DIR, last_prev.replace(day=1), last_prev)
            if month_recaps:
                digest = curator_mod.generate_monthly_digest(
                    month_recaps, month=mkey, chat_fn=llm_chat, model=RECAP_MODEL)
                path = memory.save_monthly_digest(RECAPS_DIR, digest)
                print(f"[digest] monthly {path.name}: {len(digest.get('threads', []))} threads",
                      file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[digest] monthly generation failed (non-fatal): {e}", file=sys.stderr)


# ─── TTS ─────────────────────────────────────────────────────────────────────

def openai_tts(text: str, voice: str, out_path: Path, retries: int = 6) -> None:
    """Generate one TTS clip via OpenAI. Honors Retry-After on 429."""
    payload = {"model": TTS_MODEL, "input": text, "voice": voice, "response_format": "mp3"}
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = httpx.post(
                OPENAI_TTS_URL, json=payload,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=180,
            )
            if r.status_code == 429:
                retry_after = r.headers.get("retry-after")
                wait = float(retry_after) if retry_after else (2 ** attempt) + 1
                wait = min(wait, 60)
                print(f"[tts] {out_path.name} 429, sleeping {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            out_path.write_bytes(r.content)
            return
        except Exception as e:
            last_err = e
            print(f"[tts] {out_path.name} attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"openai_tts failed for {out_path.name}: {last_err}")


def chunk_dialogue(dialogue: list[dict[str, str]]) -> list[tuple[str, str]]:
    """Group consecutive lines from same speaker into TTS-safe chunks."""
    chunks: list[tuple[str, str]] = []
    current_speaker: str | None = None
    current_parts: list[str] = []
    current_len = 0

    def voice_for(speaker: str) -> str:
        return HOST_A_VOICE if speaker == "kore" else HOST_B_VOICE

    def flush() -> None:
        nonlocal current_parts, current_len
        if current_speaker and current_parts:
            text = " ".join(current_parts).strip()
            if text:
                chunks.append((voice_for(current_speaker), text))
        current_parts = []
        current_len = 0

    for line in dialogue:
        speaker = line["speaker"]
        text = line["text"]
        # Strip stage directions
        text = re.sub(r"\[pause\]", "...", text, flags=re.IGNORECASE)
        text = re.sub(r"\[[^\]]+\]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        would_exceed = current_len + len(text) + 1 > TTS_CHUNK_CHAR_BUDGET
        if speaker != current_speaker or would_exceed:
            flush()
            current_speaker = speaker

        current_parts.append(text)
        current_len += len(text) + 1

    flush()
    return chunks


def generate_audio(dialogue: list[dict[str, str]], work_dir: Path) -> Path:
    """TTS all chunks, stitch with ffmpeg. Resumes existing chunks; raises on any missing."""
    work_dir.mkdir(parents=True, exist_ok=True)
    chunks = chunk_dialogue(dialogue)
    print(f"[audio] {len(chunks)} chunks to render", file=sys.stderr)

    chunk_files: list[Path] = [work_dir / f"chunk_{i:03d}.mp3" for i in range(len(chunks))]
    # Identify which need rendering (resume support)
    todo = [(i, voice, text) for i, (voice, text) in enumerate(chunks)
            if not (chunk_files[i].exists() and chunk_files[i].stat().st_size > 1000)]
    skipped = len(chunks) - len(todo)
    if skipped:
        print(f"[audio] resuming: {skipped} chunks already present, {len(todo)} to render", file=sys.stderr)

    failures: list[tuple[int, str]] = []
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        fut_to_idx = {
            pool.submit(openai_tts, text, voice, chunk_files[i]): i
            for (i, voice, text) in todo
        }
        for fut in concurrent.futures.as_completed(fut_to_idx):
            idx = fut_to_idx[fut]
            completed += 1
            try:
                fut.result()
                if completed % 10 == 0 or completed == len(todo):
                    print(f"[audio] {completed}/{len(todo)} chunks done", file=sys.stderr)
            except Exception as e:
                failures.append((idx, str(e)))
                print(f"[audio] chunk {idx} FAILED: {e}", file=sys.stderr)

    # Second pass: retry failures serially
    if failures:
        print(f"[audio] {len(failures)} chunks failed, retrying serially…", file=sys.stderr)
        still_failed = []
        for idx, _err in failures:
            voice, text = chunks[idx]
            try:
                openai_tts(text, voice, chunk_files[idx])
                print(f"[audio] chunk {idx} recovered on retry", file=sys.stderr)
            except Exception as e:
                still_failed.append((idx, str(e)))
                print(f"[audio] chunk {idx} FINAL FAIL: {e}", file=sys.stderr)
        if still_failed:
            raise RuntimeError(f"TTS unrecoverable failures: {still_failed}")

    # Verify all chunks present and non-trivially-sized
    missing = [i for i, p in enumerate(chunk_files) if not (p.exists() and p.stat().st_size > 1000)]
    if missing:
        raise RuntimeError(f"TTS chunks missing after render: {missing}")

    concat_list = work_dir / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.name}'" for p in chunk_files))

    final_path = work_dir / f"briefing_{dt.date.today().isoformat()}.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-acodec", "libmp3lame", "-b:a", "128k", "-ar", "44100",
        str(final_path),
    ], check=True, capture_output=True)

    for p in chunk_files:
        p.unlink(missing_ok=True)
    concat_list.unlink(missing_ok=True)

    return final_path


def get_audio_duration(mp3_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Short test run (3 segments)")
    parser.add_argument("--skip-tts", action="store_true", help="Stop after script + recap")
    parser.add_argument("--recap-only", action="store_true",
                        help="Regenerate today's recap from existing script.json")
    parser.add_argument("--from-script", action="store_true",
                        help="Skip research+curator+writer; render audio from existing script.json")
    parser.add_argument("--two-pass-curator", action=argparse.BooleanOptionalAction,
                        default=TWO_PASS_CURATOR_ENV,
                        help="v7 Tier 2/3: memory-review pass, story timeline, running "
                             "threads, weekly/monthly digests. Default ON; use "
                             "--no-two-pass-curator (or TWO_PASS_CURATOR=0) for v6.1.")
    args = parser.parse_args()
    two_pass = args.two_pass_curator
    print(f"[v7] two-pass curator + editorial intelligence "
          f"{'ENABLED' if two_pass else 'DISABLED (v6.1 fallback)'}", file=sys.stderr)

    today = dt.date.today().isoformat()
    work_dir = EPISODES_DIR / today
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # --from-script path: render TTS from an existing script.json (recovery / fast iteration)
    if args.from_script:
        script_path = work_dir / "script.json"
        if not script_path.exists():
            print(f"No script.json at {script_path}", file=sys.stderr)
            return 1
        dialogue = json.loads(script_path.read_text())
        total_words = sum(len(line["text"].split()) for line in dialogue)
        print(f"[from-script] loaded {len(dialogue)} lines, {total_words} words", file=sys.stderr)
        recap_path = RECAPS_DIR / f"{today}.json"
        if not recap_path.exists():
            recap = extract_recap(dialogue, today)
            recap_path.write_text(json.dumps(recap, indent=2))
        print("[from-script] Audio…", file=sys.stderr)
        final_mp3 = generate_audio(dialogue, work_dir)
        duration = get_audio_duration(final_mp3)
        result = {
            "status": "ok",
            "date": today,
            "mp3_path": str(final_mp3),
            "mp3_size_bytes": final_mp3.stat().st_size,
            "duration_seconds": round(duration, 1),
            "duration_minutes": round(duration / 60, 1),
            "line_count": len(dialogue),
            "word_count": total_words,
            "elapsed_sec": round(time.time() - t0, 1),
            "script_path": str(script_path),
            "recap_path": str(recap_path),
        }
        print(json.dumps(result))
        return 0

    # --recap-only path: skip everything, just rebuild recap
    if args.recap_only:
        script_path = work_dir / "script.json"
        if not script_path.exists():
            print(f"No script.json at {script_path}", file=sys.stderr)
            return 1
        dialogue = json.loads(script_path.read_text())
        recap = extract_recap(dialogue, today)
        recap_path = RECAPS_DIR / f"{today}.json"
        recap_path.write_text(json.dumps(recap, indent=2))
        print(json.dumps({"status": "recap_only", "recap_path": str(recap_path)}))
        return 0

    segments = SEGMENTS_TEST if args.test else SEGMENTS_FULL

    print("[1/6] Loading recap memory…", file=sys.stderr)
    recaps = load_recent_recaps()
    recap_block = format_recaps_for_writer(recaps)
    (work_dir / "recap_context.txt").write_text(recap_block)

    print(f"[2/7] Wide-net research ({len(BEATS)} beats)…", file=sys.stderr)
    research = gather_research()
    # Trusted, grounded news backbone: real RSS articles from major newsrooms. These
    # wire_* beats become the primary source for hard news (Sonar stays for discovery).
    research.update(gather_wire_news())
    (work_dir / "research.json").write_text(json.dumps(research, indent=2))
    total_cands = sum(len(v) for v in research.values())
    print(f"[research] {total_cands} total candidate topics", file=sys.stderr)

    print("[3/7] Markets…", file=sys.stderr)
    markets = gather_markets()
    (work_dir / "markets.txt").write_text(markets)
    macro = gather_macro()
    (work_dir / "macro.txt").write_text(macro)

    print("[4/7] Curator (rank + balance)…", file=sys.stderr)
    curated = curate_topics(research, recap_block, today)          # Pass 1: routing
    if two_pass:
        print("[4b/7] Memory review (Pass 2: new/update/repeat labels)…", file=sys.stderr)
        curated = run_two_pass_curation(curated, recap_block, recaps, today)
    curated = programmatic_dedupe(curated, recaps, today)          # hard safety net
    print("[4c/7] Currency check (drop stale/anniversary news)…", file=sys.stderr)
    curated = currency_check_news(curated, today)                  # drop prior-year events
    curated = enforce_news_floor(curated, research)                # hard_news/politics floor
    (work_dir / "curated.json").write_text(json.dumps(curated, indent=2))

    print(f"[5/7] Script ({len(segments)} segments, multi-pass)…", file=sys.stderr)
    dialogue = generate_script(curated, markets, recap_block, segments, today,
                               recaps=recaps, two_pass=two_pass, macro=macro)
    print("[5/6] Fact-checking numeric claims…", file=sys.stderr)
    dialogue, fc_log = fact_check_script(dialogue, today, markets=markets, macro=macro)
    for ln in fc_log:
        print(ln, file=sys.stderr)
    print("[5b/6] Recency audit (is the news current?)…", file=sys.stderr)
    dialogue, rec_log = recency_audit_script(dialogue, today)
    for ln in rec_log:
        print(ln, file=sys.stderr)
    (work_dir / "fact_check.log").write_text("\n".join(fc_log + rec_log))
    (work_dir / "script.json").write_text(json.dumps(dialogue, indent=2))
    total_words = sum(len(line["text"].split()) for line in dialogue)
    print(f"[script] {len(dialogue)} total lines, {total_words} words "
          f"(~{total_words/150:.1f} min target)", file=sys.stderr)

    print("[6/7] Recap extraction…", file=sys.stderr)
    recap = extract_recap(dialogue, today,
                          curated_segments=curated.get("segments") if two_pass else None)
    recap_path = RECAPS_DIR / f"{today}.json"
    recap_path.write_text(json.dumps(recap, indent=2))

    if two_pass:
        # Evolve the hot open-threads file and roll up weekly/monthly digests.
        # Both are best-effort — never block the episode on memory bookkeeping.
        try:
            update_open_threads_file(curated, recaps, today)
        except Exception as e:  # noqa: BLE001
            print(f"[open-threads] update failed (non-fatal): {e}", file=sys.stderr)
        maybe_generate_digests(today)

    if args.skip_tts:
        result = {
            "status": "skipped_tts",
            "date": today,
            "script_path": str(work_dir / "script.json"),
            "recap_path": str(recap_path),
            "line_count": len(dialogue),
            "word_count": total_words,
            "elapsed_sec": round(time.time() - t0, 1),
        }
        print(json.dumps(result))
        return 0

    print("[7/7] Audio…", file=sys.stderr)
    final_mp3 = generate_audio(dialogue, work_dir)
    duration = get_audio_duration(final_mp3)

    result = {
        "status": "ok",
        "date": today,
        "mp3_path": str(final_mp3),
        "mp3_size_bytes": final_mp3.stat().st_size,
        "duration_seconds": round(duration, 1),
        "duration_minutes": round(duration / 60, 1),
        "line_count": len(dialogue),
        "word_count": total_words,
        "elapsed_sec": round(time.time() - t0, 1),
        "research_path": str(work_dir / "research.json"),
        "curated_path": str(work_dir / "curated.json"),
        "script_path": str(work_dir / "script.json"),
        "recap_path": str(recap_path),
        "prior_recaps_used": len(recaps),
        "candidate_topics": total_cands,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
