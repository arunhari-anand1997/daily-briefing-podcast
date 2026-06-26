"""macro.py — real macroeconomic data from FRED, as ground truth for the show.

The economy segment used to take its macro numbers (core PCE, CPI, the Fed funds
rate, unemployment) from Sonar, which hallucinated them — e.g. reporting core PCE
"cooled to 2.6%" when it had actually risen to 3.4%. Yahoo grounds *prices* but
not macro *releases*, so this module pulls the real series from FRED and feeds
them into the writer as verbatim ground truth, exactly like the Yahoo overlay.

FRED's public CSV endpoint needs no API key:
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>
We use a proxy-bypassing opener (same reason as Yahoo: the agent sandbox routes
HTTPS through a credential proxy that doesn't intercept FRED). On a normal server
ProxyHandler({}) is simply a no-op.

Pure parsing/formatting is unit-tested; the network fetch is best-effort and
non-fatal (the caller falls back to the prior Sonar path).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import urllib.request as _urlreq
from typing import Callable

# Network fetch is dependency-injected (FetchFn: url -> csv text) so the parsing
# logic stays unit-testable and the caller can supply a robust client. podcast.py
# passes an httpx-based fetcher (handles gzip/redirects/TLS, proven on the runner);
# the urllib default below is only a standalone fallback.
FetchFn = Callable[[str], str]

_FRED_OPENER = _urlreq.build_opener(_urlreq.ProxyHandler({}))
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={cosd}"


def _default_fetch(url: str, timeout: float = 15.0) -> str:
    req = _urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0 (podcast-macro)"})
    with _FRED_OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

# (series_id, label, kind). kind drives how we present it:
#   "yoy"   — monthly price index → report year-over-year % change + the index month
#   "level" — report the latest value as-is (a rate/percent already)
#   "pct"   — already a percent-change figure (e.g. real GDP SAAR)
_SERIES: list[tuple[str, str, str]] = [
    ("PCEPILFE", "Core PCE inflation (YoY)", "yoy"),
    ("PCEPI",    "Headline PCE inflation (YoY)", "yoy"),
    ("CPILFESL", "Core CPI inflation (YoY)", "yoy"),
    ("CPIAUCSL", "Headline CPI inflation (YoY)", "yoy"),
    ("DFEDTARU", "Fed funds target (upper bound)", "level"),
    ("DFEDTARL", "Fed funds target (lower bound)", "level"),
    ("UNRATE",   "Unemployment rate", "level"),
    ("A191RL1Q225SBEA", "Real GDP (QoQ, annualized)", "pct"),
]


def parse_fred_csv(text: str) -> list[tuple[dt.date, float]]:
    """Parse a FRED fredgraph.csv payload into [(date, value)], dropping '.' gaps."""
    out: list[tuple[dt.date, float]] = []
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return out
    # First column is the observation date; the value column is the second.
    for row in rows[1:]:
        if len(row) < 2:
            continue
        raw_date, raw_val = row[0].strip(), row[1].strip()
        if not raw_date or raw_val in ("", "."):
            continue
        try:
            d = dt.date.fromisoformat(raw_date)
            v = float(raw_val)
        except ValueError:
            continue
        out.append((d, v))
    out.sort(key=lambda x: x[0])
    return out


def yoy_change(obs: list[tuple[dt.date, float]]) -> tuple[float, dt.date] | None:
    """Year-over-year % change of a monthly index: latest vs the obs ~12 months prior."""
    if len(obs) < 13:
        return None
    latest_date, latest_val = obs[-1]
    target = latest_date.replace(year=latest_date.year - 1)
    # nearest observation on/before the target month
    prior = None
    for d, v in obs:
        if d <= target:
            prior = (d, v)
        else:
            break
    if prior is None or prior[1] == 0:
        return None
    return (latest_val / prior[1] - 1.0) * 100.0, latest_date


def _fmt_month(d: dt.date) -> str:
    return d.strftime("%b %Y")


def build_macro_snapshot(today: dt.date | None = None,
                         fetch_fn: FetchFn | None = None,
                         log: Callable[[str], None] | None = None) -> str:
    """Fetch the macro series and render a ground-truth block, or '' if unreachable.

    ``fetch_fn(url) -> csv_text`` is injected; defaults to a urllib fetcher. The
    first fetch failure is reported via ``log`` so an empty result is diagnosable
    rather than silent.
    """
    today = today or dt.date.today()
    fetch_fn = fetch_fn or _default_fetch
    cosd = (today - dt.timedelta(days=900)).isoformat()  # ~2.5y of history for YoY
    lines: list[str] = []
    funds = {}
    first_err_logged = False
    for sid, label, kind in _SERIES:
        try:
            obs = parse_fred_csv(fetch_fn(_FRED_CSV.format(sid=sid, cosd=cosd)))
        except Exception as e:  # noqa: BLE001
            # A fetch error means FRED is unreachable (it is blocked from some
            # CI runners). Bail immediately rather than timing out on all series.
            if log:
                log(f"[macro] fetch failed for {sid}: {type(e).__name__}: {e} — skipping FRED")
            return ""
        if not obs:
            if log and not first_err_logged:
                log(f"[macro] empty/unparseable CSV for {sid}")
                first_err_logged = True
            continue
        if kind == "yoy":
            res = yoy_change(obs)
            if res is None:
                continue
            pct, asof = res
            lines.append(f"- {label}: {pct:+.1f}% as of {_fmt_month(asof)}")
        elif kind == "pct":
            d, v = obs[-1]
            lines.append(f"- {label}: {v:+.1f}% (latest: {_fmt_month(d)})")
        else:  # level
            d, v = obs[-1]
            if sid in ("DFEDTARU", "DFEDTARL"):
                funds[sid] = v
                continue
            lines.append(f"- {label}: {v:.1f}% as of {d.isoformat()}")
    if "DFEDTARL" in funds and "DFEDTARU" in funds:
        lines.append(f"- Fed funds target range: {funds['DFEDTARL']:.2f}%–{funds['DFEDTARU']:.2f}%")
    if not lines:
        return ""
    header = (
        f"LIVE MACRO SNAPSHOT (FRED official data, fetched {today.isoformat()}):\n"
        "These are the REAL, current macro figures. Use them VERBATIM. Do NOT substitute "
        "Sonar/training-data values for inflation, rates, unemployment, or GDP. Note the "
        "'as of' month — the latest release may differ from older numbers in the research.\n"
    )
    return header + "\n".join(lines) + "\n"
