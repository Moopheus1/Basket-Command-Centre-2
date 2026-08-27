"""Pre-market signal check: gap % vs prior close, and an earnings-day
flag. Computed FOUR times before/at the open - ~9:00, ~9:10, ~9:20, and
~9:30 ET (all triggered by wall-clock time in fetch_data.py main(), not
by matching a cron string - see is_premarket_check_window() for why).
The 9:30 check lands right at market open, so it captures the actual
opening print rather than a pre-open estimate. Each run overwrites the
last, so the dashboard always shows the most recent check.

IMPORTANT - what this is and isn't:
These are DESCRIPTIVE early-read signals, not predictions. A stock
already gapping up pre-market, or one that just reported earnings, has
statistically elevated odds of a large intraday move - neither
guarantees one. No price/volume pattern reliably predicts an
unannounced 5%+ move before it happens; if one did, it would already be
priced in by the time you could act on it. Treat both fields as "worth
a closer look," not "will definitely move."

Both values are a snapshot taken at the last of those four checks and
then carried forward unchanged for the rest of the day by
apply_prior_premarket_watch() in fetch_data.py - they do NOT
live-update after that. A gap % shown at 2pm reflects the 9:30am
opening print, not the current live move.

Known limitation: pre-market VOLUME from this same yfinance feed is
unreliable (reports exactly 0 for every pre-market bar, confirmed
directly against several tickers) - this module uses pre-market PRICE
only, which does work.
"""
import time
from datetime import timedelta
import yfinance as yf

REQUEST_PAUSE_SECONDS = 0.3
EARNINGS_LOOKBACK_DAYS = 3  # calendar days - covers a Friday-AMC report seen Monday morning


def get_premarket_gap(symbol, prior_close):
    """Gap % vs prior_close using the latest available pre-market price,
    or None on any failure or missing prior_close."""
    if not prior_close:
        return None
    try:
        tk = yf.Ticker(symbol)
        df = tk.history(period="1d", interval="1m", prepost=True)
        if df.empty:
            return None
        latest_price = float(df["Close"].dropna().iloc[-1])
        return (latest_price - prior_close) / prior_close * 100
    except Exception:
        return None


def get_earnings_flag(symbol, now_et):
    """'recent' if the company reported within the last few calendar
    days (catches yesterday's after-close report, or a Friday report
    seen the following Monday), 'today' if the next scheduled report is
    later today, else None. Best-effort - returns None on any failure,
    including symbols yfinance has no earnings-calendar data for."""
    try:
        tk = yf.Ticker(symbol)
        df = tk.get_earnings_dates(limit=6)
        if df is None or df.empty:
            return None
        past = df[df.index <= now_et]
        future = df[df.index > now_et]
        if not past.empty:
            last_date = past.index[0]  # most recent past entry (sorted desc)
            if (now_et.date() - last_date.date()).days <= EARNINGS_LOOKBACK_DAYS:
                return "recent"
        if not future.empty:
            next_date = future.index[-1]  # nearest future entry (sorted desc -> last)
            if next_date.date() == now_et.date():
                return "today"
        return None
    except Exception:
        return None


def run(tickers_dict, now_et):
    """Mutates each entry in tickers_dict with premarketGapPct and
    earningsFlag. Never raises - a single bad ticker just gets None
    values and the loop continues; this must not be able to take down
    the whole run the way an earlier version of the health-score loop
    almost did."""
    for sym, entry in tickers_dict.items():
        if entry.get("error"):
            entry["premarketGapPct"] = None
            entry["earningsFlag"] = None
            continue

        bars = entry.get("bars")
        prior_close = bars[-2][4] if bars and len(bars) >= 2 else None

        try:
            entry["premarketGapPct"] = get_premarket_gap(sym, prior_close)
        except Exception as e:
            print(f"[premarket_watch] {sym}: gap check failed: {e}")
            entry["premarketGapPct"] = None
        time.sleep(REQUEST_PAUSE_SECONDS)

        try:
            entry["earningsFlag"] = get_earnings_flag(sym, now_et)
        except Exception as e:
            print(f"[premarket_watch] {sym}: earnings check failed: {e}")
            entry["earningsFlag"] = None
        time.sleep(REQUEST_PAUSE_SECONDS)
