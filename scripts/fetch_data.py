"""Fetch bars + fundamentals for all tickers in tickers.txt and write docs/data.json.

Runs in GitHub Actions (see .github/workflows/update-data.yml), 34x/weekday:
  - FETCH_MODE=eod:       full 430-day history + fundamentals, for every ticker.
                           Used for the once-daily 16:30 ET run, and as the
                           fallback for manual/push-triggered runs and for the
                           very first run ever (no docs/data.json to build on).
  - FETCH_MODE=intraday:  lightweight — pulls only today's 1-minute bars,
                           synthesizes a single "today" OHLCV bar from them,
                           and merges it into the EXISTING docs/data.json's
                           bar history (replacing today's bar if already
                           present, appending it if not). Skips tk.info()
                           entirely, since sector/industry/mcap/beta don't
                           change intraday - those fields are carried over
                           unchanged from the last EOD fetch.

Design intent: the heavy call (.history(period="430d") + .info) only happens
once a day. The other 33 daily runs each do one lightweight 1-minute pull per
ticker with no fundamentals call, to avoid hammering Yahoo with the same
430-day + company-profile payload every 10 minutes.

On a per-ticker failure in intraday mode, the existing entry is left
untouched (stale but valid) rather than overwritten with an error stub -
a transient miss during the day shouldn't blank out a ticker that has
perfectly good EOD data. EOD mode keeps the original stricter behavior:
a failure there does write an error stub, since that's the once-daily
authoritative refresh and a silent stale entry would hide a real problem.

Uses yfinance (Yahoo Finance).
"""
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

import alerts

DATA_PATH = "docs/data.json"


def read_tickers(path="tickers.txt"):
    tickers = []
    with open(path) as f:
        for line in f:
            line = line.strip().upper()
            if line and not line.startswith("#") and line not in tickers:
                tickers.append(line)
    return tickers


def load_existing():
    if not os.path.exists(DATA_PATH):
        return None
    try:
        with open(DATA_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def fetch_full(symbol):
    """Heavy path: full history + fundamentals. Used for EOD and fallback runs."""
    tk = yf.Ticker(symbol)
    hist = tk.history(period="430d", interval="1d", auto_adjust=False)
    if hist.empty:
        raise ValueError("no price data returned")
    bars = []
    for ts, row in hist.iterrows():
        try:
            bars.append([
                int(ts.timestamp()),
                round(float(row["Open"]), 4),
                round(float(row["High"]), 4),
                round(float(row["Low"]), 4),
                round(float(row["Close"]), 4),
                int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,  # NaN check
            ])
        except (ValueError, TypeError):
            continue
    if len(bars) < 2:
        raise ValueError("not enough valid bars")
    info = {}
    try:
        info = tk.info or {}
    except Exception:
        pass  # fundamentals are best-effort; bars are the essential part
    return {
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "mcap": info.get("marketCap"),
        "beta": info.get("beta"),
        "targetMean": info.get("targetMeanPrice"),
        "targetHigh": info.get("targetHighPrice"),
        "targetLow": info.get("targetLowPrice"),
        "numAnalysts": info.get("numberOfAnalystOpinions"),
        "bars": bars,
    }


def fetch_today_bar(symbol):
    """Light path: today's 1-minute bars, synthesized into one OHLCV bar.
    No .info call. Returns None if there's no intraday data yet (e.g. the
    very first premarket run before any trades have printed)."""
    tk = yf.Ticker(symbol)
    h = tk.history(period="1d", interval="1m", auto_adjust=False)
    if h.empty:
        return None
    d = h.index[0].date()
    midnight_et = pd.Timestamp(d, tz="America/New_York")
    ts = int(midnight_et.timestamp())
    vol_sum = h["Volume"].sum()
    return [
        ts,
        round(float(h["Open"].iloc[0]), 4),
        round(float(h["High"].max()), 4),
        round(float(h["Low"].min()), 4),
        round(float(h["Close"].iloc[-1]), 4),
        int(vol_sum) if vol_sum == vol_sum else 0,  # NaN check
    ]


def merge_today_bar(existing_bars, today_bar):
    """Replace today's bar if the last existing bar is already today,
    otherwise append it. existing_bars may be empty (new ticker)."""
    bars = list(existing_bars)
    if bars and bars[-1][0] == today_bar[0]:
        bars[-1] = today_bar
    else:
        bars.append(today_bar)
    return bars


def run_eod(tickers):
    out = {"asof": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tickers": {}}
    failed = []
    for sym in tickers:
        try:
            out["tickers"][sym] = fetch_full(sym)
            print(f"OK   {sym}: {len(out['tickers'][sym]['bars'])} bars")
        except Exception as e:
            failed.append(sym)
            out["tickers"][sym] = {"error": str(e)[:120], "bars": []}
            print(f"FAIL {sym}: {e}")
        time.sleep(1)  # stay polite to Yahoo
    print(f"[eod] {len(tickers) - len(failed)}/{len(tickers)} tickers OK")
    if len(failed) == len(tickers):
        raise SystemExit("every ticker failed — aborting so the old data.json is kept")
    return out


def run_intraday(tickers, existing):
    out = {
        "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tickers": dict(existing.get("tickers", {})),  # start from last good state
    }
    ok, failed, skipped = 0, 0, 0
    for sym in tickers:
        prior = out["tickers"].get(sym, {})
        try:
            today_bar = fetch_today_bar(sym)
            if today_bar is None:
                skipped += 1
                print(f"SKIP {sym}: no intraday data yet")
                continue
            merged_bars = merge_today_bar(prior.get("bars", []), today_bar)
            out["tickers"][sym] = {
                "name": prior.get("name"),
                "sector": prior.get("sector"),
                "industry": prior.get("industry"),
                "mcap": prior.get("mcap"),
                "beta": prior.get("beta"),
                "targetMean": prior.get("targetMean"),
                "targetHigh": prior.get("targetHigh"),
                "targetLow": prior.get("targetLow"),
                "numAnalysts": prior.get("numAnalysts"),
                "bars": merged_bars,
            }
            ok += 1
            print(f"OK   {sym}: today bar merged ({len(merged_bars)} total bars)")
        except Exception as e:
            failed += 1
            print(f"FAIL {sym}: {e} — keeping prior data untouched")
            # prior entry (if any) is already in out["tickers"] unchanged
        time.sleep(1)  # stay polite to Yahoo
    print(f"[intraday] {ok} updated, {failed} failed (kept stale), {skipped} skipped (no data)")
    if ok == 0 and failed == len(tickers):
        raise SystemExit("every ticker failed — aborting so the old data.json is kept")
    return out


def main():
    tickers = read_tickers()
    mode = os.environ.get("FETCH_MODE", "eod").strip().lower()
    existing = load_existing()

    if mode == "intraday" and existing is not None:
        out = run_intraday(tickers, existing)
    else:
        if mode == "intraday":
            print("[intraday] no existing docs/data.json found — falling back to full eod fetch")
        out = run_eod(tickers)

    try:
        alerts.check_and_alert(out["tickers"])
    except Exception as e:
        print(f"[alerts] check failed, not blocking data write: {e}")

    os.makedirs("docs", exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {DATA_PATH} (mode={mode})")


if __name__ == "__main__":
    main()
