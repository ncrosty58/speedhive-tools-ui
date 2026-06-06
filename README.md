# Speedhive Tools UI

A feature-rich, modern web interface and dashboard for scraping, caching, storing, analyzing, and exporting motorsport race timing data from Mylaps Speedhive. 

Built using **Flask**, **SQLite**, and styled with a custom responsive **dark-mode glassmorphic interface**, it is powered by the [`speedhive-tools`](https://github.com/ncrosty58/speedhive-tools) core libraries included as a Git submodule.

---

## 🌟 Key Features

- **Dashboard & Search**: Seamlessly search for racing organizations (tracks/clubs) and drivers.
- **Detailed Event & Session Views**: Inspect championships, qualifying sessions, races, and practice runs with full driver listings.
- **Background Sync Engine**: Perform full or incremental data syncs from Speedhive to a local SQLite database in the background, with real-time task progress monitoring (percentage complete, active session count) and the ability to stop running syncs at any time.
- **Advanced Consistency & Stats**:
  - Analyze driver consistency (averages, best times, Coefficient of Variation).
  - Identify the most and least consistent drivers in a class.
  - Render interactive lap charts and sector analysis.
- **Data Export & Portability**:
  - Export speed/lap records as JSON/NDJSON.
  - Download entire organization datasets as offline zip files containing raw SQLite databases and NDJSON dumps.
- **Docker-Ready**: Containerized deployment setup using `Dockerfile` and `docker-compose.yml`.

---

## 📁 Repository Structure

- [**`app.py`**](file:///opt/speedhive_data/app.py): The main Flask application containing routes, background sync task registries, data export logic, and page controllers.
- [**`templates/`**](file:///opt/speedhive_data/templates/): Jinja2 HTML templates styled with a premium custom dark-theme glassmorphism design:
  - `base.html`: Main layout, navigation, and custom CSS styling.
  - `org_operations.html`: Control center for running, stopping, and monitoring background sync tasks.
  - `org_stats.html`: Statistics, lap analysis, and driver consistency leaderboard.
  - `event.html`, `results.html`: Session classifications, event views, and lap tables.
- [**`speedhive-tools/`**](file:///opt/speedhive_data/speedhive-tools/): Git submodule referencing the core Python library for Mylaps Speedhive API scraping, database storage, and lap-processing algorithms.
- `speedhive_tools.py`: A legacy helper module containing standard REST-based wrappers for the Mylaps Speedhive JSON API.
- `requirements.txt`: Python package dependencies for Flask, HTTPX, and general requirements.
- `Dockerfile` / `docker-compose.yml`: Ready-to-use deployment manifests.

---

## 🚀 Getting Started

### Prerequisites
- Python 3.12+
- Git
- Docker & Docker Compose (optional, for containerized run)

### Option 1: Running Locally (Directly with Python)

1. **Clone the Repository & Fetch Submodules**
   Ensure the core `speedhive-tools` library is loaded:
   ```bash
   git clone --recurse-submodules https://github.com/ncrosty58/speedhive-tools-ui.git
   cd speedhive-tools-ui
   ```
   *If you already cloned without submodules, run:*
   ```bash
   git submodule update --init --recursive
   ```

2. **Set up a Virtual Environment & Install Dependencies**
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Run the Application**
   ```bash
   python -m Flask run --host=0.0.0.0 --port=8854
   ```
   Open your browser and navigate to `http://localhost:8854`.

### Option 2: Running with Docker Compose (Recommended)

1. **Build & Spin Up the Service**
   ```bash
   docker compose up -d --build
   ```

2. **Access the UI**
   Navigate to `http://localhost:8854`.

3. **Persistent Volumes**
   The application stores local data under `/app/web_data`. This is mapped to the `./web_data` directory in your project root via Docker volumes to ensure scraped databases and exported files survive container restarts.

---

## ⚙️ Configuration

Environment variables can be adjusted in `docker-compose.yml` or your local shell:

| Environment Variable | Description | Default |
|---|---|---|
| `SPEEDHIVE_WEB_DATA_DIR` | Root directory for storing SQLite databases, exports, and background sync task results. | `./web_data` |
| `SPEEDHIVE_DB_PATH` | Path to the main SQLite database. | `<SPEEDHIVE_WEB_DATA_DIR>/speedhive.db` |
| `SPEEDHIVE_MAX_ORG_EVENTS` | Max events count limit to retrieve during a sync. | `150` |
| `SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS` | Number of events to fetch for an incremental backfill. | `3` |

---

## 👨‍💻 Contributing & Development

To work on the core scraping or data-processing logic, changes should be made within the `speedhive-tools` submodule. When committing changes:
1. Make changes inside `speedhive-tools/`.
2. Commit and push inside the `speedhive-tools/` repository first.
3. Reference the updated commit inside the parent `speedhive-tools-ui` repository.

## 📄 License
This project is licensed under the [MIT License](file:///opt/speedhive_data/speedhive-tools/LICENSE).
