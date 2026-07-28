#!/usr/bin/env python3
"""
LAYER 2b -- ensemble remainder model vs market, across all monthly rain cities.

For each unsettled rung: project the FINAL monthly total per ensemble member as
(month-to-date banked, from the NWS CLI report) + (that member's precip over the
remaining days of the month). Then

    model P(final > threshold) = fraction of members exceeding it.

Compare to the price you'd ACTUALLY trade at (the ask to buy YES, the bid to
sell YES) -- not the mid -- so the spread is baked into every edge. An edge only
counts if the model probability clears the tradeable price by a fee buffer.

THE QUESTION THIS ANSWERS: run across all cities. If edges are systematic and
one-signed, that's a candidate strategy OR a systematic ensemble bias (the tool
cannot tell which -- your journal must). If edges are small/random, there is no
strategy and the market is efficient at this lead. Disproving is confident;
confirming is only a flag.

Honest confounds baked into any flag:
  * Precip ensembles under-forecast convective extremes -> tail "overpriced"
    signals are confounded with model dry-bias.
  * Ensemble grid precip != the point gauge that settles.
  * 71 members (GEFS+ICON) is a coarse tail estimate; +-a few pp is noise.

Usage:
  python3 scan_forecast.py                    # all 11 cities
  python3 scan_forecast.py --series KXRAINMIAM KXRAINHOUM
  python3 scan_forecast.py --fee 3 --edge 5   # fee buffer / min edge, cents
"""

import argparse
import urllib.request
import json
import sys

from scan_monthly import load_ladder, fetch_mtd

ENS = "https://ensemble-api.open-meteo.com/v1/ensemble"

# station (CLI code) -> (lat, lon, tz). The gauge Kalshi settles on.
COORDS = {
    "KXRAINMIAM": (25.7881, -80.3169, "America/New_York"),      # MIA
    "KXRAINSTPM": (27.7650, -82.6270, "America/New_York"),      # SPG St Pete
    "KXRAINNYCM": (40.7789, -73.9692, "America/New_York"),      # NYC Central Park
    "KXRAINCHIM": (41.7860, -87.7520, "America/Chicago"),       # MDW
    "KXRAINDALM": (32.8968, -97.0380, "America/Chicago"),       # DFW
    "KXRAINHOUM": (29.6459, -95.2769, "America/Chicago"),       # HOU Hobby
    "KXRAINAUSM": (30.1975, -97.6664, "America/Chicago"),       # AUS
    "KXRAINDENM": (39.8466, -104.6562, "America/Denver"),       # DEN
    "KXRAINLAXM": (33.9382, -118.3865, "America/Los_Angeles"),  # LAX
    "KXRAINSFOM": (37.6188, -122.3750, "America/Los_Angeles"),  # SFO
    "KXRAINSEAM": (47.4436, -122.3016, "America/Los_Angeles"),  # SEA
}
ALL = list(COORDS)


def fetch_json(url, tmo=90):
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-fc/1.0"})
    with urllib.request.urlopen(req, timeout=tmo) as r:
        return json.load(r)


def member_final_totals(series, mtd):
    """List of projected final monthly totals (inches), one per ensemble member."""
    lat, lon, tz = COORDS[series]
    url = (f"{ENS}?latitude={lat}&longitude={lon}&daily=precipitation_sum"
           f"&models=gfs025,icon_seamless&forecast_days=16"
           f"&timezone={tz.replace('/', '%2F')}")
    d = fetch_json(url)
    daily = d["daily"]
    times = daily["time"]
    # remaining days of the CURRENT month = same month as today, day >= today.
    # We infer "current month" from the first forecast date (index 0 = today).
    cur_month = times[0][5:7]
    idx = [i for i, t in enumerate(times) if t[5:7] == cur_month]
    keys = [k for k in daily if k.startswith("precipitation_sum")]
    totals = []
    for k in keys:
        v = daily[k]
        rem = sum((v[i] or 0.0) for i in idx) / 25.4  # mm -> inch, remaining
        totals.append(mtd + rem)
    return totals, len(idx)


def analyze(series, fee, min_edge):
    if series not in COORDS:
        print(f"{series}: no coordinates configured", file=sys.stderr)
        return []
    etk, rungs, cli = load_ladder(series, 0)
    if not rungs:
        return []
    mtd, note = (fetch_mtd(cli) if cli else (None, "no station"))
    if mtd is None:
        print(f"{series}: MTD unavailable ({note})", file=sys.stderr)
        return []
    totals, ndays = member_final_totals(series, mtd)
    n = len(totals)

    print(f"\n{series}  {etk}  CLI{cli}  MTD {mtd:.2f}\"  "
          f"{ndays} days left  {n} members")
    print(f"  {'rung':>6}{'mid':>5}{'bid':>5}{'ask':>5}{'model':>7}"
          f"{'buyYES':>8}{'sellYES':>8}{'vol':>8}")
    flags = []
    for r in rungs:
        if mtd > r["x"]:
            continue  # settled by the floor; not a forecast question
        p = sum(1 for v in totals if v > r["x"]) / n
        pc = p * 100
        buy_edge = pc - r["ask"] - fee     # buy YES: model prob beats the ask
        sell_edge = r["bid"] - pc - fee    # sell YES: bid beats model prob
        tag = ""
        if buy_edge >= min_edge:
            tag = f"BUY YES +{buy_edge:.0f}c"
            flags.append((series, r["x"], "BUY", buy_edge, r, pc))
        elif sell_edge >= min_edge:
            tag = f"SELL YES +{sell_edge:.0f}c"
            flags.append((series, r["x"], "SELL", sell_edge, r, pc))
        print(f"  >{r['x']:<5}{r['mid']:>5}{r['bid']:>5}{r['ask']:>5}"
              f"{pc:>6.0f}%{buy_edge:>7.0f}c{sell_edge:>7.0f}c{r['vol']:>8.0f}"
              f"  {tag}")
    return flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", default=ALL)
    ap.add_argument("--fee", type=float, default=3, help="fee buffer, cents")
    ap.add_argument("--edge", type=float, default=5, help="min edge to flag, cents")
    a = ap.parse_args()

    all_flags = []
    for s in a.series:
        try:
            all_flags.extend(analyze(s, a.fee, a.edge))
        except Exception as e:
            print(f"{s}: ERROR {type(e).__name__}: {e}", file=sys.stderr)

    print("\n" + "=" * 70)
    print("CROSS-CITY VERDICT")
    print("=" * 70)
    if not all_flags:
        print("No rung's model prob clears the tradeable price by the fee buffer.")
        print("-> At this lead, the market is efficient within costs. No edge.")
        return
    buys = [f for f in all_flags if f[2] == "BUY"]
    sells = [f for f in all_flags if f[2] == "SELL"]
    print(f"{len(all_flags)} flags: {len(buys)} BUY-YES, {len(sells)} SELL-YES.")
    for s, x, side, edge, r, pc in sorted(all_flags, key=lambda z: -z[3]):
        print(f"  {s:<12} >{x}\"  {side} YES  +{edge:.0f}c  "
              f"(model {pc:.0f}% vs {r['bid']}/{r['ask']}, vol {r['vol']:.0f})")
    if sells and not buys:
        print("\nAll flags are SELL-tail. This is EITHER a real favorite-longshot")
        print("inefficiency OR the ensemble's convective dry-bias. The tool CANNOT")
        print("tell which -- only settlement (your journal) can. Do not size up yet.")
    elif buys and not sells:
        print("\nAll flags are BUY. Suspect a systematic wet-bias or stale MTD before")
        print("believing it. Verify against the next CLI report.")
    else:
        print("\nMixed directions -> more consistent with noise than a systematic")
        print("edge. Log them and let the journal adjudicate.")


if __name__ == "__main__":
    main()
