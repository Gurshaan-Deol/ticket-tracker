# 🎟 Ticket Tracker

A self-hosted Ticketmaster resale ticket price tracker. Add events, set target prices per listing, and get notified when prices drop — via Telegram, email, or desktop notification. Runs entirely with `docker compose up`.

> **Tracks resale (secondary market) prices only.** Primary/face value tickets are not included.

---

## Features

- **Price history charts** — interactive Chart.js line graph per listing showing price movement over time, with toggle show/hide and sort-aware rendering
- **Multi-channel alerts** — Telegram bot, email (SMTP), and desktop notifications; enable any combination via `.env`
- **Alert controls** — per-watch target price, configurable cooldown window, and minimum drop percentage filter to suppress noise
- **Confirmation re-scrape** — before firing an alert, the app scrapes again to confirm the price is real (not a blip)
- **Bot evasion** — headful Playwright via Xvfb virtual display, `playwright-stealth`, user agent rotation, and randomized polling jitter
- **Pause/resume events** — stop tracking without losing price history
- **AI features (optional)** — natural language event search and plain-English price history summaries; works with OpenAI, Gemini, or a local Ollama model. Disabled if no API key is set.
- **Zero paid services** — SQLite database, no cloud dependencies

---

## Screenshots

> _Add a screenshot of your dashboard and one of the event page with a chart here before publishing._

---

## Quick Start

**Prerequisites:** Docker and Docker Compose.

```bash
git clone https://github.com/your-username/ticket-tracker.git
cd ticket-tracker
cp .env.example .env
# Edit .env with your notification credentials (see Configuration below)
docker compose up
```

Open [http://localhost:8000](http://localhost:8000).

The initial build takes a few minutes — Playwright downloads Chromium (~600 MB) during the build step. Subsequent starts are fast.

---

## Configuration

All config is in `.env`. Copy `.env.example` and fill in what you need.

```env
# Telegram (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Email via SMTP (optional)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
ALERT_EMAIL_TO=

# Desktop notifications (optional — local only, does not work in Docker)
DESKTOP_NOTIFICATIONS_ENABLED=false

# Scheduler defaults (can be overridden per-watch)
DEFAULT_ALERT_COOLDOWN_MINUTES=60
DEFAULT_REFRESH_INTERVAL_MINUTES=30

# AI features (optional — app works without this)
AI_PROVIDER=openai
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=
AI_MODEL=gpt-4o

# For local Ollama (free):
# AI_PROVIDER=ollama
# AI_BASE_URL=http://host.docker.internal:11434/v1
# AI_API_KEY=not-needed
# AI_MODEL=gemma3:latest
```

If no notification channels are configured the app still runs and records price history — you just won't get alerts.

---

## How It Works

```
User adds Ticketmaster URL
        ↓
Initial scrape — discovers all resale listings and saves them
        ↓
User selects listings to watch, sets target price
        ↓
Scheduler polls each event on its own interval (with ±15% jitter)
        ↓
Each poll saves a PriceSnapshot per listing
        ↓
If price < target: confirmation re-scrape → alert fires if still true
        ↓
Cooldown and min-drop-% filters applied before notification
```

---

## Architecture

| Layer | Stack |
|---|---|
| Language | Python 3.11+ |
| Web framework | FastAPI |
| Templates | Jinja2 + vanilla JS |
| Charts | Chart.js (CDN) |
| Scraping | Playwright + playwright-stealth |
| Scheduler | APScheduler (AsyncIOScheduler) |
| Database | SQLite via SQLAlchemy (async) |
| Migrations | Alembic |
| Notifications | python-telegram-bot, smtplib, plyer |
| AI | openai Python client (provider-agnostic) |
| Containerization | Docker Compose |

```
ticket-tracker/
├── app/
│   ├── main.py              # FastAPI app, lifespan startup/shutdown
│   ├── config.py            # Pydantic Settings, loads .env
│   ├── database.py          # SQLAlchemy async engine + session factory
│   ├── models.py            # All ORM models
│   ├── scraper/
│   │   ├── browser.py       # Playwright context, stealth, UA rotation
│   │   └── ticketmaster.py  # Page parsing, listing discovery
│   ├── scheduler/
│   │   └── engine.py        # APScheduler, per-watch job management
│   ├── notifier/
│   │   ├── base.py          # Abstract BaseNotifier
│   │   ├── telegram.py
│   │   ├── email.py
│   │   └── desktop.py
│   ├── ai/
│   │   └── client.py        # Provider-agnostic AI wrapper
│   ├── api/
│   │   └── routes.py        # All FastAPI route handlers
│   └── templates/           # Jinja2 HTML templates
└── data/
    └── db.sqlite3           # Gitignored, persisted via Docker volume
```

---

## Notes

**Docker image size (~2 GB):** Expected for a Playwright-based scraper. Chromium alone is ~600 MB, plus X11/Xvfb system libraries required for headful mode. Headful mode is intentional — it's significantly harder for bot-detection systems to fingerprint than `--headless`.

**Desktop notifications in Docker:** Not supported — the container has no access to your desktop display. Use email or Telegram when running via Docker. Desktop notifications work when running the app locally outside of Docker.

**Resale prices only:** The scraper targets Ticketmaster's resale (fan-to-fan) inventory. Primary/face-value tickets come from a different endpoint and are not tracked. This is intentional — primary prices are fixed and don't drop.

---

## License

MIT
