"""Core transcript fetching and summarization logic."""

from __future__ import annotations

import itertools
import re
import sys
import threading
import time
from pathlib import Path

CACHE_DIR  = Path(__file__).parent / "data" / "transcripts"
GROQ_MODEL = "llama-3.3-70b-versatile"

_SYSTEM_PROMPT = (
    "You are an expert financial analyst and note-taker specializing in stock trading content. "
    "Extract and organize key information from YouTube transcripts into clear, actionable notes. "
    "Be precise with numbers, tickers, and price levels. "
    "Never invent information that is not in the transcript. "
    "Format your response in clean markdown."
)

_FULL_PROMPT = """\
Analyze this YouTube video transcript and create structured trading notes.

TRANSCRIPT:
{transcript}

Create notes with these exact sections:

## Thesis
One or two sentences summarizing the core investment or trading idea.

## Stocks & Tickers Mentioned
List each stock with:
- **$TICKER — Company Name**: Bullish / Bearish / Neutral — what was said

## Key Price Levels
Specific prices, targets, support/resistance levels, or moving averages mentioned:
- $TICKER: [level] — context (e.g. "support at $150", "target $200", "stop below $145")
If none were mentioned, write "None specified."

## Catalysts & Time Horizon
- What events or factors are expected to drive the move
- Timeframe: short-term (days/weeks) / medium-term (months) / long-term

## Risks
Key risks or concerns mentioned by the presenter.

## Actionable Takeaways
3–5 bullet points of the most important things to act on or monitor.

## Plain-English Summary
2–3 sentences summarizing the whole video for someone who hasn't watched it.\
"""

_BRIEF_PROMPT = """\
Analyze this YouTube video transcript and write a very short briefing.

TRANSCRIPT:
{transcript}

Respond with ONLY these three sections (keep each tight):

## Thesis
One sentence — the core idea.

## Top Takeaways
- Bullet 1
- Bullet 2
- Bullet 3

## Tickers
Comma-separated list of every stock ticker mentioned, each labeled (bullish/bearish/neutral).
If none mentioned, write "None."\
"""

_TRANSLATE_PROMPT = """\
Translate the following transcript to English. Output only the translated text, nothing else.

TRANSCRIPT:
{transcript}\
"""


def extract_video_id(url: str) -> str | None:
    match = re.search(r'(?:v=|youtu\.be/|shorts/|embed/)([a-zA-Z0-9_-]{11})', url)
    return match.group(1) if match else None


def _make_yt_api():
    import os
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi
    scraper_key = os.environ.get("SCRAPERAPI_KEY", "")
    if scraper_key:
        proxy_url = f"http://scraperapi:{scraper_key}@proxy-server.scraperapi.com:8001"
        session = requests.Session()
        session.proxies = {"http": proxy_url, "https": proxy_url}
        session.verify = False
        return YouTubeTranscriptApi(http_client=session)
    return YouTubeTranscriptApi()


def fetch_transcript(video_id: str, api_key: str, model: str = GROQ_MODEL) -> tuple[str, bool]:
    """Return (transcript_text, from_cache)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{video_id}.txt"
    if cache.exists():
        return cache.read_text(), True

    api        = _make_yt_api()
    translated = False
    try:
        tlist = api.list(video_id)
        transcript = tlist.find_transcript(["en", "en-US", "en-GB"])
        segments   = transcript.fetch()
    except Exception:
        try:
            tlist      = api.list(video_id)
            transcript = tlist.find_generated_transcript(["en"])
            segments   = transcript.fetch()
        except Exception:
            try:
                tlist      = api.list(video_id)
                transcript = next(iter(tlist))
                segments   = transcript.fetch()
                translated = True
            except Exception as e:
                raise ValueError(f"Could not fetch transcript: {e}")

    text = " ".join(s.text for s in segments)
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    if translated:
        text = _call_groq(_TRANSLATE_PROMPT.format(transcript=text[:60_000]),
                          api_key, model, max_tokens=4096)

    cache.write_text(text)
    return text, False


def _call_groq(prompt: str, api_key: str, model: str, max_tokens: int = 2048,
               retries: int = 2) -> str:
    from groq import Groq, APIConnectionError, APIStatusError

    client = Groq(api_key=api_key)
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content
        except (APIConnectionError, APIStatusError) as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f"Groq API failed after {retries + 1} attempts: {e}")


def summarize(transcript: str, api_key: str, model: str = GROQ_MODEL,
              brief: bool = False) -> str:
    if len(transcript) > 80_000:
        transcript = transcript[:80_000] + "\n[transcript truncated]"
    prompt = (_BRIEF_PROMPT if brief else _FULL_PROMPT).format(transcript=transcript)
    return _call_groq(prompt, api_key, model)


def extract_tickers_from_notes(notes_text: str) -> list[dict]:
    """Parse tickers and sentiment from structured notes markdown."""
    results = []
    ticker_re = re.compile(
        r'\*\*\$([A-Z]{1,5})[^*]*\*\*[^:]*:\s*(Bullish|Bearish|Neutral)',
        re.IGNORECASE,
    )
    for m in ticker_re.finditer(notes_text):
        results.append({
            "ticker":    m.group(1).upper(),
            "sentiment": m.group(2).lower(),
        })
    # Also catch bare $TICKER not in structured block
    bare_re  = re.compile(r'\$([A-Z]{2,5})\b')
    found    = {r["ticker"] for r in results}
    for m in bare_re.finditer(notes_text):
        t = m.group(1).upper()
        if t not in found:
            results.append({"ticker": t, "sentiment": "neutral"})
            found.add(t)
    return results
