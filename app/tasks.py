import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from app.utils import iso_utc, utc_now
from speedhive.utils.lap_analysis import safe_int


# Paths and Constants
APP_ROOT = Path(__file__).resolve().parent.parent
WEB_DATA_ROOT = Path(os.environ.get("SPEEDHIVE_WEB_DATA_DIR", APP_ROOT / "web_data"))
TRACK_RECORDS_ROOT = WEB_DATA_ROOT / "track_records"
MAX_ORG_EVENTS = int(os.environ.get("SPEEDHIVE_MAX_ORG_EVENTS", "150"))

_tasks_lock = threading.Lock()


def _new_task(org_id: int, mode: str) -> str:
    from app import storage
    task_id = str(uuid.uuid4())
    task = {
        "mode": mode,
        "phase": "Starting refresh...",
        "current_item": "",
        "sessions_done": 0,
        "sessions_total": 0,
        "error": None
    }
    started_at = iso_utc(utc_now())
    with _tasks_lock:
        with storage.connect() as conn:
            conn.execute(
                "INSERT INTO background_tasks (task_id, org_id, task_type, status, payload, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, org_id, "refresh_org", "running", json.dumps(task), started_at, None)
            )
            conn.commit()
    return task_id


def _fetch_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Read a task row. Caller must already hold _tasks_lock."""
    from app import storage
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT org_id, task_type, status, payload, started_at, finished_at FROM background_tasks WHERE task_id = ?",
            (task_id,)
        ).fetchone()
        if not row:
            return None
        task = {
            "task_id": task_id,
            "org_id": row["org_id"],
            "task_type": row["task_type"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
        if row["payload"]:
            try:
                task.update(json.loads(row["payload"]))
            except Exception:
                pass
        return task


def _get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _tasks_lock:
        return _fetch_task(task_id)


def _update_task(task_id: str, **kwargs) -> None:
    from app import storage
    with _tasks_lock:
        task = _fetch_task(task_id)
        if not task:
            return
        
        org_id = kwargs.pop("org_id", task.get("org_id"))
        task_type = kwargs.pop("task_type", task.get("task_type", "refresh_org"))
        status = kwargs.pop("status", task.get("status"))
        started_at = kwargs.pop("started_at", task.get("started_at"))
        finished_at = kwargs.pop("finished_at", task.get("finished_at"))
        
        # Remove primary metadata from kwargs payload
        kwargs.pop("task_id", None)
        
        payload_data = {}
        # Merge existing payload data keys
        keys_to_merge = (
            "mode", "phase", "current_item", "sessions_done", "sessions_total",
            "error", "result", "summary", "events_done", "events_total"
        )
        for k in keys_to_merge:
            if k in task:
                payload_data[k] = task[k]
        payload_data.update(kwargs)
        
        with storage.connect() as conn:
            conn.execute(
                "UPDATE background_tasks SET org_id = ?, task_type = ?, status = ?, payload = ?, started_at = ?, finished_at = ? WHERE task_id = ?",
                (org_id, task_type, status, json.dumps(payload_data, ensure_ascii=False), started_at, finished_at, task_id)
            )
            conn.commit()


def _is_stop_requested(task_id: str) -> bool:
    task = _get_task(task_id)
    return task is not None and task.get("status") == "stopping"


def _get_running_task_for_org(org_id: int) -> Optional[Dict[str, Any]]:
    from app import storage
    with _tasks_lock:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT task_id, status, payload, started_at, finished_at FROM background_tasks "
                "WHERE org_id = ? AND task_type = 'refresh_org' AND status IN ('running', 'stopping') "
                "LIMIT 1",
                (org_id,)
            ).fetchone()
            if not row:
                return None
            task = {
                "task_id": row["task_id"],
                "org_id": org_id,
                "task_type": "refresh_org",
                "status": row["status"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
            if row["payload"]:
                try:
                    task.update(json.loads(row["payload"]))
                except Exception:
                    pass
            return task


def _new_track_records_task(org_id: int) -> str:
    from app import storage
    task_id = str(uuid.uuid4())
    task = {
        "phase": "Starting",
        "error": None,
        "result": None,
    }
    started_at = iso_utc(utc_now())
    with _tasks_lock:
        with storage.connect() as conn:
            conn.execute(
                "INSERT INTO background_tasks (task_id, org_id, task_type, status, payload, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, org_id, "track_records", "running", json.dumps(task), started_at, None)
            )
            conn.commit()
    return task_id


def _get_track_records_task(org_id: int, task_id: str) -> Optional[Dict[str, Any]]:
    return _get_task(task_id)


def _update_track_records_task(org_id: int, task_id: str, **kwargs) -> None:
    _update_task(task_id, **kwargs)


def _get_running_track_records_task_for_org(org_id: int) -> Optional[Dict[str, Any]]:
    from app import storage
    with _tasks_lock:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT task_id, status, payload, started_at, finished_at FROM background_tasks "
                "WHERE org_id = ? AND task_type = 'track_records' AND status = 'running' "
                "LIMIT 1",
                (org_id,)
            ).fetchone()
            if not row:
                return None
            task = {
                "task_id": row["task_id"],
                "org_id": org_id,
                "task_type": "track_records",
                "status": row["status"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
            if row["payload"]:
                try:
                    task.update(json.loads(row["payload"]))
                except Exception:
                    pass
            return task


def _get_bulk_parser_for_org(org_id: int):
    """Return the bulk announcement parser configured for this org's scans.

    Regex is the default for every org -- LLM (Gemini) is opt-in per org via
    'parsing.engine': 'llm' in that org's own config.json (Track Records
    Settings). When LLM is active, all of the org's announcements are parsed
    in a single call rather than one call per announcement --
    storage.get_track_records() falls back to the regex parser (one call per
    text, but nearly instant) when this returns None.
    """
    from speedhive.workflows.track_records import curation as track_records
    from app.utils import read_json_file

    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id)
    config = read_json_file(p["dir"] / "config.json") or {}
    engine = (config.get("parsing") or {}).get("engine")
    if engine != "llm":
        return None
    from speedhive.llm import parse_track_records_bulk_with_gemini
    return parse_track_records_bulk_with_gemini


def _run_track_records_sync_task(task_id: str, org_id: int, full: bool, force: bool) -> None:
    from app import client, storage
    from app.notifications import _auto_notify_for_org
    from speedhive.workflows.track_records import curation as track_records

    def report(phase):
        _update_track_records_task(org_id, task_id, phase=phase)

    try:
        outcome = track_records.refresh_and_scan(
            org_id,
            client,
            storage,
            TRACK_RECORDS_ROOT,
            mode="full" if full else "incremental",
            force=force,
            max_events=MAX_ORG_EVENTS,
            recent_backfill_events=20,
            cleanup_on_full=True,
            progress_cb=report,
            bulk_parser=_get_bulk_parser_for_org(org_id),
        )
        scan_result = outcome["scan"]
        _update_track_records_task(org_id, task_id, status="done", finished_at=iso_utc(utc_now()), result=scan_result)

        # Automatically check and trigger notification emails upon successful scan completion
        if scan_result.get("candidates_found", 0) > 0:
            _auto_notify_for_org(org_id)

    except Exception as exc:
        _update_track_records_task(org_id, task_id, status="error", finished_at=iso_utc(utc_now()), error=str(exc))


def _trigger_track_records_rescan(org_id: int) -> None:
    """Fire-and-forget local rescan, used to keep the Review Queue self-consistent
    after anything that changes the curated list or the synced cache (a Speedhive
    sync completing, curated add/delete/import/approve). No-op if a track-records
    task is already running for this org.
    """
    if _get_running_track_records_task_for_org(org_id):
        return
    task_id = _new_track_records_task(org_id)
    t = threading.Thread(target=_run_track_records_scan_only_task, args=(task_id, org_id), daemon=True)
    t.start()


def _run_track_records_scan_only_task(task_id: str, org_id: int) -> None:
    """Diff the already-synced cache against the curated list. Never contacts
    Speedhive -- callers are responsible for syncing first (see _run_refresh_task).
    """
    from app import storage
    from app.notifications import _auto_notify_for_org
    from speedhive.workflows.track_records import curation as track_records

    def report(phase):
        _update_track_records_task(org_id, task_id, phase=phase)

    try:
        scan_result = track_records.run_sync_and_diff(
            org_id, storage, TRACK_RECORDS_ROOT, progress_cb=report, bulk_parser=_get_bulk_parser_for_org(org_id)
        )
        _update_track_records_task(org_id, task_id, status="done", finished_at=iso_utc(utc_now()), result=scan_result)

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
        _trigger_track_records_rescan(org_id)
    except Exception as exc:
        _update_task(
            task_id,
            status="error",
            phase="Error",
            current_item=str(exc),
            finished_at=iso_utc(utc_now()),
            error=str(exc),
        )
