# speedhive-tools-ui

A self-hosted, password-gated web dashboard for exploring [MyLaps Speedhive](https://speedhive.mylaps.com) racing data. It syncs an organization's events, sessions, results, and lap times into a local SQLite database, then layers on driver statistics, track-record curation, and backups — all served by a single Flask app. The scraping/storage/analysis engine lives in the [`speedhive-tools`](https://github.com/ncrosty58/speedhive-tools) library, vendored here as a git submodule.

## Quickstart

**Docker (recommended):**

```bash
git clone --recursive <this-repo>
cd speedhive-tools-ui
echo "SPEEDHIVE_UI_PASSWORD=choose-a-password" > .env
docker compose up -d --build     # serves on http://localhost:8854
```

**Local development:**

```bash
just install     # venv + deps + editable speedhive-tools
just run         # flask dev server on :8854
just test        # pytest
just lint        # ruff
```

Sign in with the site password, add an organization by its Speedhive ID (the number in a `speedhive.mylaps.com/organizations/<id>` URL), then run a sync under **Settings > Data**.

## What's inside

**Overview** — the org dashboard: driver search across synced results, the event list, and championships. Every driver name in the app links to that driver's report.

**Stats** — computed from synced data and cached per view:

- **Drivers** — a sortable, searchable table of every driver: starts, wins, podiums, percentages, and consistency, each with its rank.
- **Driver report** — per-driver deep dive: rank tiles (starts / wins / podiums / consistency vs the whole field), best laps per class with track-record gaps, session history with lap-by-lap times, and a consistency trend.
- **Class Pace** — average lap time per class, by year (configurable classes and trend line).
- **Participation** — distinct racers per year, with a per-class drilldown.
- **Most Improved** — each driver's earliest vs most recent qualifying-year consistency.
- **Wins & Podiums** — class-position wins and podiums across every synced race.

Stats views recalculate automatically in a background task after every sync that changes data. The automatic pass covers the primary (default-filter) views; any extra filter combination you explore refreshes on demand from its own page, and combos untouched for 45 days are pruned during a full recalculation.

**Track Records** — scans synced announcer text for lap-record callouts and proposes candidates for human review. Approved records join a curated list (with full edit history per record); rejected ones are remembered so they aren't re-proposed. Parsing is either a built-in matcher or AI-assisted via Google Gemini. Optional email notifications (via Resend) fire when new candidates appear. A public, CORS-enabled JSON feed of the curated list is available for embedding.

**Settings > Data** — sync controls with live progress, one-click stats recalculation, record rescan, portable NDJSON ZIP backups (create/restore/download), curated-record import/export, and org deletion.

## Endpoints for external automation

These are the only login-exempt routes (besides `/login` and static files), designed for CI schedules and scripts:

| Method | Path | Purpose |
| :-- | :-- | :-- |
| `GET` | `/org/<id>/track-records/update/status` | Freshness check — is a sync needed? |
| `POST` | `/org/<id>/track-records/update` | Sync-if-stale + scan; returns a `task_id` or `{"skipped": true}` |
| `GET` | `/org/<id>/track-records/update/<task_id>` | Poll that task |
| `GET` | `/org/<id>/track-records/curated.json` | Public curated-records feed (CORS-enabled, 5-min cache) |

`POST /org/<id>/track-records/notify` (re-send the review email) is login-gated and additionally honors a `?secret=` matching `SYNC_SECRET` when set.

## Configuration

Environment variables (loaded from `.env` at the repo root):

| Variable | Description | Default |
| :-- | :-- | :-- |
| `SPEEDHIVE_UI_PASSWORD` | Site password (single shared login) | unset — login always fails |
| `SPEEDHIVE_DATA_DIR` | Data directory | `./data` |
| `SPEEDHIVE_DB_PATH` | SQLite database path | `<data dir>/speedhive.db` |
| `SPEEDHIVE_PORT` | Port for `python app.py` | `8854` |
| `FLASK_SECRET_KEY` | Session-cookie signing key | insecure fallback — set in production |
| `SPEEDHIVE_MAX_ORG_EVENTS` | Event cap applied to every sync | `150` |
| `SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS` | Recent events re-checked on incremental sync | `3` |
| `SYNC_SECRET` | Optional shared secret for the notify endpoint | unset (open) |
| `TRACK_RECORDS_STALE_HOURS` | Cache age before the CI endpoint re-syncs | `20` |

Per-organization settings (parser engine, Gemini key/model, minimum laps, email notification config) are edited in **Settings > General** and stored in `data/orgs/<org_id>/settings.json`. They resolve through `speedhive.settings` in the submodule — the same mechanism the CLI uses — with per-org overrides winning over global environment defaults (per-org env vars use a `NAME_<org_id>` suffix pattern). Email/notification settings are UI-only; the library never sends email.

## Architecture

```
├── app/
│   ├── __init__.py           # Flask app factory, login gate, DB bootstrap, org-context injection
│   ├── db.py                 # Cache-first reads between the Speedhive API and SQLite
│   ├── analysis_cache.py     # Process-wide org analysis bundle (~175 MB/org, built once per data version)
│   ├── tasks.py              # Background threads: sync, track-record scans, stats recalculation
│   ├── notifications.py      # Resend email dispatch for the review queue
│   ├── utils.py              # Date/format helpers
│   └── routes/
│       ├── auth.py           # Shared-password login/logout
│       ├── dashboard.py      # Overview, driver search, live fastest-laps browser
│       ├── organizations.py  # Add org, sync, backups, org deletion, Data page
│       ├── sessions.py       # Event/session/results/lap-times views
│       ├── stats.py          # All stats views, driver report, recalc engine
│       └── track_records.py  # Curation workflow, review queue, public feed, per-org settings
├── templates/                # Jinja2 templates (dark theme, custom design system in base.html)
├── speedhive-tools/          # Submodule: scraping/storage/analysis engine + CLI
├── tests/                    # Flask route/integration tests
├── app.py                    # Entry point
├── Dockerfile                # gunicorn, single worker (deliberate: shares the in-process analysis cache)
├── docker-compose.yml
└── justfile / Makefile       # install / run / test / lint
```

Every page reads through the SQLite cache; the Speedhive API is only contacted during syncs. Expensive per-org analysis is computed once per data version and shared across requests — which is why the Docker image runs a single gunicorn worker with threads.

## Data directory

```
data/
├── speedhive.db              # All synced entities + cached stats + background-task state
├── orgs/<org_id>/
│   ├── settings.json         # Per-org settings (shared with the CLI)
│   └── track_records/        # curated / pending / rejected NDJSON + parse cache
└── saved_dumps/<org_id>/     # Backup snapshots (NDJSON + manifest)
```

Backups are portable: restore a ZIP on any other install to migrate an organization.

## Development

```bash
just install     # or: make install
just test        # UI tests;  cd speedhive-tools && pytest  for the library
just lint
```

CI (GitHub Actions) runs lint + both test suites on Python 3.12 with recursive submodules.
