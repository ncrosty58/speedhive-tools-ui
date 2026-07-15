import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from app.utils import iso_utc, utc_now
from speedhive.analysis.lap_analysis import safe_int


# Paths and Constants
APP_ROOT = Path(__file__).resolve().parent.parent
WEB_DATA_ROOT = Path(os.environ.get("SPEEDHIVE_WEB_DATA_DIR", APP_ROOT / "web_data"))
REFRESH_TASKS_DIR = WEB_DATA_ROOT / "refresh_tasks"
TRACK_RECORDS_ROOT = WEB_DATA_ROOT / "track_records"
MAX_ORG_EVENTS = int(os.environ.get("SPEEDHIVE_MAX_ORG_EVENTS", "150"))

_tasks_lock = threading.Lock()
_track_records_task_write_lock = threading.Lock()


def _get_task_path(task_id: str) -> Path:
    return REFRESH_TASKS_DIR / f"{task_id}.json"


def _new_task(org_id: int, mode: str) -> str:
    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "org_id": org_id,
        "mode": mode,
        "status": "running",
        "phase": "Starting refresh...",
        "current_item": "",
        "sessions_done": 0,
        "sessions_total": 0,
        "started_at": iso_utc(utc_now()),
        "finished_at": None,
        "error": None
    }
    _update_task(task_id, **task)
    return task_id


def _get_task(task_id: str) -> Optional[Dict[str, Any]]:
    path = _get_task_path(task_id)
    if not path.exists():
        return None
    with _tasks_lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


def _update_task(task_id: str, **kwargs) -> None:
    path = _get_task_path(task_id)
    with _tasks_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        task = {}
        if path.exists():
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        task.update(kwargs)
        try:
            path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _is_stop_requested(task_id: str) -> bool:
    task = _get_task(task_id)
    return task is not None and task.get("status") == "stopping"


def _get_running_task_for_org(org_id: int) -> Optional[Dict[str, Any]]:
    if not REFRESH_TASKS_DIR.exists():
        return None
    with _tasks_lock:
        for path in REFRESH_TASKS_DIR.glob("*.json"):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
                if task.get("org_id") == org_id and task.get("status") in ("running", "stopping"):
                    return task
            except Exception:
                continue
    return None


def _track_records_task_path(org_id: int, task_id: str) -> Path:
    from speedhive.workflows.track_records import curation as track_records
    return track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id)["tasks"] / f"{task_id}.json"


def _new_track_records_task(org_id: int) -> str:
    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "org_id": org_id,
        "status": "running",  # running | done | error
        "phase": "Starting",
        "started_at": iso_utc(utc_now()),
        "finished_at": None,
        "error": None,
        "result": None,
    }
    path = _track_records_task_path(org_id, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(task, f, indent=2)
    return task_id


def _get_track_records_task(org_id: int, task_id: str) -> Optional[Dict[str, Any]]:
    path = _track_records_task_path(org_id, task_id)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _update_track_records_task(org_id: int, task_id: str, **kwargs) -> None:
    with _track_records_task_write_lock:
        path = _track_records_task_path(org_id, task_id)
        if not path.exists():
            return
        with open(path) as f:
            task = json.load(f)
        task.update(kwargs)
        with open(path, "w") as f:
            json.dump(task, f, indent=2)


def _get_running_track_records_task_for_org(org_id: int) -> Optional[Dict[str, Any]]:
    from speedhive.workflows.track_records import curation as track_records
    tasks_dir = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id)["tasks"]
    if not tasks_dir.exists():
        return None
    for task_file in tasks_dir.glob("*.json"):
        try:
            with open(task_file) as f:
                task = json.load(f)
        except Exception:
            continue
        if task.get("status") == "running":
            return task
    return None


def _run_track_records_sync_task(task_id: str, org_id: int, full: bool, force: bool) -> None:
    from app import client
    from app.notifications import _auto_notify_for_org
    from speedhive.workflows.track_records import curation as track_records
    
    def report(phase):
        _update_track_records_task(org_id, task_id, phase=phase)

    try:
        from app import DB_PATH
        outcome = track_records.refresh_and_scan(
            org_id,
            client,
            DB_PATH,
            TRACK_RECORDS_ROOT,
            mode="full" if full else "incremental",
            force=force,
            max_events=MAX_ORG_EVENTS,
            recent_backfill_events=20,
            cleanup_on_full=True,
            progress_cb=report,
        )
        scan_result = outcome["scan"]
        _update_track_records_task(org_id, task_id, status="done", finished_at=iso_utc(utc_now()), result=scan_result)

        # Automatically check and trigger notification emails upon successful scan completion
        if scan_result.get("candidates_found", 0) > 0:
            _auto_notify_for_org(org_id)

    except Exception as exc:
        _update_track_records_task(org_id, task_id, status="error", finished_at=iso_utc(utc_now()), error=str(exc))


def _run_refresh_task(task_id: str, org_id: int, mode: str, backfill_events: int) -> None:
    """Run the org refresh in a background thread with progress updates."""
    from app import client, storage
    from speedhive.workflows.refresh_org_cache import (
        _event_ids_from_rows, _sorted_event_ids_for_backfill,
        _parse_iso_utc,
    )

    try:
        _update_task(task_id, phase="Fetching org metadata")
        previous_state = storage.get_refresh_state(org_id).payload
        if not isinstance(previous_state, dict):
            previous_state = {}

        previous_events_record = storage.get_events(org_id).payload
        previous_event_ids = {
            safe_int(event.get("id"), None)
            for event in (previous_events_record or [])
            if isinstance(event, dict) and safe_int(event.get("id"), None) is not None
        }
        previous_session_ids = {
            safe_int(session_id, None)
            for session_id in storage.load_session_payloads(org_id).keys()
            if safe_int(session_id, None) is not None
        }
        prev_full_at = previous_state.get("last_full_refresh_at")
        prev_incremental_at = previous_state.get("last_incremental_refresh_at")

        if _is_stop_requested(task_id):
            _update_task(task_id, status="stopped", phase="Stopped", finished_at=iso_utc(utc_now()))
            return

        _update_task(task_id, phase="Fetching organization", current_item="organization.json")
        organization = client.get_organization(org_id) or {"id": org_id, "name": f"Organization #{org_id}"}

        _update_task(task_id, phase="Fetching championships", current_item="championships.json")
        championships = client.get_championships(org_id) or []

        _update_task(task_id, phase="Fetching event list", current_item="events.json")
        events = list(client.iter_events(org_id))
        if MAX_ORG_EVENTS is not None:
            events = events[:max(0, int(MAX_ORG_EVENTS))]

        refresh_saved_at = iso_utc(utc_now())
        with storage.connect() as storage_conn:
            storage.save_organization(org_id, organization, saved_at=refresh_saved_at, conn=storage_conn)
            storage.save_championships(org_id, championships, saved_at=refresh_saved_at, conn=storage_conn)
            storage.save_events(org_id, events, saved_at=refresh_saved_at, conn=storage_conn)

        current_event_ids = _event_ids_from_rows(events)
        current_event_id_set = set(current_event_ids)
        new_event_ids = sorted(current_event_id_set - previous_event_ids)

        from speedhive.workflows.refresh_org_cache import _safe_int
        refresh_event_ids: set
        if mode == "full":
            refresh_event_ids = set(current_event_id_set)
        else:
            refresh_event_ids = set(new_event_ids)
            refresh_event_ids.update(_sorted_event_ids_for_backfill(events, backfill_events))

        events_to_refresh = [e for e in events if isinstance(e, dict) and _safe_int(e.get("id")) in refresh_event_ids]
        _update_task(
            task_id,
            phase="Importing events",
            events_total=len(events_to_refresh),
            events_done=0,
            current_item="",
        )

        refreshed_events = 0
        refreshed_sessions = 0
        known_session_ids: set = set() if mode == "full" else set(previous_session_ids)

        for event in events:
            if _is_stop_requested(task_id):
                _update_task(task_id, status="stopped", phase="Stopped", finished_at=iso_utc(utc_now()))
                return

            if not isinstance(event, dict):
                continue
            event_id = _safe_int(event.get("id"))
            if event_id is None or event_id not in refresh_event_ids:
                continue

            event_name = event.get("name") or f"Event #{event_id}"
            _update_task(
                task_id,
                phase="Importing events",
                current_item=f"Event: {event_name}",
            )

            event_detail = client.get_event(event_id, include_sessions=True) or {}
            sessions = client.get_sessions(event_id) or []
            with storage.connect() as storage_conn:
                storage.save_event(event_id, org_id, event_detail, saved_at=refresh_saved_at, conn=storage_conn)
                storage.save_event_sessions(event_id, org_id, sessions, saved_at=refresh_saved_at, conn=storage_conn)
            refreshed_events += 1

            session_list = [s for s in sessions if isinstance(s, dict) and _safe_int(s.get("id")) is not None]
            _update_task(
                task_id,
                events_done=refreshed_events,
                sessions_total=len(session_list),
                sessions_done=0,
            )

            event_sessions_done = 0
            for session in session_list:
                if _is_stop_requested(task_id):
                    _update_task(task_id, status="stopped", phase="Stopped", finished_at=iso_utc(utc_now()))
                    return

                sid = _safe_int(session.get("id"))
                sname = session.get("name") or f"Session #{sid}"
                _update_task(
                    task_id,
                    phase="Importing sessions",
                    current_item=f"{event_name} → {sname}",
                )

                session_detail = client.get_session(sid) or {}
                results = client.get_results(sid) or []
                laps = client.get_laps(sid) or []
                announcements = client.get_announcements(sid) or []
                lap_chart = client.get_lap_chart(sid) or []

                with storage.connect() as storage_conn:
                    storage.save_session(sid, event_id, org_id, session_detail, saved_at=refresh_saved_at, conn=storage_conn)
                    storage.save_results(sid, event_id, org_id, results, saved_at=refresh_saved_at, conn=storage_conn)
                    storage.save_laps(sid, event_id, org_id, laps, saved_at=refresh_saved_at, conn=storage_conn)
                    storage.save_announcements(sid, event_id, org_id, announcements, saved_at=refresh_saved_at, conn=storage_conn)
                    storage.save_lap_chart(sid, event_id, org_id, lap_chart, saved_at=refresh_saved_at, conn=storage_conn)

                known_session_ids.add(sid)
                refreshed_sessions += 1
                event_sessions_done += 1
                _update_task(task_id, sessions_done=event_sessions_done)

        # Cleanup stale dirs on full refresh
        removed_event_dirs = 0
        removed_session_dirs = 0
        if mode == "full":
            _update_task(task_id, phase="Cleaning up old cache", current_item="")
            storage_removed_events, storage_removed_sessions = storage.prune_org(
                org_id,
                current_event_id_set,
                known_session_ids,
            )
            removed_event_dirs += storage_removed_events
            removed_session_dirs += storage_removed_sessions

        refreshed_at = iso_utc(utc_now())
        full_at = refreshed_at if mode == "full" else prev_full_at
        incremental_at = refreshed_at if mode == "incremental" else prev_incremental_at
        refresh_dt_candidates = [dt for dt in (_parse_iso_utc(full_at), _parse_iso_utc(incremental_at)) if dt]
        last_refresh_at = (
            max(refresh_dt_candidates).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if refresh_dt_candidates
            else refreshed_at
        )

        refresh_state = {
            "org_id": org_id,
            "last_refresh_at": last_refresh_at,
            "last_refresh_mode": mode,
            "last_full_refresh_at": full_at,
            "last_incremental_refresh_at": incremental_at,
            "events_cached": len(current_event_id_set),
            "sessions_cached": len(known_session_ids),
            "championships_cached": len(championships),
            "cached_event_ids": sorted(current_event_id_set),
            "cached_session_ids": sorted(known_session_ids),
            "new_events_detected": len(new_event_ids),
            "backfill_events_requested": int(max(0, backfill_events)),
            "refreshed_events": refreshed_events,
            "refreshed_sessions": refreshed_sessions,
            "removed_event_dirs": removed_event_dirs,
            "removed_session_dirs": removed_session_dirs,
        }
        with storage.connect() as storage_conn:
            storage.save_refresh_state(org_id, refresh_state, saved_at=refresh_saved_at, conn=storage_conn)

        _update_task(
            task_id,
            status="done",
            phase="Complete",
            current_item="",
            finished_at=iso_utc(utc_now()),
            summary=refresh_state,
        )
    except Exception as exc:
        _update_task(
            task_id,
            status="error",
            phase="Error",
            current_item=str(exc),
            finished_at=iso_utc(utc_now()),
            error=str(exc),
        )
