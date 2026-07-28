# Kalshi cross-bucket scanner

Zero-dependency Python. Checks mutually-exclusive markets (one bucket must
resolve YES) for logical price inconsistencies. It does **not** forecast — it
only asks whether prices that must sum to 100¢ actually do.

```bash
python3 scan.py                                # default scalar temp series
python3 scan.py --series KXHIGHNY KXHIGHCHI    # pick series
python3 scan.py --all --min-legs 3 --top 30    # every open market (slow/noisy)
```

## Reading the output

- **CAPTURABLE LOCKS** — the only section that's a real edge. Fires when
  `sum(yes_bid) > 100` (sell every bucket) or `sum(yes_ask) < 100` (buy every
  bucket), *after* an estimated Kalshi fee. `maxSets` = smallest quote size on
  the side you'd hit, i.e. how many locked sets you could actually fill.
- **OVERROUND LEADERBOARD** — informational. `sum(ask) − 100` is the book's
  vig. High = expensive book. This is the edge the market takes from *you*,
  not free money.
- **STALE PRINTS** — last trade outside the live bid/ask. Thin-market noise,
  not edge.

## Before trusting any flag

1. **Verify MECE** on the site — the math is only valid if the event's
   markets are one partition (exactly one resolves YES). Some grouped events
   are independent yes/no questions; the sums mean nothing there. `--series`
   with scalar temperature series is the safe case.
2. **Check depth** — `maxSets`, volume, OI. A 1-contract flag isn't a fill.
3. **Confirm the live fee** — the estimate is `~7% · p · (1−p)` per contract
   per side; verify against Kalshi's current schedule before sizing.
4. Quotes move; the envelope usually closes before you can act on all legs.

API: `https://api.elections.kalshi.com/trade-api/v2` (public, no auth).

---

# scan_monthly.py — monthly precipitation ladder + MTD floor

Monthly "Above X inches" rain markets (e.g. `KXRAINMIAM`, `KXRAINSTPM`,
`KXRAINNYCM`) are a survival curve: price of "Above X" = P(monthly total > X).
Unlike the daily rain book, these are genuinely liquid (10k+ contracts) with
tradeable middle spreads.

```bash
python3 scan_monthly.py                          # Miami + St Pete + NYC
python3 scan_monthly.py --series KXRAINMIAM
python3 scan_monthly.py --series KXRAINSTPM --event-index 0   # 0 = nearest
```

For each city it prints the ladder, converts it to an implied probability
distribution, computes a crude implied mean, runs the monotonicity arb check,
and — the Layer 2a addition — pulls **month-to-date observed precip from the
NWS CLI report** (the exact product Kalshi settles on; station auto-detected
from the market's own settlement rule).

Columns: `P(bucket)` = implied prob of landing in that rung; `needs` = inches
still required to cross the threshold (or `SETTLED` if the observed floor has
already cleared it).

**FLOOR LOCKS**: because precip only accumulates, any rung whose threshold is
already below month-to-date is a certain YES. If such a rung still asks < 99¢,
that's a near-lock. Caveat printed in-tool: the CLI report lags ~1 morning, so
confirm the latest report is current before trusting a flag.

What this still does NOT do (Layer 2b, not built): model the *remaining* days
of the month (short-range ensemble for near days + climatology for the rest)
to produce an independent survival curve and judge whether the unsettled rungs
are mispriced. That's the real thesis test; MTD floor is the cheap first cut.

Honest note from live data: the fat right tail (e.g. Miami `>7"`, St Pete
`>9/10"`) is where mispricing would hide, but those rungs show single-digit
to ~40-contract volume — plausibly mispriced yet unfillable at size. And the
market already prices a heavy open tail (Miami `>7"` carried more probability
than the `6–7"` bucket), so the tail is not naively "underrated".

Data: NWS CLI via `https://api.weather.gov/products/types/CLI/locations/<code>`
(public, no key; set a real contact in the User-Agent).

---

# scan_forecast.py — ensemble remainder model vs market (Layer 2b)

For each unsettled rung, projects the final monthly total per ensemble member
(GEFS+ICON, ~71 members, free Open-Meteo ensemble API) as MTD + member's
remaining-days precip, then `P(final>threshold)` = member fraction. Compares to
the price you'd actually trade at (ask to buy, bid to sell) net of a fee buffer.

```bash
python3 scan_forecast.py            # all cities; prints a cross-city verdict
```

**Read the verdict, not individual flags.** One-signed flags across all cities =
systematic bias (the ensemble under-forecasts convective extremes), NOT edge.
Mixed/small = efficient market. This tool is built to *disprove* a strategy;
it cannot confirm one — only settlement can. (July 2026 run: 6 sell-tail flags,
judged dry-bias, no edge.)

---

# monitor_rain.py — intraday floor-lock monitor (the live candidate)

`live_MTD = CLI-through-yesterday + today's METAR accumulation`. Flags any rung
where live_MTD has already crossed the threshold (certain YES) but the market
still asks < 99c — the lag between the hourly gauge and the once-a-day CLI
report the market anchors to. This is the one mechanical, no-forecasting edge,
and it gives feedback without waiting for month-end (a lock is verifiable the
instant precip banks).

```bash
python3 monitor_rain.py --watch 900     # re-poll every 15 min
```

Opportunistic: fires only when it's actively raining near a bucket boundary, so
most polls show nothing. Watch cheap-and-close rungs (e.g. a rung priced a few
cents that's < 0.5" away) — that's where a lock pays. Caveats: ASOS gauges
under-catch heavy precip (live_MTD is a floor), and you're racing anyone else
watching METAR.

---

# watcher.py — background notify-only watcher (Phase 1)

Set-and-forget daemon. Polls every city's gauge each cycle (no forecast gate —
forecasts miss pop-up convection, and cheap polling means no reason to skip),
and notifies you ONCE per floor-lock (deduped in `watcher_state.json`). Reads
public data only — never authenticates, never trades. Auto-execution is Phase 2
(opt-in, credentialed).

```bash
# quick background run:
nohup python3 watcher.py > watcher.log 2>&1 &
tail -f watcher.log

# robust (survives logout/reboot):
mkdir -p ~/.config/systemd/user
cp watcher.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now watcher.service
journalctl --user -u watcher -f
```

Notifications: Linux desktop via `notify-send`, and/or phone/chat if you set
`KALSHI_NOTIFY_WEBHOOK` to an ntfy.sh topic URL (`https://ntfy.sh/<topic>`, free,
has a phone app) or a Slack incoming-webhook URL.

Auto-journal: every fired signal (all tiers) is appended to `trades.csv` as a
paper entry (filled at the alert ask), tagged `AUTO Tier<n>`, deduped so each
fires once. `log_bet.py --show` lists them; settle at month-end with
`log_bet.py --settle <id> --result yes|no`. Disable with `--no-journal`. This is
what makes validation automatic — a season of auto-logged signals, settled, tells
you whether any tier's entry prices actually beat their outcomes.

Two alert tiers (iron tradeoff — a signal can be certain, frequent, or
high-payout; pick two):
- **Tier 1 FLOOR-LOCK (certain, rare, tiny)** — a rung the live gauge has
  already crossed but the once-a-day CLI report hasn't, still asking < 99¢.
- **Tier 2 APPROACH (speculative, frequent, big payout)** — a cheap
  (`ask ≤ --approach-ask`, default 40¢), close (`gap ≤ --approach-gap`, default
  0.5"), liquid rung *while rain is actively falling at the gauge*. NOT a lock:
  you're betting the in-progress storm delivers the rest, and can lose. This is
  the tier that actually tests "does the market react slowly to a live storm."
- **Tier 3 NEXT-LEVEL (predictive delta, unproven)** — for each cheap unsettled
  rung, model `P(reach it)` = fraction of ensemble members where live-MTD +
  member remainder clears the threshold; flags when that beats the ask by
  `--nextlevel-edge` (default 8¢). This targets the *higher* rung that still has
  margin, not the already-locked one. CAVEAT: the ensemble is convective-dry-
  biased, so it tends to say the tail is *less* likely, not more — treat any
  flag as a lead to verify, and let the journal decide if the delta predicts.
  The heartbeat prints the biggest live delta every cycle.
- **Tier 4 SURPRISE-LAG (the edge that survives scrutiny)** — diffs each cycle
  against the last (`watcher_prev.json`). Flags a next-level rung ONLY when all
  hold: (a) the gauge banked `≥ --bank-epsilon`" (default 0.05") since last
  cycle; (b) today's actual precip *beat the forecast* by `≥ --surprise-eps`
  (default 0.10") — a genuine surprise the priced-in prediction missed; and
  (c) the rung's ask moved `≤ --stale-tol`¢ (default 1), i.e. the market hasn't
  yet absorbed the surprise. The surprise gate is essential: rain that merely
  arrived on schedule leaves a *correctly* flat price, not an edge. Caveats: a
  surprise is by definition unpredictable, so this is a reaction-speed race —
  it only pays if the market is slower than the poll interval (the slow-market
  hypothesis) and the forecast baseline is itself noisy for convection, so let
  the journal confirm the flagged rungs actually over-resolve.

Tuning: `--skip` (drop cities, e.g. summer-dry `KXRAINLAXM KXRAINSFOM
KXRAINSEAM`), `--min-size` (min rung volume, default 100), `--fee` (edge buffer),
`--approach-ask` / `--approach-gap` (Tier 2 sensitivity), `--fast` (poll cadence).
Each cycle logs a heartbeat with the closest-to-locking rung so you can see it's
alive even when nothing fires.

## Phase 2 — auto-execution (not enabled)

`place_bet()` in watcher.py is a deliberate stub. Enabling live trading needs:
dry-run default, per-trade + daily $ caps, min-liquidity/max-price guards,
idempotency (never double-fill a lock), a kill-file, and Kalshi's authenticated
trading API (API key + RSA key — different endpoint, subject to their rate
limits and conduct terms). Build it only after Phase 1 has caught a real lock.
