"""Unit tests for macro.py FRED parsing + YoY (no network)."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import macro  # noqa: E402


def test_parse_fred_csv_drops_gaps_and_sorts():
    text = ("observation_date,PCEPILFE\n"
            "2026-03-01,120.0\n"
            "2026-04-01,.\n"          # missing value dropped
            "2026-05-01,121.2\n"
            "bad,row\n")              # unparseable dropped
    obs = macro.parse_fred_csv(text)
    assert obs == [(dt.date(2026, 3, 1), 120.0), (dt.date(2026, 5, 1), 121.2)]


def test_parse_fred_csv_empty():
    assert macro.parse_fred_csv("") == []
    assert macro.parse_fred_csv("observation_date,X\n") == []


def test_yoy_change_basic():
    # 13 monthly obs; latest 3.4% above the value 12 months earlier
    obs = [(dt.date(2025, 5, 1), 100.0)]
    for i in range(1, 12):
        obs.append((dt.date(2025, 5 + i, 1) if 5 + i <= 12
                    else dt.date(2026, (5 + i) - 12, 1), 100.0))
    obs.append((dt.date(2026, 5, 1), 103.4))
    pct, asof = macro.yoy_change(obs)
    assert round(pct, 1) == 3.4
    assert asof == dt.date(2026, 5, 1)


def test_yoy_change_needs_13_points():
    obs = [(dt.date(2026, m, 1), 100.0) for m in range(1, 6)]
    assert macro.yoy_change(obs) is None


def test_build_macro_snapshot_with_injected_fetch():
    # 13 monthly rows so YoY works; +3.4% for the index series.
    def fake_fetch(url):
        rows = ["observation_date,V"]
        base = 100.0
        for m in range(13):
            y, mo = (2025, 5 + m) if 5 + m <= 12 else (2026, (5 + m) - 12)
            val = base if m < 12 else base * 1.034
            rows.append(f"{y}-{mo:02d}-01,{val:.3f}")
        return "\n".join(rows)
    snap = macro.build_macro_snapshot(today=dt.date(2026, 6, 1), fetch_fn=fake_fetch)
    assert "LIVE MACRO SNAPSHOT" in snap
    assert "+3.4%" in snap  # YoY computed from the injected data


def test_build_macro_snapshot_logs_and_returns_empty_on_failure():
    msgs = []
    def boom(url):
        raise RuntimeError("blocked")
    snap = macro.build_macro_snapshot(today=dt.date(2026, 6, 1),
                                      fetch_fn=boom, log=msgs.append)
    assert snap == ""
    assert msgs and "fetch failed" in msgs[0]


def test_yoy_change_uses_nearest_prior_month():
    obs = [(dt.date(2025, 1, 1), 200.0)]
    obs += [(dt.date(2025, m, 1), 200.0) for m in range(2, 13)]
    obs.append((dt.date(2026, 1, 1), 208.0))  # +4% vs 2025-01
    pct, asof = macro.yoy_change(obs)
    assert round(pct, 1) == 4.0
