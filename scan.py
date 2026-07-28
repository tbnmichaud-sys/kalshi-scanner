#!/usr/bin/env python3
"""
Kalshi cross-bucket consistency scanner.

What it does (and what it does NOT do):

  This finds LOGICAL inconsistencies in mutually-exclusive, collectively-
  exhaustive (MECE) markets -- e.g. a daily-high-temperature event where
  exactly one bucket must resolve YES. For such an event:

      - Buying YES on every bucket costs   sum(yes_ask).  You get back 100c.
        -> guaranteed profit only if        sum(yes_ask) < 100c   (rare).
      - Selling YES on every bucket collects sum(yes_bid). You pay out 100c.
        -> guaranteed profit only if        sum(yes_bid) > 100c   (rarer).

  sum(yes_ask) - 100  is the "overround" (the book's vig). It's normally
  POSITIVE and is NOT free money -- it's the edge the market takes from you.
  A real, capturable lock exists ONLY when sum(yes_bid) > 100 + fees, or
  sum(yes_ask) < 100 - fees. Those are what this flags as ARB.

  This does NOT forecast anything. It does not know if a price is "right".
  It only checks whether a set of prices that MUST sum to 100 actually does.

Hard caveats before you trade on any flag:
  * MECE assumption: the script assumes every market under one event is one
    bucket of a partition. Verify that on the site -- some events are NOT a
    partition (independent yes/no questions grouped together) and the math
    is meaningless there. Use --series to restrict to scalar series you trust.
  * Depth: a flag at 1-contract size is not a fill. Check volume/OI/quote size.
  * Fees: Kalshi charges ~7% * price * (1-price) per contract, per side.
    We subtract a conservative estimate but confirm against the live fee.
  * Quotes move. By the time you act the envelope is usually gone.

Usage:
  python3 scan.py                      # scan a default set of scalar series
  python3 scan.py --series KXHIGHNY KXHIGHCHI
  python3 scan.py --all                # scan every open market (slow, noisy)
  python3 scan.py --min-legs 3 --top 25
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from collections import defaultdict

API = "https://api.elections.kalshi.com/trade-api/v2"

# Scalar/range series that are genuinely MECE partitions -- safe defaults.
DEFAULT_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHMIA", "KXHIGHDEN",
    "KXHIGHPHIL", "KXHIGHAUS",
]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-scan/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_markets_by_series(series_ticker):
    """All open markets for one series, with their event grouping."""
    out = []
    cursor = None
    while True:
        url = f"{API}/markets?limit=1000&status=open&series_ticker={series_ticker}"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            data = get(url)
        except urllib.error.HTTPError as e:
            print(f"  ! {series_ticker}: HTTP {e.code}", file=sys.stderr)
            break
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def fetch_all_open_markets():
    out = []
    cursor = None
    while True:
        url = f"{API}/markets?limit=1000&status=open"
        if cursor:
            url += f"&cursor={cursor}"
        data = get(url)
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def norm(m):
    """Normalize a market dict: cents ints for prices, floats for size/vol.
    The API serves prices as *_dollars and quantities as *_fp. Returns None
    for a leg with no two-sided quote (can't be part of a tradable lock)."""
    def f(key):  # API serves these as strings ("0.0100") or None
        v = m.get(key)
        return None if v is None else float(v)

    b, a = f("yes_bid_dollars"), f("yes_ask_dollars")
    if b is None or a is None:
        return None
    last = f("last_price_dollars")
    return {
        "ticker": m.get("ticker"),
        "yes_bid": round(b * 100),
        "yes_ask": round(a * 100),
        "last_price": None if last is None else round(last * 100),
        "volume": f("volume_fp") or 0.0,
        "open_interest": f("open_interest_fp") or 0.0,
        "bid_size": f("yes_bid_size_fp") or 0.0,
        "ask_size": f("yes_ask_size_fp") or 0.0,
    }


def fee_estimate(price_cents):
    """Conservative per-contract fee estimate in cents.
    Kalshi general fee ~ ceil(0.07 * p * (1-p)) with p in dollars, *100 -> cents.
    """
    p = price_cents / 100.0
    return 7.0 * p * (1.0 - p)  # cents; the 0.07 * 100 = 7 already


def analyze_event(event_ticker, markets, min_legs):
    legs = [n for n in (norm(m) for m in markets) if n is not None]
    if len(legs) < min_legs:
        return None

    sum_bid = sum(m["yes_bid"] for m in legs)
    sum_ask = sum(m["yes_ask"] for m in legs)

    # Fee to lift every ask (buy-all-YES side) or hit every bid (sell-all side).
    buy_fees = sum(fee_estimate(m["yes_ask"]) for m in legs)
    sell_fees = sum(fee_estimate(m["yes_bid"]) for m in legs)

    # Capturable locks (after fees):
    buy_all_profit = (100 - sum_ask) - buy_fees      # profit if you buy all YES
    sell_all_profit = (sum_bid - 100) - sell_fees    # profit if you sell all YES

    min_vol = min(m["volume"] for m in legs)
    min_oi = min(m["open_interest"] for m in legs)
    # Depth of the lock = smallest quote size on the side you'd hit.
    min_ask_size = min(m["ask_size"] for m in legs)   # buy-all-YES depth
    min_bid_size = min(m["bid_size"] for m in legs)   # sell-all-YES depth

    # Stale-print signal: last outside the current [bid, ask].
    stale = []
    for m in legs:
        last = m.get("last_price")
        if last is None:
            continue
        if last < m["yes_bid"] - 1 or last > m["yes_ask"] + 1:
            stale.append((m.get("ticker"), last, m["yes_bid"], m["yes_ask"]))

    return {
        "event": event_ticker,
        "legs": len(legs),
        "sum_bid": sum_bid,
        "sum_ask": sum_ask,
        "overround": sum_ask - 100,
        "buy_all_profit": buy_all_profit,
        "sell_all_profit": sell_all_profit,
        "arb": buy_all_profit > 0 or sell_all_profit > 0,
        "min_vol": min_vol,
        "min_oi": min_oi,
        "min_ask_size": min_ask_size,
        "min_bid_size": min_bid_size,
        "stale": stale,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", help="series tickers to scan")
    ap.add_argument("--all", action="store_true", help="scan all open markets")
    ap.add_argument("--min-legs", type=int, default=2)
    ap.add_argument("--top", type=int, default=20, help="rows to show")
    args = ap.parse_args()

    by_event = defaultdict(list)
    if args.all:
        print("Fetching ALL open markets (may take a while)...", file=sys.stderr)
        for m in fetch_all_open_markets():
            by_event[m.get("event_ticker")].append(m)
    else:
        series = args.series or DEFAULT_SERIES
        for s in series:
            print(f"Fetching {s}...", file=sys.stderr)
            for m in fetch_markets_by_series(s):
                by_event[m.get("event_ticker")].append(m)

    rows = []
    for ev, mkts in by_event.items():
        r = analyze_event(ev, mkts, args.min_legs)
        if r:
            rows.append(r)

    if not rows:
        print("No multi-leg events found. Check series tickers or use --all.")
        return

    # --- Section 1: genuine capturable locks (after fees) ---
    arbs = [r for r in rows if r["arb"]]
    print("\n" + "=" * 72)
    print("CAPTURABLE LOCKS (sum(bid) > 100 or sum(ask) < 100, after est. fees)")
    print("=" * 72)
    if not arbs:
        print("None. (Expected -- these are rare and vanish fast.)")
    else:
        for r in sorted(arbs, key=lambda x: -max(x["buy_all_profit"],
                                                  x["sell_all_profit"])):
            if r["buy_all_profit"] > 0:
                side = "BUY-ALL +%.1fc/set" % r["buy_all_profit"]
                depth = r["min_ask_size"]
            else:
                side = "SELL-ALL +%.1fc/set" % r["sell_all_profit"]
                depth = r["min_bid_size"]
            print(f"  {r['event']:<28} legs={r['legs']} {side}"
                  f"  maxSets~{depth:.0f} (min quote size)")
        print("  ^ verify MECE + quote depth before trusting any of these.")

    # --- Section 2: overround leaderboard (informational, NOT edge) ---
    print("\n" + "=" * 72)
    print("OVERROUND LEADERBOARD  (sum(ask)-100 = the vig; high = expensive book)")
    print("=" * 72)
    print(f"  {'event':<28} {'legs':>4} {'sumBid':>7} {'sumAsk':>7} "
          f"{'over':>6} {'minVol':>7}")
    for r in sorted(rows, key=lambda x: -x["overround"])[:args.top]:
        print(f"  {r['event']:<28} {r['legs']:>4} {r['sum_bid']:>6}c "
              f"{r['sum_ask']:>6}c {r['overround']:>5}c {r['min_vol']:>7.0f}")

    # --- Section 3: stale prints ---
    stalerows = [r for r in rows if r["stale"]]
    if stalerows:
        print("\n" + "=" * 72)
        print("STALE PRINTS  (last trade outside current bid/ask -- thin, not edge)")
        print("=" * 72)
        for r in stalerows[:args.top]:
            for t, last, b, a in r["stale"]:
                print(f"  {t:<32} last={last}c  quote {b}/{a}")

    print(f"\nScanned {len(rows)} multi-leg events. "
          f"{len(arbs)} capturable, {len(stalerows)} with stale prints.")


if __name__ == "__main__":
    main()
