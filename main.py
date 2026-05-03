"""FastAPI web app for YouTube video note-taker."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import alerts
import auth
import db
import payments
import summarizer

app = FastAPI(title="Nutshell")

STATIC_DIR  = Path(__file__).parent / "static"
CONFIG_PATH = Path.home() / ".yt-notes.json"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

SESSION_COOKIE = "yt_session"


def _get_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "")
    if key:
        return key
    try:
        import json
        key = json.loads(CONFIG_PATH.read_text()).get("groq_api_key", "")
    except (OSError, Exception):
        pass
    if not key:
        raise HTTPException(500, "GROQ_API_KEY not set. Run: python3 ~/Desktop/yt-notes/notes.py setup")
    return key


def _current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    return auth.get_user_from_token(token)


def _require_user(request: Request) -> dict:
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Not logged in.")
    return user


# ── Static ────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ── Auth ──────────────────────────────────────────────────────────────

class AuthRequest(BaseModel):
    email:    str
    password: str


@app.post("/api/auth/register")
def api_register(req: AuthRequest, response: Response):
    email = req.email.strip().lower()
    if not email or not req.password:
        raise HTTPException(400, "Email and password are required.")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if db.get_user_by_email(email):
        raise HTTPException(409, "An account with that email already exists.")

    uid           = str(uuid.uuid4())
    password_hash = auth.hash_password(req.password)
    user          = db.create_user(uid, email, password_hash)
    token         = auth.create_session(uid)

    response.set_cookie(SESSION_COOKIE, token, max_age=86400 * 30,
                        httponly=True, samesite="lax")
    return _user_payload(user)


@app.post("/api/auth/login")
def api_login(req: AuthRequest, response: Response):
    email = req.email.strip().lower()
    user  = db.get_user_by_email(email)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password.")

    token = auth.create_session(user["id"])
    response.set_cookie(SESSION_COOKIE, token, max_age=86400 * 30,
                        httponly=True, samesite="lax")
    return _user_payload(user)


@app.post("/api/auth/logout")
def api_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(request: Request):
    user = _current_user(request)
    if not user:
        return {"user": None}
    return {"user": _user_payload(user)}


def _user_payload(user: dict) -> dict:
    return {
        "id":            user["id"],
        "email":         user["email"],
        "plan":          user["plan"],
        "summary_count": user["summary_count"],
        "monthly_count": user["monthly_count"],
    }


# ── Summarize ─────────────────────────────────────────────────────────

class SummarizeRequest(BaseModel):
    url:   str
    brief: bool = False
    model: str  = summarizer.GROQ_MODEL


@app.post("/api/summarize")
async def api_summarize(req: SummarizeRequest, request: Request):
    user     = _current_user(request)
    api_key  = _get_api_key()
    video_id = summarizer.extract_video_id(req.url)
    if not video_id:
        raise HTTPException(400, f"Could not parse a video ID from: {req.url}")

    # Enforce plan limits for logged-in users
    if user:
        allowed, reason = payments.can_summarize(user)
        if not allowed:
            raise HTTPException(402, reason)

    try:
        transcript, cached = await run_in_threadpool(
            summarizer.fetch_transcript, video_id, api_key, req.model
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    word_count = len(transcript.split())

    try:
        notes = await run_in_threadpool(
            summarizer.summarize, transcript, api_key, req.model, req.brief
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    tickers = summarizer.extract_tickers_from_notes(notes)
    sid     = str(uuid.uuid4())[:8]
    uid     = user["id"] if user else None

    await run_in_threadpool(
        db.save_summary, sid, video_id, req.url, notes, req.brief, word_count, tickers, uid
    )

    if uid:
        await run_in_threadpool(db.increment_summary_count, uid)

    # Fire watchlist alerts in the background (non-blocking)
    if tickers:
        import asyncio
        asyncio.create_task(
            run_in_threadpool(alerts.send_watchlist_alerts, sid, req.url, tickers)
        )

    return {
        "id":         sid,
        "video_id":   video_id,
        "url":        req.url,
        "notes":      notes,
        "brief":      req.brief,
        "word_count": word_count,
        "cached":     cached,
        "tickers":    tickers,
    }


@app.get("/api/summaries/{sid}")
def api_get_summary(sid: str):
    row = db.get_summary(sid)
    if not row:
        raise HTTPException(404, "Summary not found.")
    return row


@app.get("/api/history")
def api_history(request: Request, limit: int = 50):
    user = _current_user(request)
    uid  = user["id"] if user else None
    return db.get_history(limit, user_id=uid)


@app.get("/api/tickers")
def api_tickers():
    return db.get_ticker_tally()


# ── Payments ──────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan: str  # "basic" or "pro"


@app.post("/api/payments/checkout")
def api_checkout(req: CheckoutRequest, request: Request):
    user        = _require_user(request)
    base_url    = str(request.base_url).rstrip("/")
    success_url = f"{base_url}/"
    cancel_url  = f"{base_url}/"
    try:
        url = payments.create_checkout_session(user, req.plan, success_url, cancel_url)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"url": url}


@app.post("/api/payments/portal")
def api_portal(request: Request):
    user       = _require_user(request)
    return_url = str(request.base_url).rstrip("/") + "/"
    try:
        url = payments.create_portal_session(user, return_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"url": url}


@app.post("/api/payments/webhook")
async def api_webhook(request: Request):
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        await run_in_threadpool(payments.handle_webhook, payload, sig_header)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ── Watchlist ─────────────────────────────────────────────────────────

@app.get("/api/watchlist")
def api_get_watchlist(request: Request):
    user = _require_user(request)
    if user["plan"] != "pro":
        raise HTTPException(403, "Watchlist is a Pro plan feature.")
    return {"tickers": db.get_watchlist(user["id"])}


class WatchlistRequest(BaseModel):
    ticker: str


@app.post("/api/watchlist")
def api_add_watchlist(req: WatchlistRequest, request: Request):
    user = _require_user(request)
    if user["plan"] != "pro":
        raise HTTPException(403, "Watchlist is a Pro plan feature.")
    ticker = req.ticker.strip().upper()
    if not ticker or len(ticker) > 10:
        raise HTTPException(400, "Invalid ticker symbol.")
    db.add_to_watchlist(user["id"], ticker)
    return {"tickers": db.get_watchlist(user["id"])}


@app.delete("/api/watchlist/{ticker}")
def api_remove_watchlist(ticker: str, request: Request):
    user = _require_user(request)
    if user["plan"] != "pro":
        raise HTTPException(403, "Watchlist is a Pro plan feature.")
    db.remove_from_watchlist(user["id"], ticker.upper())
    return {"tickers": db.get_watchlist(user["id"])}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
