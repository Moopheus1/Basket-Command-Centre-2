"""Rebound-setup R:R/feasibility calculations and Discord alerting.

This is a Python port of the same math implemented in index.html's
computeRebound()/computeReboundMA50() JavaScript functions, run here so a
GitHub Actions job (no browser) can detect qualifying setups and fire a
Discord alert. Deliberately duplicated rather than shared with the
frontend - the tradeoff accepted: if the JS formula changes, this file
must be manually updated to match, or the alert and the dashboard will
silently disagree about what counts as a setup.

Alerts fire only on a NEW qualifying setup - a ticker crossing INTO the
qualifying set since the last run - not on every run a ticker remains
qualifying. A ticker that drops out and later re-qualifies alerts again;
that's treated as a fresh setup event, not a duplicate. State (which
tickers currently qualify) is persisted to ALERT_STATE_PATH and committed
alongside data.json, so it survives across the ~37 runs/day.
"""
import json
import math
import os
import urllib.error
import urllib.request

ALERT_STATE_PATH = "docs/alert_state.json"
BENCHMARKS = {"SPY", "QQQ", "IWM"}

REBOUND_HORIZON_DAYS = 15
MIN_RISK_ATR_MULT = 0.5
MIN_RR = 1.5
MIN_RR_MA50 = 1.5


def sma(closes, n):
    if len(closes) < n:
        return None
    window = closes[-n:]
    return sum(window) / n


def atr14(bars):
    """bars: list of dicts with high/low/close, oldest to newest."""
    if len(bars) < 15:
        return None
    total = 0.0
    for i in range(len(bars) - 14, len(bars)):
        tr = max(
            bars[i]["high"] - bars[i]["low"],
            abs(bars[i]["high"] - bars[i - 1]["close"]),
            abs(bars[i]["low"] - bars[i - 1]["close"]),
        )
        total += tr
    return total / 14


def swing_low(bars, n=12):
    window = bars[-n:]
    if not window:
        return None
    return min(b["low"] for b in window)


def bars_to_dicts(raw_bars):
    """raw_bars: [[ts, open, high, low, close, volume], ...] from data.json."""
    return [
        {"ts": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]}
        for b in raw_bars
    ]


def compute_derived(raw_bars):
    """Port of the fields computeMetrics() derives that the rebound math
    needs: price, atr, swingLow, ma20, ma50, ma200."""
    bars = bars_to_dicts(raw_bars)
    if len(bars) < 2:
        return None
    closes = [b["close"] for b in bars]
    return {
        "price": bars[-1]["close"],
        "atr": atr14(bars),
        "swingLow": swing_low(bars, 12),
        "ma20": sma(closes, 20),
        "ma50": sma(closes, 50),
        "ma200": sma(closes, 200),
    }


def _rr_and_feasibility(price, atr, sl, target):
    """Shared R:R/feasibility calc - port of the shared block inside both
    JS computeRebound() and computeReboundMA50()."""
    reward = target - price
    risk = price - sl
    min_risk = MIN_RISK_ATR_MULT * atr
    tight_stop = risk < min_risk
    if tight_stop:
        risk = min_risk
    rr = reward / risk if risk > 0 else None
    expected_move = atr * math.sqrt(REBOUND_HORIZON_DAYS)
    feasibility = None
    if reward > 0:
        feasibility = max(0, min(100, (expected_move / reward) * 100))
    return {"reward": reward, "risk": risk, "rr": rr, "feasibility": feasibility, "tightStop": tight_stop}


def check_ma200_rebound(d):
    """d: dict from compute_derived(). Returns the rebound dict, or None if
    not qualifying (already above MA200) or missing required data."""
    price, atr, sl, ma200 = d["price"], d["atr"], d["swingLow"], d["ma200"]
    if price is None or atr is None or sl is None:
        return None
    if ma200 is None or ma200 <= price:
        return None
    result = _rr_and_feasibility(price, atr, sl, ma200)
    result["target"] = ma200
    return result


def check_ma50_uptrend(d):
    price, atr, sl = d["price"], d["atr"], d["swingLow"]
    ma20, ma50, ma200 = d["ma20"], d["ma50"], d["ma200"]
    if None in (price, atr, sl, ma20, ma50, ma200):
        return None
    confirmed_uptrend = price > ma200 and ma20 > ma50
    needs_to_rise = ma50 > price
    if not (confirmed_uptrend and needs_to_rise):
        return None
    result = _rr_and_feasibility(price, atr, sl, ma50)
    result["target"] = ma50
    return result


def find_qualifying_setups(tickers_data):
    """tickers_data: the out["tickers"] dict from fetch_data.py.
    Returns (ma200_qualifying: dict[sym -> rebound], ma50_qualifying: dict[sym -> rebound])
    for tickers currently at/above the R:R threshold on each table."""
    ma200_qual, ma50_qual = {}, {}
    for sym, entry in tickers_data.items():
        if sym in BENCHMARKS:
            continue
        if not entry or entry.get("error") or not entry.get("bars"):
            continue
        d = compute_derived(entry["bars"])
        if d is None:
            continue
        r200 = check_ma200_rebound(d)
        if r200 and r200["rr"] is not None and r200["rr"] >= MIN_RR:
            ma200_qual[sym] = r200
        r50 = check_ma50_uptrend(d)
        if r50 and r50["rr"] is not None and r50["rr"] >= MIN_RR_MA50:
            ma50_qual[sym] = r50
    return ma200_qual, ma50_qual


def load_alert_state():
    if not os.path.exists(ALERT_STATE_PATH):
        return {"ma200": [], "ma50": []}
    try:
        with open(ALERT_STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"ma200": [], "ma50": []}


def save_alert_state(ma200_qual, ma50_qual):
    os.makedirs("docs", exist_ok=True)
    with open(ALERT_STATE_PATH, "w") as f:
        json.dump({"ma200": sorted(ma200_qual.keys()), "ma50": sorted(ma50_qual.keys())}, f)


def send_discord_alert(new_ma200, new_ma50):
    """new_ma200 / new_ma50: dict[sym -> rebound dict] of NEWLY qualifying tickers."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("[alerts] DISCORD_WEBHOOK_URL not set - skipping alert (would have fired)")
        return
    if not new_ma200 and not new_ma50:
        return

    lines = ["**New rebound setup(s) detected**"]
    for sym, r in sorted(new_ma200.items()):
        lines.append(
            f"- **{sym}** -> MA200 reclaim | R:R {r['rr']:.2f} | "
            f"Feasibility {r['feasibility']:.0f} | Target ${r['target']:.2f}"
        )
    for sym, r in sorted(new_ma50.items()):
        lines.append(
            f"- **{sym}** -> Uptrend to MA50 | R:R {r['rr']:.2f} | "
            f"Feasibility {r['feasibility']:.0f} | Target ${r['target']:.2f}"
        )
    content = "\n".join(lines)

    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[alerts] Discord webhook responded {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"[alerts] Discord webhook failed: HTTP {e.code} {e.read()[:200]}")
    except Exception as e:
        print(f"[alerts] Discord webhook failed: {e}")


def check_and_alert(tickers_data):
    """Main entry point, called from fetch_data.py after building out['tickers']."""
    ma200_qual, ma50_qual = find_qualifying_setups(tickers_data)
    prev_state = load_alert_state()
    prev_ma200 = set(prev_state.get("ma200", []))
    prev_ma50 = set(prev_state.get("ma50", []))

    new_ma200 = {s: r for s, r in ma200_qual.items() if s not in prev_ma200}
    new_ma50 = {s: r for s, r in ma50_qual.items() if s not in prev_ma50}

    if new_ma200 or new_ma50:
        print(f"[alerts] NEW setups: MA200={list(new_ma200)} MA50={list(new_ma50)}")
        send_discord_alert(new_ma200, new_ma50)
    else:
        print("[alerts] no new setups this run "
              f"(currently qualifying: MA200={list(ma200_qual)} MA50={list(ma50_qual)})")

    save_alert_state(ma200_qual, ma50_qual)
