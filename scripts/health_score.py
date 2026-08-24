"""Fetch a ticker's fundamentals from equitylens.investwithbjorn.com and
reproduce its client-side "Financial Health Score" (0-100 + letter grade).

Equity Lens has no public score API - the /api/financials endpoint returns
raw fundamentals only, and the 0-100 score is computed in the browser from
that data. This module re-implements that scoring formula in Python,
reverse-engineered from their minified JS bundle (validated against their
own page metadata: AAPL -> 72/B, matching their published "Grade B" for
Apple at time of writing).

This is an UNOFFICIAL, UNDOCUMENTED endpoint - not a public API. Treat it
as best-effort: on any failure (network, missing fields, unexpected shape,
rate limiting) this returns (None, None) rather than raising, so a bad
fetch here never blocks the rest of the EOD data run.

Only call this from the once-daily EOD path, not the intraday path -
these are annual-report fundamentals, they don't move intraday, and
hammering someone else's undocumented endpoint every 10 minutes is rude
and likely to get us blocked.
"""
import hashlib
import json
import time
import requests

BASE = "https://equitylens.investwithbjorn.com/api/financials"
TIMEOUT = 10
CANARY_TICKER = "AAPL"


# ---- helpers, mirroring the JS (hy/hm/hb/hg/hx/hw in their bundle) --------

def _hy(series, t):
    return [x["value"] for x in series[-t:] if x.get("value") is not None]


def _hm(arr):
    """True if arr is strictly decreasing step-over-step, needs len>=3."""
    if len(arr) < 3:
        return False
    return all(arr[i] < arr[i - 1] for i in range(1, len(arr)))


def _hb(series):
    return series[-1]["value"] if series else None


def _hg(x):
    return max(0, min(100, round(x)))


def _hx(e, t, r):
    return _hg((e - t) / (r - t) * 100)


def _hw(series, t=4):
    if len(series) < 2:
        return None
    r = series[-1]["value"]
    n = series[max(0, len(series) - t)]["value"]
    if n == 0:
        return None
    return (r - n) / abs(n) * 100


def _count_warnings(fin):
    m = fin["metrics"]
    o = fin.get("sector") == "Financial Services"
    warns = 0

    i = _hy(m["efficiencyRatio"] if o else m["grossMargin"], 4)
    if _hm(i) and not o:
        warns += 1
    if o:
        e = _hy(m["efficiencyRatio"], 4)
        if len(e) >= 3 and all(e[r] > e[r - 1] for r in range(1, len(e))):
            warns += 1
    if not o:
        e = _hy(m["opMargin"], 4)
        if _hm(e):
            warns += 1
    if not o:
        e = _hy(m["fcf"], 3)
        n = _hy(m["netIncome"], 3)
        if len(e) >= 2 and len(n) >= 2:
            if n[-1] > 0 and e[-1] < 0.5 * n[-1]:
                warns += 1
            if (n[-1] - n[0]) > 0 and (e[-1] - e[0]) < 0:
                warns += 1
    a = _hy(m["debtEquity"], 4)
    if len(a) >= 3 and a[0] > 0 and _hm([-x for x in a]):
        if a[-1] - a[0] > 0.5:
            warns += 1
    s = _hy(m["eps"], 4)
    if _hm(s) and s and s[-1] > 0:
        warns += 1
    return warns


def _compute_score(fin):
    """Returns (score:int, grade:str) or (None, None) if not scoreable
    (ETF, or fewer than 3 usable component metrics - mirrors their
    `if (l.length < 3) return null`)."""
    if fin.get("isETF"):
        return None, None
    m = fin.get("metrics") or {}
    required = ("revenue", "roe", "opMargin", "debtEquity", "fcf",
                "netIncome", "efficiencyRatio", "eps", "grossMargin")
    if not all(k in m for k in required):
        return None, None

    a = fin.get("sector") == "Financial Services"
    l = []

    revenue = m["revenue"]
    s = None
    if len(revenue) >= 2:
        s = (revenue[-1]["value"] - revenue[-2]["value"]) / abs(revenue[-2]["value"]) * 100
    if s is not None:
        l.append({"weight": 1, "score": _hx(s, -8 if a else -10, 12 if a else 25)})

    c = _hb(m["roe"])
    if c is not None:
        l.append({"weight": 1, "score": _hx(c, 4 if a else 0, 16 if a else 25)})

    if a:
        e = _hb(m["efficiencyRatio"])
        if e is not None:
            l.append({"weight": 1, "score": _hx(e, 70, 40)})
        t_, r_ = _hb(m["revenue"]), _hb(m["netIncome"])
        if t_ is not None and r_ is not None and t_ != 0:
            l.append({"weight": 1, "score": _hx(r_ / t_ * 100, 10, 45)})
    else:
        e = _hb(m["opMargin"])
        if e is not None:
            l.append({"weight": 1, "score": _hx(e, 0, 30)})
        t_ = _hb(m["debtEquity"])
        if t_ is not None:
            l.append({"weight": 1, "score": 35 if t_ < 0 else _hx(t_, 2.5, 0)})
        r_, n_ = _hb(m["fcf"]), _hb(m["netIncome"])
        if r_ is not None:
            if r_ <= 0:
                score = 20
            elif n_ is not None and n_ > 0:
                score = _hx(r_ / n_, 0, 1)
            else:
                score = 60
            l.append({"weight": 1, "score": score})

    if len(l) < 3:
        return None, None

    u = _count_warnings(fin)
    f = []
    if len(revenue) >= 3:
        d = ((revenue[-1]["value"] - revenue[-2]["value"]) / abs(revenue[-2]["value"]) -
             (revenue[-2]["value"] - revenue[-3]["value"]) / abs(revenue[-3]["value"])) * 100
        if abs(d) <= 40:
            f.append({"v": _hx(d, -25 if a else -15, 8 if a else 5), "w": 2})
    if a:
        e = _hw(m["efficiencyRatio"], 2)
        if e is not None:
            f.append({"v": _hx(e, 12, -8), "w": 1})
    else:
        opm = m["opMargin"] if len(m["opMargin"]) else m["grossMargin"]
        e = _hw(opm, 2)
        if e is not None:
            f.append({"v": _hx(e, -12, 8), "w": 1})
        t2 = _hw(m["fcf"], 2)
        if t2 is not None:
            f.append({"v": _hx(t2, -25, 30), "w": 1})
    p = _hw(m["eps"], 2)
    if p is not None:
        f.append({"v": _hx(p, -20 if a else -15, 20 if a else 30), "w": 1})

    if f:
        wsum = sum(x["w"] for x in f)
        tscore = _hg(sum(x["v"] * x["w"] for x in f) / wsum - min(30, 10 * u))
        l.append({"weight": 2 if a else 3, "score": tscore})

    h = sum(x["weight"] for x in l)
    y = _hg(sum(x["score"] * x["weight"] for x in l) / h)
    grade = "A" if y >= 75 else "B" if y >= 60 else "C" if y >= 45 else "D"
    return y, grade


def _fetch_financials(symbol):
    """Raw fetch + shape check, separated from scoring so pipeline
    monitoring can tell apart 'site unreachable' from 'site reachable but
    response shape changed' from 'reachable, valid shape, just not
    scoreable' (e.g. an ETF)."""
    try:
        resp = requests.get(BASE, params={"ticker": symbol}, timeout=TIMEOUT)
    except Exception:
        return None, "http_error"
    if resp.status_code != 200:
        return None, "http_error"
    try:
        fin = resp.json()
    except Exception:
        return None, "bad_shape"
    if not isinstance(fin, dict) or "metrics" not in fin or not isinstance(fin.get("metrics"), dict):
        return None, "bad_shape"
    return fin, "ok"


def get_score_with_status(symbol):
    """Like get_score, but also classifies WHY there's no score, so a
    pipeline monitor can distinguish real failure modes:
      ok / etf / insufficient_data / http_error / bad_shape
    'insufficient_data' means the fetch worked fine but fewer than 3 of
    the scoring components had usable data for this specific company -
    that's a normal data gap, not a sign anything broke."""
    fin, fetch_status = _fetch_financials(symbol)
    if fetch_status != "ok":
        return None, None, fetch_status
    if fin.get("isETF"):
        return None, None, "etf"
    score, grade = _compute_score(fin)
    if score is None:
        return None, None, "insufficient_data"
    return score, grade, "ok"


def get_canary_fingerprint(symbol=CANARY_TICKER):
    """Hash of the response shape (top-level keys + metrics keys) for a
    fixed reference ticker. Unchanged fingerprint == Equity Lens hasn't
    changed its response shape since the last EOD run. Returns None if
    the canary itself can't be fetched (site down / blocked)."""
    fin, status = _fetch_financials(symbol)
    if status != "ok":
        return None
    top_keys = sorted(fin.keys())
    metric_keys = sorted((fin.get("metrics") or {}).keys())
    fingerprint_src = json.dumps([top_keys, metric_keys], sort_keys=True)
    return hashlib.sha256(fingerprint_src.encode()).hexdigest()[:12]


def get_score(symbol):
    """Best-effort: (score, grade) or (None, None). Never raises."""
    score, grade, _status = get_score_with_status(symbol)
    return score, grade


def get_scores_with_status(symbols, pause=1.0):
    """Sequential fetch for a list of symbols, staying polite to an
    unofficial endpoint. Returns {symbol: (score, grade, status)}."""
    out = {}
    for sym in symbols:
        out[sym] = get_score_with_status(sym)
        time.sleep(pause)
    return out
