# Ticket Tracker

A self-hosted tool for tracking Ticketmaster resale prices. You add an event URL, pick the listings you care about, set a target price, and it emails or notifies you when something drops. All of it runs locally with `docker compose up`.

Resale only — primary/face value tickets aren't tracked because those prices don't move.

---

## What it does

- Scrapes resale listings for any Ticketmaster event and saves them to a local SQLite database
- Polls each event on its own schedule with randomized jitter so it doesn't look like a bot
- Before firing an alert, does a confirmation re-scrape to make sure the price drop is real and not a blip
- Sends alerts via email (SMTP) or desktop notification, with a configurable cooldown so you don't get spammed
- Shows a price history chart per listing so you can see whether a price has been trending down or just dipped once
- Handles sold-out events — add the URL and it'll start tracking as soon as resale listings appear
- Detects when an event date changes and flags it on the event page
- Has optional AI features (natural language event search, price history summaries) that work with OpenAI, Gemini, or a local Ollama model — disabled automatically if no API key is set

---

## Getting started

You need Docker and Docker Compose. That's it.

```bash
git clone https://github.com/your-username/ticket-tracker.git
cd ticket-tracker
cp .env.example .env
# Fill in your notification credentials (see below)
docker compose up
```

Then open [http://localhost:8000](http://localhost:8000).

The first build takes a few minutes because Playwright downloads Chromium (~600 MB). After that, starts are fast.

---

## Configuration

Everything goes in `.env`. Copy `.env.example` and fill in what you want to use — nothing is required except at least one notification channel if you actually want alerts.

```env
# Email (SMTP)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password
ALERT_EMAIL_TO=your-email@gmail.com

# Desktop notifications (doesn't work inside Docker — local only)
DESKTOP_NOTIFICATIONS_ENABLED=false

# Polling defaults (can be overridden per watch)
DEFAULT_ALERT_COOLDOWN_MINUTES=60
DEFAULT_REFRESH_INTERVAL_MINUTES=30

# AI features (optional — app works fine without this)
AI_PROVIDER=openai
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=
AI_MODEL=gpt-4o

# Local Ollama (free, no API key needed):
# AI_PROVIDER=ollama
# AI_BASE_URL=http://host.docker.internal:11434/v1
# AI_API_KEY=not-needed
# AI_MODEL=gemma3:latest
```

If you don't configure any notification channels the app still runs and records price history — you just won't get alerts.

---

## How it works

```
Add a Ticketmaster URL
        ↓
Initial scrape finds all resale listings
        ↓
You pick which listings to watch and set a target price
        ↓
Scheduler polls on your chosen interval (with ±15% jitter)
        ↓
Each poll saves a price snapshot
        ↓
Price drops below target → confirmation re-scrape
        ↓
Still below target → alert fires, cooldown starts
```

---

## Stack

| Layer         | What                              |
| ------------- | --------------------------------- |
| Language      | Python 3.11+                      |
| Web           | FastAPI + Jinja2 + vanilla JS     |
| Charts        | Chart.js                          |
| Scraping      | Playwright + playwright-stealth   |
| Scheduler     | APScheduler                       |
| Database      | SQLite via SQLAlchemy (async)     |
| Migrations    | Alembic                           |
| Notifications | smtplib, plyer                    |
| AI            | openai client (provider-agnostic) |
| Container     | Docker Compose                    |

```
ticket-tracker/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   ├── scraper/
│   │   ├── browser.py
│   │   └── ticketmaster.py
│   ├── scheduler/
│   │   └── engine.py
│   ├── notifier/
│   │   ├── base.py
│   │   ├── email.py
│   │   └── desktop.py
│   ├── ai/
│   │   └── client.py
│   ├── api/
│   │   └── routes.py
│   └── templates/
└── data/
    └── db.sqlite3
```

---

## A few things worth knowing

**Image size is ~2 GB.** Most of that is Chromium and the X11 libraries needed to run a headful browser inside the container. Headful mode is intentional — it's much harder to fingerprint than `--headless`.

**Desktop notifications don't work in Docker.** The container has no access to your desktop display. Use email when running via Docker. Desktop notifications work if you run the app locally outside of Docker.

**Gmail users:** Use port 465 with an App Password, not your account password. Port 587 with STARTTLS tends to time out.

---

## License

MIT
