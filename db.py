"""SQLite / Turso persistence layer.

Set TURSO_URL + TURSO_TOKEN env vars to use Turso (production).
Falls back to local SQLite when those vars are absent (dev).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "summaries.db"

_SCHEMA_STMTS = [
    """CREATE TABLE IF NOT EXISTS summaries (
        id          TEXT    PRIMARY KEY,
        video_id    TEXT    NOT NULL,
        url         TEXT    NOT NULL,
        title       TEXT    NOT NULL DEFAULT '',
        notes       TEXT    NOT NULL,
        brief       INTEGER NOT NULL DEFAULT 0,
        word_count  INTEGER NOT NULL DEFAULT 0,
        tickers     TEXT    NOT NULL DEFAULT '[]',
        created_at  TEXT    NOT NULL,
        user_id     TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_created ON summaries(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_video   ON summaries(video_id)",
    "CREATE INDEX IF NOT EXISTS idx_user    ON summaries(user_id)",
    """CREATE TABLE IF NOT EXISTS users (
        id                     TEXT PRIMARY KEY,
        email                  TEXT NOT NULL UNIQUE,
        password_hash          TEXT NOT NULL,
        plan                   TEXT NOT NULL DEFAULT 'free',
        summary_count          INTEGER NOT NULL DEFAULT 0,
        monthly_count          INTEGER NOT NULL DEFAULT 0,
        stripe_customer_id     TEXT,
        stripe_subscription_id TEXT,
        created_at             TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_email  ON users(email)",
    "CREATE INDEX IF NOT EXISTS idx_user_stripe ON users(stripe_customer_id)",
    """CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_session_user ON sessions(user_id)",
    """CREATE TABLE IF NOT EXISTS watchlist (
        user_id TEXT NOT NULL,
        ticker  TEXT NOT NULL,
        PRIMARY KEY (user_id, ticker),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS watchlist_alerts (
        user_id    TEXT NOT NULL,
        ticker     TEXT NOT NULL,
        summary_id TEXT NOT NULL,
        sent_at    TEXT NOT NULL,
        PRIMARY KEY (user_id, ticker, summary_id)
    )""",
]


# ── Turso HTTP client ──────────────────────────────────────────────────

def _turso_arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": str(v)}
    return {"type": "text", "value": str(v)}


def _turso_val(v: dict):
    t = v.get("type", "null")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    return v.get("value")


class _TursoRow(dict):
    """dict subclass that supports row["col"] access, compatible with sqlite3.Row."""
    pass


class _TursoCursor:
    def __init__(self, cols: list[str], rows: list):
        self._rows = [
            _TursoRow(zip(cols, [_turso_val(v) for v in row]))
            for row in rows
        ]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _TursoConn:
    """Pure-HTTP Turso client. No native compilation required."""

    def __init__(self, url: str, token: str):
        import requests as _req
        self._sess  = _req.Session()
        self._sess.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        })
        self._url = url.strip().replace("libsql://", "https://") + "/v2/pipeline"

    def execute(self, sql: str, params=()):
        body = {"requests": [
            {"type": "execute", "stmt": {
                "sql":  sql,
                "args": [_turso_arg(p) for p in params],
            }},
            {"type": "close"},
        ]}
        r = self._sess.post(self._url, json=body, timeout=15)
        r.raise_for_status()
        data   = r.json()
        result = data["results"][0]
        if result.get("type") == "error":
            raise Exception(result.get("error", {}).get("message", "Turso error"))
        res    = result.get("response", {}).get("result", {})
        cols   = [c["name"] for c in res.get("cols", [])]
        rows   = res.get("rows", [])
        return _TursoCursor(cols, rows)

    def commit(self):   pass   # HTTP API is auto-commit per request
    def rollback(self): pass
    def __enter__(self):        return self
    def __exit__(self, *args):  return False


# ── Connection factory ─────────────────────────────────────────────────

_turso: _TursoConn | None = None
_turso_lock = threading.Lock()


def _conn():
    global _turso
    turso_url   = os.environ.get("TURSO_URL", "").strip()
    turso_token = os.environ.get("TURSO_TOKEN", "").strip()

    if turso_url and turso_token:
        if _turso is None:
            with _turso_lock:
                if _turso is None:   # double-checked locking
                    conn = _TursoConn(turso_url, turso_token)
                    for stmt in _SCHEMA_STMTS:
                        conn.execute(stmt)
                    _turso = conn
        return _turso

    # Local dev — plain SQLite
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    inner = sqlite3.connect(str(DB_PATH))
    inner.row_factory = sqlite3.Row
    for stmt in _SCHEMA_STMTS:
        inner.execute(stmt)
    inner.commit()
    return inner


_MAX_SUMMARIES = 1000


def save_summary(id: str, video_id: str, url: str, notes: str,
                 brief: bool, word_count: int, tickers: list[dict],
                 user_id: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO summaries
               (id, video_id, url, notes, brief, word_count, tickers, created_at, user_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (id, video_id, url, notes, int(brief), word_count,
             json.dumps(tickers), datetime.now().isoformat(), user_id),
        )
        c.execute(
            """DELETE FROM summaries WHERE id IN (
               SELECT id FROM summaries ORDER BY created_at DESC
               LIMIT -1 OFFSET ?)""",
            (_MAX_SUMMARIES,),
        )


def get_summary(id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM summaries WHERE id=?", (id,)).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def get_history(limit: int = 50, user_id: str | None = None) -> list[dict]:
    with _conn() as c:
        if user_id:
            rows = c.execute(
                "SELECT * FROM summaries WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM summaries ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_ticker_tally() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT tickers FROM summaries").fetchall()

    tally: dict[str, dict] = {}
    for row in rows:
        for t in json.loads(row["tickers"] or "[]"):
            ticker = t["ticker"]
            sent   = t.get("sentiment", "neutral")
            if ticker not in tally:
                tally[ticker] = {"ticker": ticker, "count": 0,
                                 "bullish": 0, "bearish": 0, "neutral": 0}
            tally[ticker]["count"] += 1
            tally[ticker][sent]    += 1

    return sorted(tally.values(), key=lambda x: x["count"], reverse=True)


# ── Users ─────────────────────────────────────────────────────────────

def create_user(id: str, email: str, password_hash: str) -> dict:
    with _conn() as c:
        c.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?,?,?,?)",
            (id, email, password_hash, datetime.now().isoformat()),
        )
    return get_user_by_id(id)


def get_user_by_email(email: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def get_user_by_stripe_customer(customer_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)
        ).fetchone()
    return dict(row) if row else None


def update_user(id: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [id]
    with _conn() as c:
        c.execute(f"UPDATE users SET {cols} WHERE id=?", vals)


def increment_summary_count(user_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE users SET summary_count=summary_count+1, monthly_count=monthly_count+1 WHERE id=?",
            (user_id,)
        )


def reset_monthly_count(user_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET monthly_count=0 WHERE id=?", (user_id,))


# ── Sessions ──────────────────────────────────────────────────────────

def create_session(token: str, user_id: str, expires_at: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires_at),
        )


def get_user_by_session(token: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            """SELECT u.* FROM users u
               JOIN sessions s ON s.user_id = u.id
               WHERE s.token=? AND s.expires_at > ?""",
            (token, datetime.now().isoformat()),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


# ── Watchlist ─────────────────────────────────────────────────────────

def get_watchlist(user_id: str) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ticker FROM watchlist WHERE user_id=?", (user_id,)
        ).fetchall()
    return [r["ticker"] for r in rows]


def add_to_watchlist(user_id: str, ticker: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, ticker) VALUES (?,?)",
            (user_id, ticker.upper()),
        )


def remove_from_watchlist(user_id: str, ticker: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM watchlist WHERE user_id=? AND ticker=?",
            (user_id, ticker.upper()),
        )


def get_watchers_for_tickers(tickers: list[str]) -> list[dict]:
    if not tickers:
        return []
    placeholders = ",".join("?" * len(tickers))
    with _conn() as c:
        rows = c.execute(
            f"""SELECT w.user_id, w.ticker FROM watchlist w
                JOIN users u ON u.id = w.user_id
                WHERE u.plan='pro' AND w.ticker IN ({placeholders})""",
            tickers,
        ).fetchall()
    return [dict(r) for r in rows]


def alert_already_sent(user_id: str, ticker: str, summary_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM watchlist_alerts WHERE user_id=? AND ticker=? AND summary_id=?",
            (user_id, ticker, summary_id),
        ).fetchone()
    return row is not None


def record_alert_sent(user_id: str, ticker: str, summary_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO watchlist_alerts (user_id, ticker, summary_id, sent_at) VALUES (?,?,?,?)",
            (user_id, ticker, summary_id, datetime.now().isoformat()),
        )


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["tickers"] = json.loads(d.get("tickers") or "[]")
    d["brief"]   = bool(d.get("brief", 0))
    return d
