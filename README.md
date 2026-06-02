# Ticket Tracker

A self-hosted Ticketmaster resale ticket price tracker. Paste a Ticketmaster event URL, set a target price and check interval, and the app polls the resale market on a schedule and sends you an alert when prices drop below your target. You can also watch for sold-out sections or fully sold-out events to become available. A web dashboard shows all tracked events, current prices, price history charts, and a full alert log.

## Features

### Event Management
- Add events by Ticketmaster resale URL — triggers an immediate scrape on add
- Event name and date parsed automatically from the URL slug and live page data
- Duplicate URLs rejected at submission
- Sold-out events can be added and monitored; you are alerted when listings first appear
- Ended events are detected automatically, marked as ended, and all tracking jobs are cancelled
- Events can be paused and resumed without losing watches or price history
- Events can be deleted with full cascade cleanup of all watches, snapshots, and alerts
- Per-event notes field (inline editable, no page reload)
- Date-change detection: if Ticketmaster updates the event date, a badge appears on the dashboard and an alert is sent

### Price Tracking
- Set price watches on individual listings (section × ticket quantity)
- Configure target price, check interval (minimum 5 minutes), and alert cooldown per watch
- Multiple watches per event, each on an independent schedule
- Price snapshots recorded on every scrape
- Price history chart shown inline on the event page (Chart.js, on-demand fetch)
- Quantity filter pills to view only 2-ticket or 4-ticket listings, for example
- Listings from previous scrapes are preserved as unavailable rather than deleted

### Availability Watches
- Watch for a specific unavailable section × quantity combination to reappear
- Watch for any listing to appear on a fully sold-out event (alerts on the cheapest available ticket)
- Optional price cap on availability alerts (only alert if price is under $X)
- Venue section list populated from the Ticketmaster event manifest on first add
- GA / Lawn / General Admission sections grouped for cleaner display

### Alerts
- Email alerts via SMTP (Gmail app passwords supported; STARTTLS on port 587 or SSL on port 465)
- Desktop notifications via OS notification system (local use only — not inside Docker)
- Alert cooldown enforced per listing: configurable minimum time between repeat alerts
- All alert channels are optional — the app runs and records history without any of them
- Price drop alerts, availability alerts (section appears), and date-change alerts
- Alert history page with a persistent log of every alert fired

### Dashboard and UI
- Dark theme with Tailwind CSS (CDN, no build step)
- Event cards showing name, venue, date, lowest current price, last checked time, and check interval
- Status badges: Stable, Price Dropping, Price Rising, Near Target, Alert Sent, Sold Out, Event Ended
- Sort events by date, current price, or percentage to target
- Filter events by name
- Dismissible date-change and alert-sent badges on event cards
- Progress overlay with animated spinner during scrapes
- Available Now and Unavailable Sections tables per event

### Settings
- Configurable default refresh interval and alert cooldown for price watches
- Configurable defaults for availability watches and sold-out event watches
- Defaults applied when creating new watches; overridable per watch
- Settings persisted in the database and survive container restarts

### AI (optional)
- Price history summarization: 2–3 sentence plain English trend summary per listing (requires at least 5 snapshots)
- Event search query parsing: converts natural language to structured search fields
- Works with OpenAI, Google Gemini, or a local Ollama instance
- All AI features are hidden from the UI when no API key is configured; the app is fully functional without AI

## Stack

- **Language:** Python 3.11+
- **Web framework:** FastAPI + Jinja2 templates + vanilla JS
- **Scraper:** Playwright + playwright-stealth (headful Chromium, Xvfb in Docker)
- **Scheduler:** APScheduler (AsyncIOScheduler)
- **Database:** SQLite via SQLAlchemy async ORM (aiosqlite driver)
- **Migrations:** Alembic
- **Charts:** Chart.js (CDN)
- **Containerisation:** Docker Compose

## Requirements

- Docker and Docker Compose
- A Ticketmaster resale URL for any event you want to track
- (Optional) An SMTP account for email alerts — a Gmail app password works

## Setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd ticket-tracker
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`. Every variable is optional — the app starts and tracks events without any of them configured.

| Variable | Required | Description |
|---|---|---|
| `SMTP_HOST` | No | SMTP server hostname. Default: `smtp.gmail.com` |
| `SMTP_PORT` | No | SMTP port. Use `587` for STARTTLS or `465` for SSL. Default: `587` |
| `SMTP_USER` | For email alerts | The sending email address |
| `SMTP_PASSWORD` | For email alerts | App password for the sending account. For Gmail, generate one at myaccount.google.com/apppasswords — do not use your main account password |
| `ALERT_EMAIL_TO` | For email alerts | Address where alert emails are delivered |
| `DESKTOP_NOTIFICATIONS_ENABLED` | No | Set `true` to enable OS desktop notifications. Only works when running locally outside Docker. Default: `false` |
| `DEFAULT_ALERT_COOLDOWN_MINUTES` | No | Default minimum minutes between repeated alerts for the same listing. Default: `60` |
| `DEFAULT_REFRESH_INTERVAL_MINUTES` | No | Default check interval in minutes when creating new watches. Default: `30` |
| `AI_PROVIDER` | No | AI provider name, e.g. `openai`, `google`, `ollama` |
| `AI_BASE_URL` | No | Base URL for the AI API. For OpenAI: `https://api.openai.com/v1`. For Ollama: `http://host.docker.internal:11434/v1` |
| `AI_API_KEY` | For AI features | API key. Leave empty to disable all AI features entirely |
| `AI_MODEL` | No | Model name, e.g. `gpt-4o`, `gemini-1.5-pro`, `gemma3:latest` |

### 3. Run

```bash
docker compose up
```

Open `http://localhost:8000` in your browser. The first startup takes several minutes because Docker pulls the base image and installs Playwright's Chromium browser.

## Usage

### Adding an event

1. Find the resale listing page on Ticketmaster for the event you want to track. The URL should point to the fan-to-fan resale section.
2. On the dashboard, click **+ Add Event**, paste the URL, and click **Track Event**.
3. The app scrapes the page immediately (typically 15–30 seconds). If tickets are available you land on the event detail page. If the event is currently sold out it is saved anyway and monitoring begins automatically.

### How price tracking works

The scraper uses a headful Chromium browser with stealth patches to load the Ticketmaster resale page and intercept the API response containing listing data. Each listing is stored as a section name + ticket quantity combination. The cheapest available price for each listing is recorded as a snapshot on every poll. On the event page, listings in the **Available Now** table show the current cheapest price. Clicking a price opens an inline price history chart. Use the quantity filter pills to narrow the table to a specific ticket count.

### Setting a price watch

1. On the event page, select a ticket quantity using the filter pills at the top of the Available Now table.
2. Click **Set Watch** on the listing you want to monitor.
3. Enter your target price, check interval, and alert cooldown.
4. Click **Save**. A scheduler job starts immediately.

When the scraped price drops to or below your target the app sends an alert through all configured channels, then waits for the cooldown period before alerting again for the same listing.

### Availability watches (sold-out sections)

Sections that appear in the Ticketmaster venue manifest but have no current listings show in the **Unavailable Sections** table. Click the bell icon next to any section, choose a ticket quantity, and click **Add alert**. You will be notified when that section × quantity combination appears in the resale market.

For a fully sold-out event (no listings at all) a panel is shown where you can set a watch for any listing to appear, with an optional price cap.

### Settings page

Click **Settings** in the navigation bar to change the default values pre-filled when creating new watches. There are separate defaults for price watches, availability watches, and sold-out event watches.

### Alert history

Click **Alert History** in the navigation bar for a full log of every alert fired, with event name, section, quantity, price at alert, and timestamp.

## Data and persistence

All data is stored in a SQLite database inside a Docker named volume (`ticket_data`) mounted at `/app/data/db.sqlite3` inside the container. The database survives `docker compose down` and restarts.

To back up the database:

```bash
docker compose cp app:/app/data/db.sqlite3 ./backup.sqlite3
```

To restore from backup:

```bash
docker compose cp ./backup.sqlite3 app:/app/data/db.sqlite3
```

The `data/` directory on the host is not the live database — always work with the volume copy inside the container.

## Notes

- This tool is intended for personal, self-hosted use. Scraping Ticketmaster violates their Terms of Service. Use at your own risk.
- The scraper runs Chromium in headful (non-headless) mode with `playwright-stealth` applied to reduce bot-detection signals. Xvfb provides a virtual display inside the Docker container. Ticketmaster may still block requests intermittently; the app logs failures and retries on the next scheduled interval.
- Only resale (fan-to-fan) listings are tracked. Primary sale retail tickets are not included.
- A global asyncio lock prevents concurrent scrapes. If you have many watches with short intervals, scrapes from different watches queue behind each other.
- Desktop notifications require direct access to the host display and do not work inside Docker. Use email alerts when running containerised.
- All intervals and cooldowns have a minimum of 5 minutes.
