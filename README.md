# speedhive-tools-ui

A password-gated Flask dashboard for exploring and curating [MyLaps Speedhive](https://speedhive.com) racing data. It wraps the [`speedhive-tools`](https://github.com/ncrosty58/speedhive-tools) scraping/analysis engine (vendored here as a git submodule) with a browsable UI, a local SQLite cache, background sync jobs, and an announcer-text track-records review workflow.

## What it does

- **Organization dashboard** — add a Speedhive organization by ID, browse its events/sessions/results, and search for a driver's results across recently cached events.
- **Session detail views** — per-session results, lap charts, announcer messages, and per-driver lap-time traces (with optional IQR outlier filtering).
- **Local sync cache** — a background thread pulls organizations/events/sessions/results/laps/announcements from Speedhive into a SQLite database (`app/db.py`, `app/tasks.py`), incrementally or via a full re-sync, with live progress reporting and a stop button.
- **Consistency & class-pace analytics** — computed driver-consistency rankings and per-class average-pace-by-year charts (`app/routes/stats.py`), backed by the `speedhive-tools` analyzers and cached in the `org_stats` table.
- **Track records curation** — scans synced announcer text for lap-record callouts, proposes candidates for review, and maintains curated/rejected lists per organization (`app/routes/track_records.py`), with an optional Gemini-LLM parser as an alternative to the regex parser.
- **Email notifications** — sends a Resend email when new track-record candidates appear, with per-organization or global sender/recipient configuration and fingerprint-based de-duplication (`app/notifications.py`).
- **Offline dumps** — export/import a organization's cache as a portable NDJSON ZIP archive for backup or migration between installs (`app/routes/organizations.py`).

## Architecture

```
├── app/
│   ├── __init__.py           # Flask app factory, shared SpeedhiveStorage/client globals, org-context injection
│   ├── db.py                 # Cache-first read/write helpers between the Speedhive API and SQLite storage
│   ├── env_config.py         # Per-org vs. global settings resolution (env vars backed by data/org_settings.env)
│   ├── notifications.py      # Resend email dispatch for the track-records review queue
│   ├── tasks.py              # Background threads for org refresh and track-records sync/scan, task-state persistence
│   ├── utils.py              # Date/time, cache-metadata, and JSON file helpers
│   └── routes/
│       ├── auth.py           # Single shared-password login/logout
│       ├── dashboard.py      # Home dashboard, driver search, live "lap records" browser
│       ├── organizations.py  # Add org, refresh/clear cache, offline dump export/import/download
│       ├── sessions.py       # Event/session/results/lap-times views
│       ├── stats.py          # Consistency rankings, driver breakdown, class-pace charts
│       └── track_records.py  # Curated/review/rejected track-records workflow, per-org settings
├── templates/                 # Jinja2 templates (Bootstrap-based UI)
├── static/                    # CSS/JS assets
├── data/                      # Gitignored: SQLite DB, per-org settings, saved dumps
├── speedhive-tools/            # Submodule: the scraping/storage/analysis engine and CLI
├── tests/                      # Flask route/integration tests
├── app.py                      # WSGI entry point
├── Dockerfile / docker-compose.yml
└── justfile / Makefile         # Local dev commands (install, run, test, lint)
```

The app never talks to Speedhive directly from a template — every route reads through `app/db.py`, which serves from the SQLite cache when available and falls back to a live API call (via the shared `speedhive.wrapper.SpeedhiveClient`) otherwise, persisting the result for next time.

## Configuration

Configured via environment variables (loaded from a `.env` file at the repo root) and per-organization `settings.json` files under `data/orgs/<org_id>/`.

### Environment variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `SPEEDHIVE_UI_PASSWORD` | Shared password protecting the whole site. | **required** |
| `SPEEDHIVE_DATA_DIR` | Root directory for the SQLite cache, dumps, and per-org settings. | `./data` |
| `SPEEDHIVE_DB_PATH` | Explicit path to the SQLite cache file. | `<SPEEDHIVE_DATA_DIR>/speedhive.db` |
| `SPEEDHIVE_PORT` | Port for the Flask dev server. | `8854` |
| `SPEEDHIVE_MAX_ORG_EVENTS` | Cap on events fetched/refreshed per organization. | `150` |
| `SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS` | Recent-events re-checked on every incremental refresh. | `3` |
| `FLASK_SECRET_KEY` | Flask session-cookie signing key. | built-in fallback |

### Shared vs. per-organization settings

Two integrations — Resend email and Gemini LLM parsing — can be configured either **globally** (bare env var, e.g. `RESEND_API_KEY` in `.env`) or **per organization** (via the Settings page, stored as `overrides` in `data/orgs/<org_id>/settings.json` and exposed to the process as `RESEND_API_KEY_<org_id>`). An org's own override always wins; otherwise the global value is used as a fallback. See `app/env_config.py` for the resolution logic.

A per-org `settings.json` also controls:
- `notifications`: `enabled` / `de_duplicate` — whether and how often to email on new track-record candidates.
- `parsing.engine`: `"regex"` (default) or `"llm"` — how announcer text is parsed for track records.
- `stats.min_laps`: minimum lap count for a driver to appear in consistency rankings.

A template is provided at [settings.json.example](settings.json.example).

## Local development

```bash
git clone https://github.com/ncrosty58/speedhive-tools-ui.git
cd speedhive-tools-ui
git submodule update --init --recursive

just install   # creates venv/, installs requirements.txt + speedhive-tools in editable mode
```

Create a `.env` file at the repo root with at least `SPEEDHIVE_UI_PASSWORD`, then:

```bash
just run       # flask run --host=0.0.0.0 --port=8854
just test      # pytest
just lint      # ruff check
```

`justfile` and `Makefile` provide the same commands if you don't have `just` installed.

## Docker deployment

```bash
docker compose up -d --build
```

The container mounts `./data` onto `/app/data` so the SQLite cache, per-org settings, and saved dumps persist across rebuilds. `SPEEDHIVE_UI_PASSWORD` and `SPEEDHIVE_MAX_ORG_EVENTS` are passed through from the host's `.env` file (see `docker-compose.yml`). The image is served by gunicorn on port `8854`.

## Testing

```bash
just test
```

`tests/conftest.py` makes the app importable directly (falling back to the `speedhive-tools/src` tree if the submodule isn't pip-installed), and `tests/test_app.py` covers the Flask routes end-to-end against a temporary SQLite database.
