#!/usr/bin/env python3
"""
Monthly precipitation ladder analyzer (Kalshi 'Above X inches' markets).

LAYER 1 of the plan -- Kalshi side only, no weather data yet.

These markets are a survival function: for rising thresholds X, the price of
"Above X inches" = P(monthly total > X). This tool:

  1. Pulls the nearest event's ladder for a monthly series (e.g. KXRAINMIAM).
  2. Converts the mid-price survival curve into an implied probability
     distribution over the buckets, and an implied mean.
  3. Runs the monotonicity arb check: P(>low) must be >= P(>high). A rung
     where the EASIER threshold trades cheaper than a HARDER one is a lock.
  4. Flags each rung's spread / volume / OI so you can see where the tail is
     plausibly mispriced but too thin to fill.

What it deliberately does NOT do yet (Layer 2, requires weather data):
  - compare the market curve against an INDEPENDENT probability estimate
    built from month-to-date observed precip + a remainder model. Without
    that, this only tells you what the market thinks and whether it's
    internally consistent -- NOT whether a price is wrong.

Usage:
  python3 scan_monthly.py                       # Miami + St Pete + NYC
  python3 scan_monthly.py --series KXRAINMIAM
  python3 scan_monthly.py --series KXRAINSTPM KXRAINLAXM --event-index 0
"""

import argparse
import json
import re
import sys
import urllib.request
from collections import defaultdict

API = "https://api.elections.kalshi.com/trade-api/v2"
NWS = "https://api.weather.gov"
NWS_UA = "kalshi-mtd/1.0 (research; +https://github.com/tbnmichaud-sys/kalshi-scanner)"
DEFAULT = ["KXRAINMIAM", "KXRAINSTPM", "KXRAINNYCM"]

THRESH_RE = re.compile(r"above\s+([0-9]+(?:\.[0-9]+)?)\s*inch", re.I)
# Settlement rule names the station as CLI<code>, e.g. "at CLIMIA" -> MIA.
CLI_RE = re.compile(r"\bCLI([A-Z]{2,4})\b")
# Fallback when the rule spells the site out instead of using a CLI code.
NAME_TO_CLI = [("central park", "NYC"), ("kennedy", "JFK"), ("laguardia", "LGA")]
MTD_RE = re.compile(r"MONTH TO DATE\s+(T|[0-9]+\.[0-9]+)")
# Event tickers end in -<YY><MON>, e.g. KXRAINMIAM-26AUG.
EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})$")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def event_month(etk):
    """(year, month) parsed from an event ticker, or None if unparseable."""
    m = EVENT_DATE_RE.search(etk or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return 2000 + int(m.group(1)), MONTHS[m.group(2)]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-scan/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def nws_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": NWS_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_mtd(cli_code):
    """Month-to-date precip (inches) from the latest NWS CLI report -- the
    exact product Kalshi settles on. Returns (mtd_inches, raw_line) or
    (None, reason). 'T' (trace) is treated as 0.00 but noted."""
    try:
        idx = nws_get(f"{NWS}/products/types/CLI/locations/{cli_code}")
        prods = idx.get("@graph", [])
        if not prods:
            return None, "no CLI product"
        txt = nws_get(f"{NWS}/products/{prods[0]['id']}").get("productText", "")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    m = MTD_RE.search(txt)
    if not m:
        return None, "MONTH TO DATE not found in report"
    raw = m.group(1)
    val = 0.0 if raw == "T" else float(raw)
    return val, ("trace (0.00)" if raw == "T" else f"{val:.2f} in")


def fnum(v):
    return None if v is None else float(v)


def load_ladder(series, event_index):
    d = get(f"{API}/markets?limit=100&status=open&series_ticker={series}")
    ms = d.get("markets", [])
    if not ms:
        return None, None, None
    events = defaultdict(list)
    for m in ms:
        events[m["event_ticker"]].append(m)
    # Chronological, NOT alphabetical: "-26AUG" sorts before "-26JUL" as a
    # string, which made "nearest event" flip to next month early.
    ev_tickers = sorted(events.keys(),
                        key=lambda e: (event_month(e) or (9999, 99), e))
    if event_index >= len(ev_tickers):
        return None, None, None
    etk = ev_tickers[event_index]
    cli_code = None
    rungs = []
    for m in events[etk]:
        if cli_code is None:
            rule = m.get("rules_primary") or ""
            cm = CLI_RE.search(rule)
            if cm:
                cli_code = cm.group(1)
            else:
                low = rule.lower()
                for name, code in NAME_TO_CLI:
                    if name in low:
                        cli_code = code
                        break
        sub = m.get("yes_sub_title") or m.get("subtitle") or ""
        mt = THRESH_RE.search(sub)
        if not mt:
            continue
        bid, ask = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
        if bid is None or ask is None:
            continue
        rungs.append({
            "x": float(mt.group(1)),
            "label": sub.strip(),
            "bid": round(bid * 100),
            "ask": round(ask * 100),
            "mid": round((bid + ask) * 50),
            "spread": round((ask - bid) * 100),
            "vol": fnum(m.get("volume_fp")) or 0.0,
            "oi": fnum(m.get("open_interest_fp")) or 0.0,
        })
    rungs.sort(key=lambda r: r["x"])
    return etk, rungs, cli_code


def analyze(series, event_index):
    etk, rungs, cli_code = load_ladder(series, event_index)
    if not rungs:
        print(f"\n{series}: no parseable ladder.")
        return

    mtd, mtd_note = (None, "no station code in rule")
    if cli_code:
        mtd, mtd_note = fetch_mtd(cli_code)

    print("\n" + "=" * 74)
    print(f"{series}   event {etk}   station CLI{cli_code or '?'}   "
          f"month-to-date: {mtd_note}")
    print("=" * 74)
    print(f"  {'threshold':<12}{'bid':>5}{'ask':>5}{'mid':>5}{'spr':>5}"
          f"{'P(bucket)':>11}{'needs':>8}{'vol':>9}{'oi':>8}")

    # Implied bucket probabilities from the mid survival curve.
    # bucket i = (x_i, x_{i+1}] has prob S(x_i) - S(x_{i+1}); top rung = S(x_max).
    locks = []  # rungs the observed floor has ALREADY settled YES
    for i, r in enumerate(rungs):
        s_here = r["mid"]
        s_next = rungs[i + 1]["mid"] if i + 1 < len(rungs) else 0
        pbucket = s_here - s_next  # in "cents" == % points
        # Floor logic: rule settles YES if final total > threshold. Precip only
        # accumulates, so if month-to-date already > threshold, YES is certain.
        needs = ""
        if mtd is not None:
            if mtd > r["x"]:
                needs = "SETTLED"
                if r["ask"] < 99:      # fair value ~100, buyable below it
                    locks.append(r)
            else:
                needs = f"+{r['x'] - mtd:.2f}\""
        tail = "  <-- fat tail (thin/wide)" if (i >= len(rungs) - 2
                                                and r["spread"] >= 6) else ""
        print(f"  >{r['x']:<11}{r['bid']:>5}{r['ask']:>5}{r['mid']:>5}"
              f"{r['spread']:>5}{pbucket:>9}pp{needs:>8}{r['vol']:>9.0f}"
              f"{r['oi']:>8.0f}{tail}")

    if mtd is not None and locks:
        print("\n  FLOOR LOCKS (observed MTD already exceeds threshold -> YES certain):")
        for r in locks:
            print(f"    >{r['x']}\" is SETTLED yet asks {r['ask']}c "
                  f"-> buy YES for ~{100 - r['ask']}c edge "
                  f"(min size {r['vol']:.0f}, spread {r['spread']}c)")
        print("    ^ verify the latest CLI report is post-dated to today "
              "before trusting (reports lag ~1 morning).")

    # Implied mean (lower bound: uses rung midpoints; open top tail ignored).
    # E[total] approx sum over buckets of bucket_prob * bucket_midpoint.
    mean = 0.0
    for i, r in enumerate(rungs):
        lo = r["x"]
        hi = rungs[i + 1]["x"] if i + 1 < len(rungs) else r["x"] + 1.0  # crude
        s_here = r["mid"] / 100.0
        s_next = (rungs[i + 1]["mid"] / 100.0) if i + 1 < len(rungs) else 0.0
        mean += (s_here - s_next) * ((lo + hi) / 2.0)
    print(f"\n  implied mean total ~ {mean:.2f} in (crude; open top tail ignored)")

    # Monotonicity arb: easier threshold must not trade cheaper than harder one.
    # Lock if you can BUY >low at ask cheaper than you SELL >high at bid.
    print("  consistency (survival must be non-increasing):")
    flagged = False
    for i in range(len(rungs) - 1):
        low, high = rungs[i], rungs[i + 1]
        if low["ask"] < high["bid"]:
            gain = high["bid"] - low["ask"]
            print(f"    !! ARB: buy >{low['x']} @ {low['ask']}  /  "
                  f"sell >{high['x']} @ {high['bid']}  -> +{gain}pp locked, "
                  f"min size {min(low['vol'], high['vol']):.0f}")
            flagged = True
    if not flagged:
        print("    ok - survival curve is monotone within the spread.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", default=DEFAULT)
    ap.add_argument("--event-index", type=int, default=0,
                    help="0 = nearest-settling event")
    args = ap.parse_args()
    for s in args.series:
        try:
            analyze(s, args.event_index)
        except Exception as e:
            print(f"{s}: ERROR {type(e).__name__}: {e}", file=sys.stderr)
    print("\nNOTE: Layer 1 shows the market's own view + internal consistency.")
    print("It does NOT judge if a price is wrong -- that needs the month-to-date")
    print("+ remainder model (Layer 2). Do not trade tail flags on this alone.")


if __name__ == "__main__":
    main()
