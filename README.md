# Speedhive Tools UI

A professional Flask-based Web UI and background scanning pipeline for Speedhive racing data, powered by the `speedhive-tools` engine submodule.

This application provides tools to scrape organizations, browse events and sessions, view interactive lap time charts with outlier filtering, calculate driver consistency statistics, and automatically run track record review workflows with email notifications.

---

## 🏗️ Repository Architecture

The project is structured as a modern Flask package separating concerns cleanly between database persistence, background worker tasks, notification templating, and route handlers.

```
├── app/                        # Main application package
│   ├── __init__.py             # Flask App Factory and application setup
│   ├── db.py                   # Data persistence, cache managers, and dump generation
│   ├── utils.py                # Text parser utilities, display formatters, and helpers
│   ├── tasks.py                # Disk-backed background worker tasks
│   ├── notifications.py        # Jinja2 template rendering and Resend email dispatcher
│   └── routes/                 # Decoupled web routes
│       ├── auth.py             # Login/logout authentication endpoints
│       ├── dashboard.py        # Dashboard main page and searches
│       ├── organizations.py    # Organization refresh, cache clean, and dump management
│       ├── sessions.py         # Event list, session results, and lap traces
│       ├── track_records.py    # Track record curation review, reject list, and sync API
│       └── stats.py            # Consistency rankings and driver percentiles
├── speedhive-tools/            # Submodule containing the scraper API & mathematical models
├── templates/                  # Jinja2 web layout files
├── static/                     # CSS stylesheets and client-side scripts
├── tests/                      # UI web testing suite
├── app.py                      # Thin application entrypoint
└── Makefile                    # Local build and test targets
```

---

## ⚡ Background Task State Model (Gunicorn-Safe)

To support multi-process web servers like Gunicorn where memory is isolated between worker processes, background task status tracking is stored on disk under the persistent `web_data/` directory:

1. **Task Persistence**: When a refresh/sync task is triggered, a task entry is created in `web_data/refresh_tasks/<task_id>.json`.
2. **Atomic Writing**: Progress updates write atomically to the JSON state files under a thread lock.
3. **Multi-process Visibility**: Workers across process boundaries can retrieve current background execution status by reading the task's JSON state file on disk rather than checking in-memory dictionaries.

---

## 🚀 Running Locally

### 1. Initialize Submodules
Ensure the core library submodule is populated:
```bash
git submodule update --init --recursive
```

### 2. Install Dependencies
Build the virtual environment, install requirements, and set up the local `speedhive-tools` package in editable mode:
```bash
make install
```

### 3. Run the Test Suite
Ensure the codebase is working cleanly:
```bash
make test
```

### 4. Start the Application
Run the local development server:
```bash
make run
```
The server will start on [http://localhost:8854](http://localhost:8854).

---

## 🐳 Docker Deployment

The application is pre-configured for Docker Compose. Start the container in detached mode:
```bash
docker compose up -d --build
```

---

## ⚙️ Configuration Variables

Configuration is handled using environment variables, which can be defined in a `.env` file at the repository root:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `SPEEDHIVE_WEB_DATA_DIR` | Directory on disk to store database, task logs, and local files. | `./web_data` |
| `SPEEDHIVE_DB_PATH` | Full file path to the SQLite cache database. | `<SPEEDHIVE_WEB_DATA_DIR>/speedhive.db` |
| `SPEEDHIVE_UI_PASSWORD` | Password required to access the application. | *Required (e.g. `test-password`)* |
| `SPEEDHIVE_PORT` | The port the web server binds to when executed directly. | `8854` |
| `SPEEDHIVE_MAX_ORG_EVENTS` | Maximum number of events to process per organization. | `150` |
| `SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS` | Number of events backfilled during incremental scans. | `3` |
| `FLASK_SECRET_KEY` | Private secret key used to secure Flask session cookies. | *Pre-configured fallback* |
| `RESEND_API_KEY` | API token for Resend to send track record review notifications. | *Optional* |
| `NOTIFICATION_FROM_EMAIL` | Sender address for review notification emails. | *Optional* |
| `NOTIFICATION_TO_EMAILS` | Comma-separated list of recipient addresses for notifications. | *Optional* |

---

## 🛠️ Submodule Development

If you make modifications to the scraper library or mathematical consistency code under the `speedhive-tools/` submodule, compile and run the local tests there first:
```bash
cd speedhive-tools
pytest
```
Remember to commit changes in the submodule directory before committing the updated submodule pointer in the parent repository.
