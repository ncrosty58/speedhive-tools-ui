# speedhive-tools-ui

A password-gated Flask dashboard for [MyLaps Speedhive](https://speedhive.com)
racing data: browse organizations, events and session results, chart lap
times with outlier filtering, rank driver consistency, and run a background
track-record curation pipeline with email notifications. Built on the
[`speedhive-tools`](https://github.com/ncrosty58/speedhive-tools) engine,
vendored here as a git submodule.

---

## Architecture

```
├── app/
│   ├── __init__.py           # Flask app factory; owns the shared SpeedhiveStorage + SpeedhiveClient
│   ├── db.py                 # Read-through cache helpers, offline dump management
│   ├── tasks.py              # Background task execution + SQLite-backed task state
│   ├── notifications.py      # Resend email dispatch for track-record review
│   ├── utils.py              # Formatting/parsing helpers shared by routes
│   └── routes/
│       ├── auth.py           # Login / logout
│       ├── dashboard.py      # Home page, org/driver search
│       ├── organizations.py  # Org add, refresh (sync), cache clear, offline dump import/export
│       ├── sessions.py       # Event/session results, lap-time charts
│       ├── stats.py          # Consistency rankings, per-driver percentile breakdown
│       └── track_records.py  # Track-record sync, review/approve/reject, curated list, settings
├── templates/                 # Jinja2 templates (incl. templates/emails/)
├── static/                    # CSS/JS
├── speedhive-tools/            # Submodule: the scraping/storage/CLI engine
├── tests/                      # UI test suite
├── app.py                      # WSGI entrypoint (`app:app`)
├── Dockerfile / docker-compose.yml
└── justfile                    # install / test / lint / run
```

The app holds one shared `SpeedhiveStorage` instance (`app.storage`), built
once in `create_app()` against `SPEEDHIVE_DB_PATH`, and passes it into
`speedhive-tools` workflow functions (`refresh_org_cache`, `refresh_and_scan`,
`import_dump_to_storage`, ...) rather than re-opening the database on every
call.

## Background task model

Refreshes and track-record scans run in daemon threads, not a separate worker
process. Because Gunicorn runs multiple worker processes with isolated
memory, task progress can't live in an in-process dict — it's written to a
`background_tasks` table in the same SQLite database instead, so any worker
process can poll a task's status regardless of which process started it.

Two task types share this table: `refresh_org` (data sync) and
`track_records` (curation scan). Progress is polled from the browser via
`/refresh/status/<task_id>` and `/org/<id>/track-records/update/<task_id>`.

## Track-record curation

Speedhive announcers flag new track/class records inside session
announcements. The pipeline extracts those, normalizes classification codes
against a per-org alias map, and diffs them against a curated NDJSON file —
new or faster results land in a pending-review queue
(`/org/<id>/track-records/review`), never written to the curated list
automatically. Approving or rejecting a candidate is a manual step in the UI.
If Resend credentials are configured, new candidates trigger a review-request
email automatically after a scan completes.

Announcements can be parsed two ways, set per-org in
`/org/<id>/track-records/settings`:

- **LLM (Gemini)** — the default. Tolerates announcer phrasing beyond the one
  exact template the regex parser matches. Requires a Gemini API key, entered
  in Settings (or via the `GEMINI_API_KEY` env var as a fallback). Parses an
  org's entire announcement history in a single call, and caches results per
  announcement so repeat scans only pay for genuinely new announcements
  instead of re-parsing everything every time.
- **Regex** — the original zero-dependency parser, for orgs that don't want
  to use an LLM or haven't configured a key.

Either way, extractions the parser itself flags as unreliable (an
unrecognized/ambiguous classification, or a low-confidence LLM extraction)
are routed straight to the Rejected list rather than the review queue —
restorable from there if that call was wrong, but not presented as something
to decide on.

---

## Running locally

```bash
git submodule update --init --recursive   # pull in the speedhive-tools engine
just install                                # venv + deps + editable speedhive-tools install
just test                                   # run the test suite
just run                                    # http://localhost:8854
```

`SPEEDHIVE_UI_PASSWORD` must be set (e.g. in a `.env` file) or every route
except login will redirect to it.

## Docker

```bash
docker compose up -d --build
```

`docker-compose.yml` builds the image from the `Dockerfile`, mounts
`./web_data` for persistent SQLite/dump storage, and reads
`SPEEDHIVE_UI_PASSWORD` from the environment (put it in a gitignored `.env`
file next to `docker-compose.yml`).

## Configuration

| Variable | Description | Default |
| :--- | :--- | :--- |
| `SPEEDHIVE_UI_PASSWORD` | Password gating the whole app | *required* |
| `SPEEDHIVE_WEB_DATA_DIR` | Root dir for the SQLite cache, task history, and saved dumps | `./web_data` |
| `SPEEDHIVE_DB_PATH` | Full path to the SQLite cache file | `<SPEEDHIVE_WEB_DATA_DIR>/speedhive.db` |
| `SPEEDHIVE_PORT` | Port for `flask run` / the dev server | `8854` |
| `SPEEDHIVE_MAX_ORG_EVENTS` | Cap on events processed per org per sync | `150` |
| `SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS` | Recent events re-checked during an incremental sync | `3` |
| `FLASK_SECRET_KEY` | Session cookie signing key | *pre-configured fallback — override in production* |
| `TRACK_RECORDS_STALE_HOURS` | Cache age before a track-record scan triggers an auto-refresh | `20` |
| `SYNC_SECRET` | Shared secret required by the external `/org/<id>/track-records/notify` webhook | *optional* |
| `RESEND_API_KEY`, `NOTIFICATION_FROM_EMAIL`, `NOTIFICATION_TO_EMAILS` | Resend email credentials for track-record review notifications | *optional* |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Fallback for the LLM track-record parser if not set in Track Records Settings | *optional — `gemini-2.5-flash`* |
| `GOTIFY_URL`, `GOTIFY_APP_TOKEN` | Push notification when new track-record candidates are found | *optional* |

---

## Working on the `speedhive-tools` submodule

Library, storage, and CLI code lives in the `speedhive-tools/` submodule (its
own repo). Make changes there, run its own test suite, commit and push
inside the submodule first, then commit the updated submodule pointer here:

```bash
cd speedhive-tools
pytest
git commit -am "..." && git push

cd ..
git add speedhive-tools
git commit -m "Bump speedhive-tools submodule reference"
```
