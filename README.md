# Speedhive Tools UI

Web UI for Speedhive data powered by the `speedhive-tools` submodule.

## Run Locally

```bash
git clone --recurse-submodules https://github.com/ncrosty58/speedhive-tools-ui.git
cd speedhive-tools-ui
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Run With Docker

```bash
docker compose up -d --build
```

## Data

- The app stores local files under `web_data/`
- The core logic lives in the `speedhive-tools/` submodule
- The UI uses the core package for sync, export, and track-record workflows

## Configuration

Set these as needed in your shell or `.env` file:

- `SPEEDHIVE_WEB_DATA_DIR`
- `SPEEDHIVE_DB_PATH`
- `SPEEDHIVE_MAX_ORG_EVENTS`
- `SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS`
- `SPEEDHIVE_UI_PASSWORD`

## Development

Make code changes in the `speedhive-tools/` submodule when you are touching core logic. Commit and push the submodule first, then update the parent repo pointer.
