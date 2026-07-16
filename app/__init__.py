import json
import os
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, session, request, redirect, url_for
from speedhive.storage import SpeedhiveStorage
from speedhive.wrapper import SpeedhiveClient
from speedhive.exporters.export_db_dump import export_db_dump  # noqa: F401
from app.utils import iso_utc, utc_now

load_dotenv()

# Shared globals
client = SpeedhiveClient.create()
storage = None
UI_PASSWORD = None

app_root = Path(__file__).resolve().parent.parent
data_root = Path(
    os.environ.get("SPEEDHIVE_DATA_DIR") or
    (app_root / "data")
)
DB_PATH = Path(os.environ.get("SPEEDHIVE_DB_PATH", data_root / "speedhive.db"))
DUMPS_ROOT = data_root / "saved_dumps"





PUBLIC_ENDPOINTS = {
    "login",
    "static",
    "org_track_records_json",
    "org_track_records_status",
    "org_track_records_sync",
    "org_track_records_sync_status",
}


def require_login():
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get("authenticated"):
        return None
    next_path = request.path if request.method == "GET" else None
    return redirect(url_for("login", next=next_path))


def create_app() -> Flask:
    global storage, UI_PASSWORD
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "speedhive-tools-secret-key-34399")
    
    UI_PASSWORD = os.environ.get("SPEEDHIVE_UI_PASSWORD")
    storage = SpeedhiveStorage(DB_PATH)

    
    # Initialize the org_stats database table if not exists
    with storage.connect() as conn:
        try:
            cursor = conn.execute("PRAGMA table_info(org_stats)")
            cols = [row[1] for row in cursor.fetchall()]
            if cols and "session_type" not in cols:
                conn.execute("DROP TABLE org_stats")
                conn.commit()
        except Exception:
            pass

        conn.execute(
            "CREATE TABLE IF NOT EXISTS org_stats ("
            "org_id INTEGER, "
            "session_type TEXT, "
            "payload TEXT, "
            "calculated_at TEXT, "
            "PRIMARY KEY (org_id, session_type)"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS background_tasks ("
            "task_id TEXT PRIMARY KEY, "
            "org_id INTEGER, "
            "task_type TEXT, "
            "status TEXT, "
            "payload TEXT, "
            "started_at TEXT, "
            "finished_at TEXT"
            ")"
        )
        conn.commit()

        # Any task still 'running'/'stopping' at process startup belongs to a
        # daemon thread from a previous process (a restart, a crash) that no
        # longer exists and will never update it again -- mark it failed so
        # the UI doesn't show it as perpetually in-progress and callers can retry.
        orphaned = conn.execute(
            "SELECT task_id, payload FROM background_tasks WHERE status IN ('running', 'stopping')"
        ).fetchall()
        if orphaned:
            finished_at = iso_utc(utc_now())
            for row in orphaned:
                try:
                    payload = json.loads(row["payload"]) if row["payload"] else {}
                except Exception:
                    payload = {}
                payload["error"] = "Interrupted by server restart"
                conn.execute(
                    "UPDATE background_tasks SET status = 'error', finished_at = ?, payload = ? WHERE task_id = ?",
                    (finished_at, json.dumps(payload, ensure_ascii=False), row["task_id"]),
                )
            conn.commit()
        
    # Configure global request hook
    app.before_request(require_login)
    
    # Register routes
    from app.routes.auth import register_routes as reg_auth
    from app.routes.dashboard import register_routes as reg_dashboard
    from app.routes.organizations import register_routes as reg_org
    from app.routes.sessions import register_routes as reg_sessions
    from app.routes.track_records import register_routes as reg_track_records
    from app.routes.stats import register_routes as reg_stats
    
    reg_auth(app)
    reg_dashboard(app)
    reg_org(app)
    reg_sessions(app)
    reg_track_records(app)
    reg_stats(app)

    @app.context_processor
    def inject_global_data():
        from flask import request, session
        from datetime import datetime
        from speedhive.utils.lap_analysis import parse_time_value
        from app.db import list_stored_orgs, format_saved_at_display, store_status_label

        org_list = list_stored_orgs()

        global_org_id = request.args.get("org_id") or (request.view_args.get("org_id") if request.view_args else None)
        if not global_org_id and request.path.startswith("/org/"):
            parts = request.path.split("/")
            if len(parts) > 2 and parts[2].isdigit():
                global_org_id = parts[2]
                
        # Resolve from session_id
        if not global_org_id:
            session_id = request.view_args.get("session_id") if request.view_args else None
            if not session_id and "/session/" in request.path:
                parts = request.path.split("/")
                for p in parts:
                    if p.isdigit():
                        session_id = p
                        break
            if session_id:
                try:
                    with storage.connect() as conn:
                        row = conn.execute("SELECT org_id FROM sessions WHERE session_id = ?", (int(session_id),)).fetchone()
                        if row and row[0]:
                            global_org_id = str(row[0])
                except Exception:
                    pass

        # Resolve from event_id
        if not global_org_id:
            event_id = request.view_args.get("event_id") if request.view_args else None
            if not event_id and "/event/" in request.path:
                parts = request.path.split("/")
                for p in parts:
                    if p.isdigit():
                        event_id = p
                        break
            if event_id:
                try:
                    with storage.connect() as conn:
                        row = conn.execute("SELECT org_id FROM events WHERE event_id = ?", (int(event_id),)).fetchone()
                        if row and row[0]:
                            global_org_id = str(row[0])
                except Exception:
                    pass

        # Resolve from championship_id
        if not global_org_id:
            championship_id = request.view_args.get("championship_id") if request.view_args else None
            if not championship_id and "/championship/" in request.path:
                parts = request.path.split("/")
                for p in parts:
                    if p.isdigit():
                        championship_id = p
                        break
            if championship_id:
                try:
                    import json
                    with storage.connect() as conn:
                        cursor = conn.execute("SELECT org_id, payload FROM org_championships")
                        for org_id_val, payload_str in cursor.fetchall():
                            try:
                                champs = json.loads(payload_str)
                                if isinstance(champs, list):
                                    for ch in champs:
                                        if str(ch.get("id")) == str(championship_id):
                                            global_org_id = str(org_id_val)
                                            break
                            except Exception:
                                pass
                            if global_org_id:
                                break
                except Exception:
                    pass

        # Fall back to the org chosen at login (or last explicitly visited)
        if not global_org_id:
            global_org_id = session.get("org_id")

        if not global_org_id and request.path == "/":
            if org_list:
                global_org_id = org_list[0]["id"]

        # Keep the session's org sticky: switching orgs via the navbar (or any
        # org-scoped URL) becomes the new default for URLs without an org in them.
        if session.get("authenticated") and global_org_id:
            session["org_id"] = str(global_org_id)

        return {
            "org_list": org_list,
            "global_org_id": global_org_id,
            "authenticated": session.get("authenticated", False),
            "datetime": datetime,
            "parse_time_value": parse_time_value,
            "format_saved_at_display": format_saved_at_display,
            "store_status_label": store_status_label,
        }
    
    return app

# Instantiate the global application object for backward compatibility with tests and run scripts
app = create_app()

