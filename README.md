# Project Odin Overwatch

A **safe, public-source defense event monitor**. Odin Overwatch polls
**publicly available news/event APIs** (starting with GDELT), runs a
simple rule-based classifier over the results, deduplicates them, stores
them locally in SQLite, and posts a Discord notification when a notable
event is detected.

> **Disclaimer — read first.**
> This project is for **educational and public-source open-data monitoring
> only**. It does **not** detect, track, or target real-time military
> systems. It does **not** perform surveillance of private individuals,
> weapon targeting, evasion, or any operational defense function. It is
> **not** a real-time warning system and **must not** be used as one.
> All data comes from publicly indexed news articles and other open
> sources after events have been publicly reported.

## What it does (MVP)

1. Queries **GDELT 2.0 DOC API** for a curated list of defense-relevant
   keywords (drone strike, ballistic missile, NOTAM, etc.).
2. Classifies each article into one of:
   `DRONE`, `MISSILE`, `ROCKET`, `AIR_DEFENSE`, `AIRSPACE`, `UNKNOWN`.
3. Assigns a severity of `LOW`, `MEDIUM`, or `HIGH` based on a small
   weighted-keyword score.
4. Deduplicates events by URL and exact title against a local SQLite DB.
5. Saves new events to SQLite.
6. Posts a Discord embed for any new event at or above the configured
   minimum severity (default: `MEDIUM`).

## Project layout

```
project-odin-overwatch/
├── app/
│   ├── __init__.py
│   ├── config.py            # env loading + shared Config object
│   ├── main.py              # entry point — runs one monitor pass
│   ├── sources/
│   │   ├── __init__.py
│   │   └── gdelt.py         # GDELT DOC API client
│   ├── classify/
│   │   ├── __init__.py
│   │   ├── rules.py         # event-type rules
│   │   └── scoring.py       # severity scoring
│   ├── notify/
│   │   ├── __init__.py
│   │   └── discord.py       # Discord webhook
│   └── db/
│       ├── __init__.py
│       ├── database.py      # SQLAlchemy session + helpers
│       └── models.py        # Event ORM model
├── data/
│   └── .gitkeep             # SQLite DB lives here at runtime
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Setup

Requires **Python 3.10+**.

```bash
# 1. Clone / cd into the project folder
cd project-odin-overwatch

# 2. Create and activate a virtual environment
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the env template and edit values
cp .env.example .env
# Then open .env and paste your Discord webhook URL (optional but recommended).
```

### Creating a Discord webhook

1. Open your Discord server → pick a channel.
2. **Channel settings → Integrations → Webhooks → New Webhook**.
3. Copy the **Webhook URL** and paste it into `DISCORD_WEBHOOK_URL` in `.env`.

If you leave `DISCORD_WEBHOOK_URL` blank, the app will still fetch,
classify, and store events — it just won't post anywhere.

## Run

From the project root (so `app` is importable):

```bash
python -m app.main
```

You should see log output like:

```
2026-05-22 10:00:00 [INFO] odin: Odin Overwatch starting | timespan=24h max=75 lang=english
2026-05-22 10:00:00 [INFO] app.sources.gdelt: Querying GDELT for keyword: 'drone strike'
2026-05-22 10:00:01 [INFO] app.sources.gdelt:   -> 42 article(s) for 'drone strike'
...
2026-05-22 10:00:08 [INFO] odin: Fetched 312 raw article(s) across all keywords
2026-05-22 10:00:08 [INFO] odin: After in-batch dedup: 187 unique article(s)
2026-05-22 10:00:08 [INFO] odin: Event types: {'DRONE': 51, 'MISSILE': 23, 'AIR_DEFENSE': 17, ...}
2026-05-22 10:00:08 [INFO] odin: Severity:    {'MEDIUM': 38, 'HIGH': 12, 'LOW': 137}
2026-05-22 10:00:08 [INFO] odin: NEW [DRONE/HIGH] reuters.com — Officials report drone strike on ...
2026-05-22 10:00:09 [INFO] odin: Done. new_events=42 notified=12 (min_severity=MEDIUM)
```

### Running on a schedule

Each run is idempotent — the URL/title dedup means re-running won't spam
Discord with the same events.

**Windows (Task Scheduler, PowerShell):**

```powershell
# Adjust the two paths to match your install. Repeats every 15 minutes.
$python = "C:\Users\Jeremiah\source\Odin Overwatch\project-odin-overwatch\.venv\Scripts\python.exe"
$workdir = "C:\Users\Jeremiah\source\Odin Overwatch\project-odin-overwatch"

$action  = New-ScheduledTaskAction -Execute $python -Argument "-m app.main" -WorkingDirectory $workdir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 9999)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "OdinOverwatch" -Action $action -Trigger $trigger -Settings $settings
```

To stop / remove: `Unregister-ScheduledTask -TaskName "OdinOverwatch"`.

**Linux (cron):**

```cron
*/15 * * * * cd /path/to/project-odin-overwatch && .venv/bin/python -m app.main >> logs/cron.log 2>&1
```

**Heartbeat:** set `HEARTBEAT_INTERVAL_HOURS=24` in `.env` and Odin will
post a daily one-liner to Discord summarizing the latest run. If you stop
seeing it, the scheduler died.

**Log file:** rotates at 5 MB with 5 backups, at the path set by
`LOG_FILE` (default: `logs/odin.log`). Console output still works as before.

## Configuration

All configuration lives in `.env` (see `.env.example` for the full list).

| Variable | Description | Default |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Webhook URL for alert delivery | _empty_ |
| `GDELT_TIMESPAN` | How far back to search (`15min`, `1h`, `24h`, `7d`, ...) | `24h` |
| `GDELT_MAX_RECORDS` | Max records per keyword query (GDELT caps near 250) | `75` |
| `GDELT_LANGUAGE` | GDELT `sourcelang` filter; blank for any language | `english` |
| `DATABASE_URL` | SQLAlchemy URL for storage | `sqlite:///data/odin.db` |
| `MIN_NOTIFY_SEVERITY` | Floor for Discord alerts: `LOW`/`MEDIUM`/`HIGH`. Invalid values fail fast at startup. | `MEDIUM` |
| `MAX_NOTIFICATIONS_PER_RUN` | Hard cap on Discord posts per run (incl. retries). Uncapped events are retried next run. | `20` |
| `LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` | `INFO` |

The keyword list is defined in `app/config.py` as `DEFAULT_KEYWORDS` —
edit it there to add/remove queries.

## How the MVP works

`app/main.run_once()` is the entire pipeline. Each run does:

0. **Validate config.** Bad values for `MIN_NOTIFY_SEVERITY`, `LOG_LEVEL`,
   or `MAX_NOTIFICATIONS_PER_RUN` cause an immediate exit instead of
   silently muting alerts.
1. **Retry unsent alerts.** Any event previously saved with `notified=0`
   (because Discord was down or the webhook wasn't configured yet) is
   re-attempted first, up to the run cap.
2. **Fetch.** `sources/gdelt.py` calls the GDELT DOC API once per keyword
   with retry-on-429/5xx + exponential backoff, parses the JSON, and
   normalizes dates.
3. **In-batch dedup + URL normalization.** The same article often shows up
   under multiple keywords. `main.py` strips tracking params (`utm_*`,
   `fbclid`, etc.), normalizes whitespace in titles, and drops duplicates
   inside the batch before touching the DB.
4. **Classify.** `classify/rules.py` maps title + snippet to an
   `event_type` and matched-keyword list. `classify/scoring.py` produces
   an integer score and a `LOW`/`MEDIUM`/`HIGH` severity.
5. **Persist per-event.** Each new event is saved in its **own** SQLite
   transaction. A duplicate URL slipping past the dedup check raises
   `IntegrityError` for that event only — the rest of the batch is
   unaffected.
6. **Notify per-event.** For events that meet `MIN_NOTIFY_SEVERITY` and
   haven't exceeded `MAX_NOTIFICATIONS_PER_RUN`, `notify/discord.py`
   posts an embed (with 429 `Retry-After` handling and 5xx retries).
   On success, `notified=1` is set in a **second** transaction so an
   in-flight notification can't be rolled back by a later save failure.

The two-transaction-per-event model is what makes scheduled runs safe:
saved events are durable before any network call, and successful
notifications survive subsequent failures.

Everything is logged to stdout with timestamps; severity counts and a
final summary line make it easy to spot-check what happened.

## Inspecting the data

The SQLite DB lives at `data/odin.db` by default. You can browse it with
any SQLite client, or quickly from the CLI:

```bash
sqlite3 data/odin.db "SELECT id, event_type, severity, country, substr(title,1,60) FROM events ORDER BY created_at DESC LIMIT 20;"
```

## Dashboard (v2)

A read-only web dashboard with two visualizations: a dark Leaflet 2D map
and a 3D globe (globe.gl). Switch between them with the toggle in the
header. Markers are colored by severity (gray/orange/red) and the side
panel lists the latest events with filters for time window, event type,
and minimum severity.

Run the API:

```bash
python -m app.api.server
# or:
uvicorn app.api.server:app --reload --port 8000
```

Then open <http://127.0.0.1:8000/>.

The dashboard refreshes every 60s and remembers your filter choices via
`localStorage`. It only displays events that the gazetteer was able to
geo-tag; non-geo events are still saved to SQLite and visible via
`/api/events?geo_only=false`.

### Backfilling geo for existing rows

If you already had data in `data/odin.db` before adding geo enrichment,
run the backfill once:

```bash
python -m scripts.backfill_geo
```

This walks every event with `latitude IS NULL` and runs the gazetteer
matcher on its title + snippet. Safe to re-run.

### API endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /api/events` | Filterable event list. Params: `hours`, `event_type`, `min_severity`, `country`, `geo_only`, `limit`. |
| `GET /api/stats` | Aggregate counts by type, severity, and country. Param: `hours`. |
| `GET /api/health` | Liveness check. |

## Suggested next steps (v3+)

- **More public sources** — drop new modules into `app/sources/`:
  - `faa_tfr.py` — FAA TFR / NOTAM feeds for airspace restrictions.
  - `launch_library.py` — Launch Library 2 for rocket launches.
  - `acled.py` — ACLED conflict event API (requires API key).
- **Richer geo** — extend the gazetteer in `app/enrich/geo.py`, or swap
  in spaCy NER / Nominatim for finer-grained locations.
- **Richer classification** — extend `rules.py` with regional / actor
  rules, or replace the rule-based classifier with an LLM behind the
  same `find_matches()` interface.
- **Supabase / Postgres** — change `DATABASE_URL` to a Postgres URL; the
  SQLAlchemy layer needs no other changes.
- **More alert channels** — add `notify/email.py` (SMTP) or
  `notify/sms.py` (Twilio) alongside Discord.
- **Scheduling** — run `python -m app.main` from cron / systemd /
  GitHub Actions / Task Scheduler on a 5–15 minute cadence.
- **Live dashboard updates** — replace 60s polling with WebSocket pushes
  from the FastAPI app when new events land.

## Responsible use

This project must remain a **public-source news/event monitor**. Do not
extend it to:

- target, guide, or evade weapons;
- track real-time positions of military assets;
- surveil private individuals;
- act as a primary or operational warning system for human safety.

If you need real warnings (severe weather, civil defense, etc.), use
official government channels.

## License

Choose a license appropriate for your use case (MIT, Apache-2.0, etc.)
and add a `LICENSE` file before publishing.
