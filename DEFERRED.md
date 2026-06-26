# Deferred from the Arun Daily Podcast Taste Spec (v8)

This pass shipped the **editorial core** of the taste spec: the ~120-minute
11-segment broadcast structure, the Host A / Host B archetypes, the topic
balance rules (daily minimums + caps, AI/tech explicitly capped), and
per-segment freshness windows — all running through the proven v6.x/v7
plumbing (real Yahoo markets, `today_preamble()`, programmatic dedupe, numeric
fact-check, buzzing-topics fetch with Sonar fallback, TTS `--from-script`
resume, recap-as-memory, v7 two-pass curator + open-threads/digests).

The items below were intentionally **not** built yet. They're the spec's new
data subsystems and supplements — each is fragile, paid, or net-new and wanted
its own validation rather than riding on tomorrow's live cron.

## Sourcing / data layer

- **X list-based feeds (`x_client.py`).** The spec's curated lists
  (`politics_institutional`, `macro_markets_contrarian`, `ai_builders_critics`,
  …) are not wired. Today we only have `fetch_buzzing_material()` which unfurls
  X URLs we already discovered via fxtwitter/fixupx. Planned upgrade: a
  `x_client.py` with two methods behind one interface — fxtwitter for known
  URLs + Nitter RSS (`nitter.<instance>/<handle>/rss`) for list feeds, with a
  pool of instances + health check and graceful fallback. Upgrade path if
  Nitter is too flaky: RSS.app (~$15/mo) or X Basic API ($200/mo).
- **Trends layer.** Trends24 / XTrending / X Explore / Trend Calendar scraping
  (HTTP + BeautifulSoup) to rank stories by live social salience. Not built;
  ranking currently uses the curator's `buzz` field only.
- **Beat tuning to named sources.** The 15 research beats are domain-broad but
  don't yet explicitly target the spec's example sources (Odd Lots, Acquired,
  Ezra Klein, BBC docs, Science News / New Scientist, Longform, etc.).
- **Paywall strategy.** No cookie-jar for FT/WSJ/NYT/Atlantic/New Yorker/
  Economist/Foreign Affairs. Current behavior = the v6.1 Sonar "find and quote
  the argument" fallback when an article body is unreachable. Decide which
  outlets you have subscriptions for and whether to wire credentials.
- **Markets skill.** Economy prices use the Yahoo v8 chart endpoint and macro
  releases now use real FRED data (`macro.py`) — both ground truth. The richer
  `finance/finance-markets` skill (pre-IPO names, debt, KPIs, options) is still
  an optional upgrade, not yet integrated.

## Editorial mechanisms

- **"Room for Disagreement" adversarial-pair finder.** The segment exists and
  is currently written from the day's politics/feature/hard-news picks with a
  prompt to stage one real disagreement. The spec wants the curator to actively
  search for a *specific published* adversarial pair (two named writers with
  opposing takes on the same story). That dedicated finder is not built.
- **Host-canon memory.** `hosts/host_a_canon.json` / `host_b_canon.json` —
  standing record of positions each host has taken + running gags/callbacks, so
  the writer stays consistent and the show feels serialized. Not built (the v7
  Tier-2 memory covers *story* memory, not *host* memory).

## Quality / feedback loop

- **Self-critique pass.** A post-script, pre-TTS LLM pass that reads the script
  against this spec and logs spec violations / out-of-character moments /
  AI-over-indexing (log only, no auto-rewrite). Not built.
- **Topic-touch frequency report.** Weekly minutes-per-domain rollup to catch
  the Tech/AI cap being violated over a trailing window. Not built.
- **Closed-loop chapter feedback.** Per-chapter one-click thumbs-up/down links
  in the delivery email writing back to a repo feedback file. Needs an endpoint
  to receive clicks; not built.

## Resolved after the v8 transcript review

- **Macro correctness** — the economy segment is no longer exempt from the
  numeric fact-check (it only shielded hallucinated macro like core PCE "2.6%"
  when it had really risen to 3.4%). Now every segment is fact-checked and only
  the exact injected ground-truth numbers (Yahoo prices) are protected, so
  macro claims get web-verified and corrected. A FRED fetch (`macro.py`) is
  attempted as a bonus but is BLOCKED on GitHub's runners (ReadTimeout); it
  fails fast and the fact-check is the real macro safety net. Wiring a
  runner-reachable macro source (FRED API key, or a data MCP) is a future
  upgrade if hard ground truth is wanted.
- **US politics sourcing** — dedicated `us_politics` research beat added.
- **Cold open** — now synthesized from the finished body segments, so the
  headline scan can't contradict the body.
- **Recency** — a recency-audit pass re-anchors stale events (e.g. an old NBA
  result narrated as "Thursday") against today's date.

Still open on the editorial side: the dedicated adversarial-pair finder, host-
canon memory, self-critique pass, topic-touch report, and the X/trends layers
above.

## Notes

- Length is now ~120 min (~18.5k words). This roughly doubles OpenAI TTS cost
  and render time vs. the old ~70-min show — expected, per the chosen config.
- Kill-switch unchanged: `TWO_PASS_CURATOR=0` / `--no-two-pass-curator` reverts
  the v7 memory/editorial layer; the v8 structure/hosts remain.
