# Nutshell

AI-powered YouTube video summarizer. Paste a video URL and get structured trading notes in seconds — thesis, tickers, price levels, catalysts, risks, and actionable takeaways.

Live at: https://web-production-c271f.up.railway.app

---

## What it does

1. Fetches the video transcript (via Supadata or youtube-transcript-api)
2. Sends it to Groq's Llama 3.3 70B model for analysis
3. Returns structured markdown notes with tickers, price levels, and key takeaways
4. Saves your history and fires watchlist alerts when tracked tickers appear in new videos

---

## Plans

| Plan | Price | Summaries |
|------|-------|-----------|
| Free | $0 | 3 / month |
| Basic | $19 / month | 30 / month |
| Pro | $49 / month | Unlimited + Watchlist alerts |

Payments via Stripe. Manage or cancel anytime from the Account tab.

---

## Tech stack

- **Backend**: FastAPI + Python 3.11
- **AI**: Groq API (llama-3.3-70b-versatile)
- **Transcripts**: Supadata API (handles YouTube's cloud IP restrictions)
- **Auth**: bcrypt password hashing, httponly session cookies
- **Payments**: Stripe Checkout + Customer Portal + webhooks
- **Email alerts**: Resend
- **Database**: SQLite
- **Deployment**: Railway (auto-deploy from GitHub)

---

## Self-hosting

### Requirements

- Python 3.9+
- [Groq API key](https://console.groq.com) (free)
- Stripe account (for payments)
- Supadata API key (for transcripts on cloud deployments)

### Install

```bash
git clone https://github.com/echang793/nutshell
cd nutshell
pip3 install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

Required:
```
GROQ_API_KEY=your_groq_key
```

Optional (for payments and alerts):
```
STRIPE_SECRET_KEY=sk_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_BASIC_PRICE_ID=price_...
STRIPE_PRO_PRICE_ID=price_...
SUPADATA_API_KEY=your_supadata_key
RESEND_API_KEY=re_...
RESEND_FROM=alerts@yourdomain.com
```

### Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000

---

## Files

```
main.py          FastAPI app — routes for auth, summarize, payments, watchlist
summarizer.py    Transcript fetching and Groq summarization logic
db.py            SQLite — users, sessions, summaries, watchlist
auth.py          Password hashing and session management
payments.py      Stripe checkout, portal, and webhook handling
alerts.py        Resend watchlist email alerts
static/          Frontend SPA (index.html)
Procfile         Railway / Heroku process definition
data/            SQLite database (gitignored)
```
