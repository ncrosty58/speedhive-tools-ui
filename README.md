# speedhive-tools-ui

A password-gated Flask dashboard for exploring and curating [MyLaps Speedhive](https://speedhive.com) racing data. It wraps the [`speedhive-tools`](https://github.com/ncrosty58/speedhive-tools) scraping/analysis engine (vendored here as a git submodule) with a browsable UI, a local SQLite cache, background sync jobs, and an announcer-text track-records review workflow — plus a handful of endpoints meant to be called by external automation (CI schedules, scripts), not just a browser.

## What it does

- **Organization dashboard** — add a Speedhive organization by ID, browse its events/sessions/results, and search for a driver's results across recently cached events.
- **Session detail views** — per-session results, lap charts, announcer messages, and per-driver lap-time traces (with optional IQR outlier filtering).
- **Local sync cache** — a background thread pulls organizations/events/sessions/results/laps/announcements from Speedhive into a SQLite database (`app/db.py`, `app/tasks.py`), incrementally or via a full re-sync, with live progress reporting and a stop button.
- **Consistency & class-pace analytics** — driver-consistency rankings and per-class average-pace-by-year charts (`app/routes/stats.py`), backed by the `speedhive-tools` analyzers and cached in the `org_stats` table.
- **Track records curation** — scans synced announcer text for lap-record callouts, proposes candidates for review, and maintains curated/rejected lists per organization (`app/routes/track_records.py`), with an optional Gemini-LLM parser as an alternative to the regex parser. Every human edit to a curated record keeps a full history (what changed, and when), visible by clicking its "Modified" badge.
- **Email notifications** — sends a Resend email when new track-record candidates appear, with per-organization or global sender/recipient configuration and fingerprint-based de-duplication (`app/notifications.py`).
- **Offline dumps** — export/import an organization's cache as a portable NDJSON ZIP archive for backup or migration between installs (`app/routes/organizations.py`).

## Architecture

```
├── app/
│   ├── __init__.py           # Flask app factory, shared SpeedhiveStorage/client globals, org-context injection
│   ├── db.py                 # Cache-first read/write helpers between the Speedhive API and SQLite storage
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
├── speedhive-tools/            # Submodule: the scraping/storage/analysis engine, CLI, and settings resolution
├── tests/                      # Flask route/integration tests
├── app.py                      # WSGI entry point
├── Dockerfile / docker-compose.yml
└── justfile / Makefile         # Local dev commands (install, run, test, lint)
```

Every route reads through `app/db.py`, which serves from the SQLite cache when available and falls back to a live Speedhive API call (via `speedhive.wrapper.SpeedhiveClient`) otherwise, persisting the result for next time. Per-organization settings (Gemini keys, parsing engine, min-laps) are resolved through `speedhive.settings` in the submodule, shared with the CLI; email/notification settings are UI-only and never referenced there.

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
| `TRACK_RECORDS_STALE_HOURS` | Hours before track-records cache is considered stale (drives `needs_sync`). | `20` |
| `SYNC_SECRET` | If set, required as `?secret=` on `/org/<id>/track-records/notify`. | unset (open) |
| `FLASK_SECRET_KEY` | Flask session-cookie signing key. | built-in fallback |

### Shared vs. per-organization settings

Two integrations — Resend email and Gemini LLM parsing — can be configured either **globally** (bare env var, e.g. `RESEND_API_KEY` in `.env`) or **per organization** (via the Settings page, stored under `overrides` in `data/orgs/<org_id>/settings.json`, exposed to the process as `RESEND_API_KEY_<org_id>`). An org's own override always wins; otherwise the global value is used as a fallback.

A per-org `settings.json` also controls:
- `notifications`: `enabled` / `de_duplicate` — whether and how often to email on new track-record candidates.
- `parsing.engine`: `"regex"` (default) or `"llm"` — how announcer text is parsed for track records.
- `stats.min_laps`: minimum lap count for a driver to appear in consistency rankings.

A template is provided at [settings.json.example](settings.json.example).

## API / endpoints

Most routes render an HTML page and require the site password (session cookie from `/login`). A handful are exempt from login specifically because they're meant to be called by scripts/CI (`app/__init__.py`'s `PUBLIC_ENDPOINTS`), marked **public** below. All `<org_id>`/`<session_id>`/`<event_id>`/`<task_id>` path segments are plain identifiers.

### Auth
| Method | Path | Notes |
| :--- | :--- | :--- |
| GET/POST | `/login` | Public. Form login against `SPEEDHIVE_UI_PASSWORD`. |
| POST | `/logout` | |

### Dashboard & search
| Method | Path | Notes |
| :--- | :--- | :--- |
| GET | `/` | Home dashboard: org's events/championships, inline driver search. |
| GET | `/org/<org_id>/lap-records` | Live fastest-lap-per-class browser computed from the synced cache. |
| GET | `/track-records` | Redirects to `lap-records` for a given `org_id`. |
| GET | `/track-records/export.json` | JSON export of the ad-hoc lap-records scan (not the curated list). |
| GET | `/org-search`, `/driver-search` | Redirect helpers used by the nav search forms. |

### Organizations & sync
| Method | Path | Notes |
| :--- | :--- | :--- |
| GET/POST | `/organizations/add` | Look up and add a new org by Speedhive ID. |
| GET | `/org/<org_id>` | Redirects to `/` with `org_id` set. |
| GET | `/org/<org_id>/operations` | Data/ops tab: cache status, refresh controls, dump list. |
| POST | `/org/<org_id>/refresh` | Legacy synchronous refresh (no-JS fallback); JSON path starts a background task. |
| POST | `/org/<org_id>/refresh/start` | Always-async: starts a background full/incremental refresh, returns `task_id`. |
| GET | `/refresh/status/<task_id>` | Poll refresh-task progress/result. |
| POST | `/refresh/stop/<task_id>` | Request a running refresh to stop. |
| POST | `/org/<org_id>/clear-cache` | Deletes the org's local cache/dumps entirely. |
| POST | `/org/<org_id>/dumps` | Generate an offline NDJSON dump. |
| GET | `/org/<org_id>/dumps/latest.zip`, `/org/<org_id>/dumps/<dump_key>.zip` | Download a dump snapshot as a ZIP. |
| POST | `/org/<org_id>/dumps/import` | Upload and import a dump ZIP. |
| POST | `/org/<org_id>/dumps/delete`, `/org/<org_id>/dumps/<dump_key>/delete` | Delete a dump snapshot. |
| GET | `/org/<org_id>/export-lap-records.ndjson` | Streamed NDJSON of lap records from the cache. |

### Events & sessions
| Method | Path | Notes |
| :--- | :--- | :--- |
| GET | `/event/<event_id>` | Event detail with its sessions. |
| GET | `/session/<session_id>`, `/session/<session_id>/results` | Session results, lap chart, announcements. |
| GET | `/session/<session_id>/export-laps.json` | Raw laps as a JSON download. |
| GET | `/session/<session_id>/driver/<driver_id>/laps` | Per-driver lap-time trace (`driver_id` accepts `cid:`, `sn:`, or `pos:` prefixes). |

### Stats
| Method | Path | Notes |
| :--- | :--- | :--- |
| GET | `/org/<org_id>/stats` | Consistency rankings overview. |
| POST | `/org/<org_id>/stats/generate` | (Re)compute and cache consistency stats. |
| GET | `/org/<org_id>/stats/driver/<driver_name>` | Per-driver session-by-session breakdown. |
| GET | `/org/<org_id>/stats/class-pace` | Average lap time by class/year chart. |
| POST | `/org/<org_id>/stats/class-pace/generate` | (Re)compute and cache class-pace data. |

### Track records
| Method | Path | Notes |
| :--- | :--- | :--- |
| GET | `/org/<org_id>/track-records/update/status` | **Public.** Cache freshness / `needs_sync` check (used by CI). |
| POST | `/org/<org_id>/track-records/update` | **Public.** Sync-if-stale + scan; starts a background task, returns `task_id` (or `{"skipped": true}`). |
| GET | `/org/<org_id>/track-records/update/<task_id>` | **Public.** Poll that task's status. |
| POST | `/org/<org_id>/track-records/scan` | Diff the already-synced cache against curated, without contacting Speedhive. |
| GET | `/org/<org_id>/track-records/review` | Pending candidates queue. |
| POST | `/org/<org_id>/track-records/review/approve` (alias `/apply`) | Approve one candidate into curated. |
| POST | `/org/<org_id>/track-records/review/reject` | Reject one candidate. |
| POST | `/org/<org_id>/track-records/review/approve-all` | Bulk-approve all `new_record` candidates. |
| GET | `/org/<org_id>/track-records/curated` | Curated list, with add/edit/delete forms. |
| POST | `/org/<org_id>/track-records/curated/add` | Add a record manually. |
| POST | `/org/<org_id>/track-records/curated/edit` | Edit a record (Speedhive-sourced edits are flagged `modified` with full edit history). |
| POST | `/org/<org_id>/track-records/curated/remove` (alias `/delete`) | Remove a record (blocked from future scans, or permanently for manual records). |
| POST | `/org/<org_id>/track-records/curated/dedupe` | Remove Speedhive-added duplicates of already-curated records. JSON response. |
| GET | `/org/<org_id>/track-records/curated.ndjson` | NDJSON export of curated records. |
| POST | `/org/<org_id>/track-records/curated/import` | Import curated records from an uploaded NDJSON file. |
| GET | `/org/<org_id>/track-records/rejected` | Rejected list. |
| POST | `/org/<org_id>/track-records/rejected/restore` | Restore a rejected record. |
| GET | `/org/<org_id>/track-records/history` | Background-task run history for this org. |
| GET | `/org/<org_id>/track-records/curated.json` (alias `/org/<org_id>/track-records.json`) | **Public.** CORS-enabled, cached (5 min) JSON of the curated list, for embedding elsewhere. |
| POST | `/org/<org_id>/track-records/notify` | Send (or re-send) the review-queue email on demand. Requires `?secret=` if `SYNC_SECRET` is set. |
| GET/POST | `/org/<org_id>/settings` | Per-org notification/parsing/stats/Gemini/Resend configuration. |

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
