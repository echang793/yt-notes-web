"""Watchlist email alerts via Resend."""

from __future__ import annotations

import os

import db

RESEND_API_KEY   = os.environ.get("RESEND_API_KEY", "")
ALERT_FROM_EMAIL = os.environ.get("ALERT_FROM_EMAIL", "alerts@ytnotes.app")
APP_URL          = os.environ.get("APP_URL", "http://localhost:8000")


def send_watchlist_alerts(summary_id: str, url: str, tickers: list[dict]) -> None:
    """
    For each ticker in this summary, find users watching it (pro plan only)
    and send them an email — once per user per summary.
    """
    if not RESEND_API_KEY:
        return
    if not tickers:
        return

    ticker_symbols = {t["ticker"] for t in tickers}

    # Find all pro users watching any of these tickers
    watchers = db.get_watchers_for_tickers(list(ticker_symbols))
    if not watchers:
        return

    import resend
    resend.api_key = RESEND_API_KEY

    # Group tickers by user so one email covers all matches
    user_tickers: dict[str, list[dict]] = {}
    for w in watchers:
        uid    = w["user_id"]
        ticker = w["ticker"]
        sent   = db.alert_already_sent(uid, ticker, summary_id)
        if sent:
            continue
        if uid not in user_tickers:
            user_tickers[uid] = []
        match = next((t for t in tickers if t["ticker"] == ticker), None)
        if match:
            user_tickers[uid].append(match)

    for user_id, matched in user_tickers.items():
        user = db.get_user_by_id(user_id)
        if not user:
            continue
        _send_alert_email(user["email"], matched, url, summary_id)
        for t in matched:
            db.record_alert_sent(user_id, t["ticker"], summary_id)


def _send_alert_email(email: str, tickers: list[dict], url: str, summary_id: str) -> None:
    import resend

    ticker_lines = "".join(
        f"<li><strong>${t['ticker']}</strong> — "
        f"<span style='color:{'green' if t['sentiment']=='bullish' else 'red' if t['sentiment']=='bearish' else 'gray'}'>"
        f"{t['sentiment']}</span></li>"
        for t in tickers
    )
    summary_url = f"{APP_URL}/?summary={summary_id}"

    html = f"""
    <div style="font-family:monospace;max-width:520px;margin:auto;padding:24px;
                background:#0f1117;color:#e2e8f0;border-radius:12px">
      <h2 style="color:#38bdf8;margin-top:0">📈 Watchlist Alert</h2>
      <p>Tickers on your watchlist were mentioned in a new video:</p>
      <ul style="padding-left:1.2em;line-height:1.8">{ticker_lines}</ul>
      <p><strong>Source:</strong> <a href="{url}" style="color:#38bdf8">{url[:80]}</a></p>
      <a href="{summary_url}" style="display:inline-block;margin-top:16px;
         background:#0284c7;color:white;padding:10px 20px;border-radius:8px;
         text-decoration:none;font-size:14px">View Full Notes →</a>
      <p style="margin-top:24px;font-size:12px;color:#475569">
        Manage your watchlist at <a href="{APP_URL}" style="color:#38bdf8">{APP_URL}</a>
      </p>
    </div>
    """
    try:
        resend.Emails.send({
            "from":    ALERT_FROM_EMAIL,
            "to":      email,
            "subject": f"Watchlist: {', '.join('$' + t['ticker'] for t in tickers)} mentioned",
            "html":    html,
        })
    except Exception as e:
        print(f"[alerts] Failed to send to {email}: {e}")
