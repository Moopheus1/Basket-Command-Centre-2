"""Runs the Equity Lens health-score fetch across the full ticker list once
per EOD run (called from fetch_data.py, EOD path only), and watches the
pipeline itself for two DIFFERENT failure modes that need different
handling:

  - STALE / DOWN: Equity Lens stops responding, times out, or returns
    something we can't parse for most tickers. Likely transient (their
    site is down, we got rate-limited) - or not. Escalates from
    "degraded" to "down" after a few consecutive bad EOD runs, so a
    single blip doesn't cry wolf.

  - CHANGED: the shape of /api/financials changes (fields added/removed)
    for a fixed canary ticker (AAPL), even while fetches keep succeeding.
    This is the more dangerous case: the reverse-engineered scoring
    formula in health_score.py may now silently disagree with what
    Bjorn's site actually computes, and every score in the dashboard
    could be quietly wrong with no visible error. Always alerts
    immediately - severity isn't given time to "escalate" the way
    downtime is, since by the time it's happened once, it's already
    happened.

State persists in docs/health_score_state.json (committed alongside
data.json, same pattern as alerts.py's alert_state.json). A Discord
alert fires on a state TRANSITION (ok->degraded, degraded->down,
degraded/down->ok, or any fingerprint change) rather than on every bad
run, so a known-degraded pipeline doesn't spam the channel daily.
Reuses the same DISCORD_WEBHOOK_URL secret alerts.py already uses.

A summary is also returned for embedding into out["healthScoreMeta"] in
data.json, so the dashboard itself shows a banner - not everyone
watches the Discord channel.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

import health_score as hs

STATE_PATH = "docs/health_score_state.json"
SUCCESS_RATE_ALERT_THRESHOLD = 0.5
CONSECUTIVE_BAD_FOR_DOWN = 3


def load_state():
    default = {"fingerprint": None, "consecutiveBadRuns": 0, "lastGoodAsof": None, "lastStatus": "ok"}
    if not os.path.exists(STATE_PATH):
        return default
    try:
        with open(STATE_PATH) as f:
            return {**default, **json.load(f)}
    except (json.JSONDecodeError, OSError):
        return default


def save_state(state):
    os.makedirs("docs", exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def send_discord_alert(message):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("[health_score_monitor] DISCORD_WEBHOOK_URL not set - skipping alert (would have fired):")
        print(message)
        return
    payload = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[health_score_monitor] Discord webhook responded {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"[health_score_monitor] Discord webhook failed: HTTP {e.code} {e.read()[:200]}")
    except Exception as e:
        print(f"[health_score_monitor] Discord webhook failed: {e}")


def run(tickers_dict):
    """tickers_dict: out["tickers"] from fetch_data.py (EOD path only),
    already populated with bars/fundamentals from Yahoo. Mutates each
    entry in place with healthScore/healthGrade. Returns a
    healthScoreMeta dict to attach to out[]."""
    statuses = {}
    for sym, entry in tickers_dict.items():
        if entry.get("error"):
            entry["healthScore"] = None
            entry["healthGrade"] = None
            continue
        score, grade, status = hs.get_score_with_status(sym)
        entry["healthScore"] = score
        entry["healthGrade"] = grade
        statuses[sym] = status

    # "attempted" excludes ETFs - they're EXPECTED to have no score, so an
    # ETF-heavy run shouldn't drag the success rate down artificially.
    attempted = {s: st for s, st in statuses.items() if st != "etf"}
    ok_count = sum(1 for st in attempted.values() if st == "ok")
    success_rate = (ok_count / len(attempted)) if attempted else None

    fingerprint = hs.get_canary_fingerprint()

    state = load_state()
    prev_fingerprint = state.get("fingerprint")
    fingerprint_changed = (
        prev_fingerprint is not None and fingerprint is not None and fingerprint != prev_fingerprint
    )
    canary_unreachable = fingerprint is None

    this_run_bad = canary_unreachable or (
        success_rate is not None and success_rate < SUCCESS_RATE_ALERT_THRESHOLD
    )
    consecutive = (state.get("consecutiveBadRuns", 0) + 1) if this_run_bad else 0

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    last_good = now_iso if not this_run_bad else state.get("lastGoodAsof")

    if consecutive >= CONSECUTIVE_BAD_FOR_DOWN:
        status = "down"
    elif this_run_bad:
        status = "degraded"
    else:
        status = "ok"

    prev_status = state.get("lastStatus", "ok")
    rate_str = f"{success_rate:.0%}" if success_rate is not None else "n/a"

    messages = []
    if fingerprint_changed:
        messages.append(
            f"**Equity Lens API shape changed** (canary fingerprint {prev_fingerprint} -> {fingerprint}). "
            "The reverse-engineered scoring formula may no longer match what their site actually computes - "
            "health scores could now be silently wrong even though fetches keep succeeding. Worth spot-checking "
            "a few tickers against the live site."
        )
    if status != prev_status and status in ("degraded", "down"):
        messages.append(
            f"**Health Score pipeline is now {status.upper()}** - success rate {rate_str} "
            f"({ok_count}/{len(attempted)} tickers), {consecutive} consecutive bad EOD run(s). "
            f"Last known-good run: {last_good or 'never'}."
        )
    if status == "ok" and prev_status in ("degraded", "down"):
        messages.append("**Health Score pipeline recovered** - back to normal on the latest EOD run.")

    if messages:
        text = "\n".join(messages)
        send_discord_alert(text)
        print("[health_score_monitor] " + " | ".join(messages))
    else:
        print(f"[health_score_monitor] status={status} success_rate={rate_str} consecutiveBad={consecutive}")

    save_state({
        "fingerprint": fingerprint or prev_fingerprint,
        "consecutiveBadRuns": consecutive,
        "lastGoodAsof": last_good,
        "lastStatus": status,
    })

    return {
        "asof": now_iso,
        "status": status,
        "successRate": success_rate,
        "okCount": ok_count,
        "attempted": len(attempted),
        "consecutiveBadRuns": consecutive,
        "lastGoodAsof": last_good,
    }
