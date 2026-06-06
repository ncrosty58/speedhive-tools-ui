"""Web app for Speedhive data with HTML frontend using speedhive-tools."""
import json
import os
import re
import statistics
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, Response, after_this_request, jsonify, redirect, render_template, request, send_file, url_for

# Insert local speedhive-tools src folder to sys.path so we can import speedhive directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "speedhive-tools", "src"))

from speedhive.wrapper import SpeedhiveClient
from speedhive.exporters.export_org_cache import refresh_org_cache as refresh_org_cache_bundle
from speedhive.processing.process_lap_analysis import (
    extract_iso_date,
    parse_time_value,
    parse_track_record_text,
    format_seconds,
    first_non_empty,
    compute_lap_statistics,
    build_lap_chart_from_laps,
    normalize_search_text,
    name_match_score,
)

app = Flask(__name__)

# Initialize the Speedhive client
client = SpeedhiveClient.create()

APP_ROOT = Path(__file__).resolve().parent
WEB_DATA_ROOT = Path(os.environ.get("SPEEDHIVE_WEB_DATA_DIR", APP_ROOT / "web_data"))
CACHE_ROOT = WEB_DATA_ROOT / "cache"
DUMPS_ROOT = WEB_DATA_ROOT / "saved_dumps"
MAX_ORG_EVENTS = int(os.environ.get("SPEEDHIVE_MAX_ORG_EVENTS", "150"))
DEFAULT_INCREMENTAL_BACKFILL_EVENTS = int(os.environ.get("SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS", "3"))

CACHE_ROOT.mkdir(parents=True, exist_ok=True)
DUMPS_ROOT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Background refresh task registry
# ---------------------------------------------------------------------------
_refresh_tasks: Dict[str, Dict[str, Any]] = {}  # task_id -> task state
_refresh_tasks_lock = threading.Lock()


def _new_task(org_id: int, mode: str) -> str:
    task_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with _refresh_tasks_lock:
        _refresh_tasks[task_id] = {
            "task_id": task_id,
            "org_id": org_id,
            "mode": mode,
            "status": "running",   # running | done | stopped | error
            "phase": "Starting",
            "current_item": "",
            "events_total": 0,
            "events_done": 0,
            "sessions_total": 0,
            "sessions_done": 0,
            "stop_requested": False,
            "started_at": now_iso,
            "finished_at": None,
            "error": None,
            "summary": None,
        }
    return task_id


def _get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _refresh_tasks_lock:
        return dict(_refresh_tasks.get(task_id, {})) if task_id in _refresh_tasks else None


def _update_task(task_id: str, **kwargs) -> None:
    with _refresh_tasks_lock:
        if task_id in _refresh_tasks:
            _refresh_tasks[task_id].update(kwargs)


def _is_stop_requested(task_id: str) -> bool:
    with _refresh_tasks_lock:
        task = _refresh_tasks.get(task_id)
        return bool(task and task.get("stop_requested"))


def _run_refresh_task(task_id: str, org_id: int, mode: str, backfill_events: int) -> None:
    """Run the org refresh in a background thread with progress updates."""
    # Inline version of refresh_org_cache that reports progress and supports stop.
    import json as _json
    import shutil as _shutil
    from speedhive.exporters.export_org_cache import (
        _utc_now_iso, _write_cache_entry, _write_json, _read_json,
        _load_previous_event_ids, _load_previous_session_ids,
        _event_ids_from_rows, _sorted_event_ids_for_backfill,
        _cleanup_numeric_child_dirs, _parse_iso_utc,
    )

    try:
        _update_task(task_id, phase="Fetching org metadata")
        cache_root = CACHE_ROOT
        org_cache_dir = cache_root / "orgs" / str(org_id)
        events_root = cache_root / "events"
        sessions_root = cache_root / "sessions"

        previous_state = _read_json(org_cache_dir / "refresh_state.json")
        if not isinstance(previous_state, dict):
            previous_state = {}

        previous_event_ids = _load_previous_event_ids(org_cache_dir)
        previous_session_ids = _load_previous_session_ids(org_cache_dir)
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

        _write_cache_entry(org_cache_dir / "organization.json", organization)
        _write_cache_entry(org_cache_dir / "championships.json", championships)
        _write_cache_entry(org_cache_dir / "events.json", events)

        current_event_ids = _event_ids_from_rows(events)
        current_event_id_set = set(current_event_ids)
        new_event_ids = sorted(current_event_id_set - previous_event_ids)

        from speedhive.exporters.export_org_cache import _safe_int
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
            _write_cache_entry(events_root / str(event_id) / "event.json", event_detail)
            _write_cache_entry(events_root / str(event_id) / "sessions.json", sessions)
            refreshed_events += 1

            session_list = [s for s in sessions if isinstance(s, dict) and _safe_int(s.get("id")) is not None]
            _update_task(
                task_id,
                events_done=refreshed_events,
                sessions_total=_get_task(task_id).get("sessions_total", 0) + len(session_list),
            )

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

                session_dir = sessions_root / str(sid)
                _write_cache_entry(session_dir / "session.json", session_detail)
                _write_cache_entry(session_dir / "results.json", results)
                _write_cache_entry(session_dir / "laps.json", laps)
                _write_cache_entry(session_dir / "announcements.json", announcements)
                _write_cache_entry(session_dir / "lap_chart.json", lap_chart)

                known_session_ids.add(sid)
                refreshed_sessions += 1
                _update_task(task_id, sessions_done=refreshed_sessions)

        # Cleanup stale dirs on full refresh
        removed_event_dirs = 0
        removed_session_dirs = 0
        if mode == "full":
            _update_task(task_id, phase="Cleaning up old cache", current_item="")
            removed_event_dirs = _cleanup_numeric_child_dirs(events_root, current_event_id_set)
            removed_session_dirs = _cleanup_numeric_child_dirs(sessions_root, known_session_ids)

        refreshed_at = _utc_now_iso()
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
        _write_json(org_cache_dir / "refresh_state.json", refresh_state)

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



def parse_date_to_comparison(dt_str):
    """Parse common date strings for filtering comparisons."""
    if not dt_str:
        return None
    try:
        # standard ISO format: 2026-06-06T12:00:00Z or similar
        # slice first 10 characters for YYYY-MM-DD
        return datetime.strptime(dt_str[:10], "%Y-%m-%d").date()
    except Exception:
        return None





def _country_name_from_value(value: Any) -> Optional[str]:
    """Normalize country-like values to a readable name."""
    if value is None:
        return None
    if isinstance(value, dict):
        return first_non_empty(
            value.get("name"),
            value.get("fullName"),
            value.get("alpha2"),
            value.get("alpha3"),
            value.get("code"),
        )
    text = str(value).strip()
    return text or None

def _org_display_name_from_cache(org_id: int) -> str:
    """Return org name from cache only (no API call), with stable fallback."""
    org_path = cache_entry_path("orgs", str(org_id), "organization.json")
    payload, _ = read_cache_entry(org_path)
    if isinstance(payload, dict):
        name = first_non_empty(payload.get("name"), payload.get("organizationName"))
        if name:
            return str(name)
    return f"Organization #{org_id}"

def extract_event_datetime(raw: Dict[str, Any]) -> Optional[str]:
    """Extract event/session datetime using common API keys."""
    if not isinstance(raw, dict):
        return None
    for key in (
        "startTime",
        "startDate",
        "startDateTime",
        "scheduledStart",
        "date",
        "start",
        "eventDate",
        "event_date",
    ):
        value = raw.get(key)
        if value:
            return str(value)
    return extract_iso_date(raw)

def format_datetime_display(value: Any, include_time: bool = True) -> Optional[str]:
    """Format API datetime-like values into readable strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            dt = datetime.utcfromtimestamp(float(value))
            return dt.strftime("%Y-%m-%d %H:%M") if include_time else dt.strftime("%Y-%m-%d")
        except Exception:
            return str(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M") if include_time else dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return text

def format_gap_display(gap: Any) -> Optional[str]:
    """Format a gap/difference object from classification responses."""
    if not isinstance(gap, dict):
        return None
    laps_behind = gap.get("lapsBehind")
    time_diff = first_non_empty(gap.get("timeDifference"), gap.get("difference"))
    if laps_behind not in (None, "", 0):
        if time_diff and str(time_diff) not in ("00.000", "0", "00:00.000"):
            return f"+{laps_behind} lap(s), {time_diff}"
        return f"+{laps_behind} lap(s)"
    if not time_diff:
        return None
    if str(time_diff) in ("00.000", "0", "00:00.000"):
        return "Leader"
    return f"+{time_diff}" if not str(time_diff).startswith("+") else str(time_diff)

def safe_int(value: Any, default: int = 9999) -> int:
    """Best-effort integer conversion for sorting."""
    try:
        return int(value)
    except Exception:
        return default

def normalize_result_row(
    row: Dict[str, Any],
    available_comp_ids: Optional[set] = None,
    available_start_numbers: Optional[set] = None,
) -> Dict[str, Any]:
    """Normalize result row keys across payload variants for template rendering."""
    competitor = row.get("competitor") if isinstance(row.get("competitor"), dict) else {}
    driver = row.get("driver") if isinstance(row.get("driver"), dict) else {}
    driver_name = first_non_empty(
        row.get("name"),
        row.get("driverName"),
        competitor.get("name"),
        driver.get("name"),
        row.get("participantName"),
    ) or "Unknown Competitor"

    car_name = first_non_empty(
        row.get("car"),
        row.get("vehicle"),
        row.get("marque"),
        competitor.get("car"),
    )
    class_name = first_non_empty(
        row.get("resultClass"),
        row.get("vehicleClass"),
        row.get("classification"),
        row.get("class"),
        competitor.get("class"),
    )
    if car_name and class_name:
        car_class = f"{car_name} / {class_name}"
    else:
        car_class = car_name or class_name or "N/A"

    total_time = first_non_empty(
        row.get("totalTime"),
        row.get("total_time"),
        row.get("time"),
        row.get("elapsedTime"),
        row.get("finishTime"),
    )
    if not total_time:
        total_time = format_gap_display(row.get("difference")) or format_gap_display(row.get("gap")) or "N/A"

    best_lap = first_non_empty(
        row.get("bestLapTime"),
        row.get("best_lap_time"),
        row.get("bestTime"),
        row.get("lapTime"),
    ) or "N/A"

    laps = first_non_empty(
        row.get("laps"),
        row.get("lapCount"),
        row.get("lap_count"),
        row.get("completedLaps"),
        row.get("numberOfLaps"),
    )
    laps_display = laps if laps is not None else "N/A"

    position = first_non_empty(row.get("position"), row.get("pos"))
    competitor_id = first_non_empty(row.get("competitorId"), row.get("id"), competitor.get("id"))
    start_number = first_non_empty(row.get("startNumber"), row.get("transponder"))

    lap_driver_id = None
    action_label = None
    comp_id_ok = competitor_id not in (None, "") and (
        available_comp_ids is None or str(competitor_id) in available_comp_ids
    )
    start_no_ok = start_number not in (None, "") and (
        available_start_numbers is None or str(start_number) in available_start_numbers
    )

    if comp_id_ok:
        lap_driver_id = f"cid:{competitor_id}"
        action_label = "View Laps"
    elif start_no_ok:
        lap_driver_id = f"sn:{start_number}"
        action_label = "View Laps"
    elif position not in (None, ""):
        lap_driver_id = f"pos:{position}"
        action_label = "View Position Trace"

    return {
        "position": position if position is not None else "N/A",
        "position_sort": safe_int(position),
        "driver_name": driver_name,
        "car_class": car_class,
        "total_time_display": total_time,
        "best_lap_display": best_lap,
        "laps_display": laps_display,
        "lap_driver_id": lap_driver_id,
        "action_label": action_label,
    }



def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def cache_entry_path(*parts: str) -> Path:
    return CACHE_ROOT.joinpath(*parts)

def read_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

def read_cache_entry(path: Path) -> tuple[Any, Optional[datetime]]:
    payload = read_json_file(path)
    if not isinstance(payload, dict):
        return payload, None
    if "data" not in payload:
        return payload, None
    saved_at = parse_iso_utc(payload.get("saved_at"))
    return payload.get("data"), saved_at

def write_cache_entry(path: Path, data: Any) -> None:
    write_json_file(path, {"saved_at": iso_utc(utc_now()), "data": data})

def cache_age_seconds(saved_at: Optional[datetime]) -> Optional[float]:
    if not saved_at:
        return None
    return max((utc_now() - saved_at).total_seconds(), 0.0)

def cache_meta(saved_at: Optional[datetime], source: str, error: Optional[str] = None) -> Dict[str, Any]:
    age = cache_age_seconds(saved_at)
    return {
        "saved_at": iso_utc(saved_at) if saved_at else None,
        "age_seconds": age,
        "age_hours": (age / 3600.0) if age is not None else None,
        "source": source,
        "stale": saved_at is None,
        "error": error,
    }

def cache_fetch(path: Path, fetcher, force_refresh: bool = False) -> tuple[Any, Dict[str, Any]]:
    cached, saved_at = read_cache_entry(path)
    if cached is not None and not force_refresh:
        return cached, cache_meta(saved_at, source="cache")

    try:
        data = fetcher()
        write_cache_entry(path, data)
        _, new_saved_at = read_cache_entry(path)
        return data, cache_meta(new_saved_at, source="api")
    except Exception as exc:
        if cached is not None:
            return cached, cache_meta(saved_at, source="cache-error", error=str(exc))
        raise

def get_org_cache_status(org_id: int) -> Dict[str, Any]:
    refresh_state = read_org_refresh_state(org_id)
    if refresh_state.get("last_refresh_at"):
        return {
            "saved_at": refresh_state.get("last_refresh_at"),
            "age_seconds": refresh_state.get("age_seconds"),
            "age_hours": refresh_state.get("age_hours"),
            "source": "org-refresh-state",
            "stale": False,
            "error": None,
            "events_cached": refresh_state.get("events_cached"),
            "sessions_cached": refresh_state.get("sessions_cached"),
            "championships_cached": refresh_state.get("championships_cached"),
        }
    path = cache_entry_path("orgs", str(org_id), "events.json")
    _, saved_at = read_cache_entry(path)
    return cache_meta(saved_at, source="cache-status")

def get_organization_cached(org_id: int, force_refresh: bool = False) -> tuple[Dict[str, Any], Dict[str, Any]]:
    path = cache_entry_path("orgs", str(org_id), "organization.json")
    data, meta = cache_fetch(path, lambda: client.get_organization(org_id) or {"id": org_id, "name": f"Organization #{org_id}"}, force_refresh=force_refresh)
    if not data:
        data = {"id": org_id, "name": f"Organization #{org_id}"}
    return data, meta

def get_championships_cached(org_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    path = cache_entry_path("orgs", str(org_id), "championships.json")
    data, meta = cache_fetch(path, lambda: client.get_championships(org_id) or [], force_refresh=force_refresh)
    return data if isinstance(data, list) else [], meta

def fetch_org_events(org_id: int) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    try:
        for event in client.iter_events(org_id):
            if isinstance(event, dict):
                events.append(event)
            if len(events) >= MAX_ORG_EVENTS:
                break
    except Exception:
        # Fallback to paged endpoint if iterator fails for any reason.
        events = client.get_events(org_id, limit=MAX_ORG_EVENTS) or []
    return events

def get_events_cached(org_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    path = cache_entry_path("orgs", str(org_id), "events.json")
    data, meta = cache_fetch(path, lambda: fetch_org_events(org_id), force_refresh=force_refresh)
    return data if isinstance(data, list) else [], meta

def get_event_cached(event_id: int, force_refresh: bool = False) -> tuple[Dict[str, Any], Dict[str, Any]]:
    path = cache_entry_path("events", str(event_id), "event.json")
    data, meta = cache_fetch(path, lambda: client.get_event(event_id, include_sessions=True) or {}, force_refresh=force_refresh)
    return data if isinstance(data, dict) else {}, meta

def get_sessions_cached(event_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    path = cache_entry_path("events", str(event_id), "sessions.json")
    data, meta = cache_fetch(path, lambda: client.get_sessions(event_id) or [], force_refresh=force_refresh)
    return data if isinstance(data, list) else [], meta

def get_session_cached(session_id: int, force_refresh: bool = False) -> tuple[Dict[str, Any], Dict[str, Any]]:
    path = cache_entry_path("sessions", str(session_id), "session.json")
    data, meta = cache_fetch(path, lambda: client.get_session(session_id) or {}, force_refresh=force_refresh)
    return data if isinstance(data, dict) else {}, meta

def get_results_cached(session_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    path = cache_entry_path("sessions", str(session_id), "results.json")
    data, meta = cache_fetch(path, lambda: client.get_results(session_id) or [], force_refresh=force_refresh)
    return data if isinstance(data, list) else [], meta

def get_announcements_cached(session_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    path = cache_entry_path("sessions", str(session_id), "announcements.json")
    data, meta = cache_fetch(path, lambda: client.get_announcements(session_id) or [], force_refresh=force_refresh)
    return data if isinstance(data, list) else [], meta

def get_laps_cached(session_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    path = cache_entry_path("sessions", str(session_id), "laps.json")
    data, meta = cache_fetch(path, lambda: client.get_laps(session_id) or [], force_refresh=force_refresh)
    return data if isinstance(data, list) else [], meta

def get_lap_chart_cached(session_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    path = cache_entry_path("sessions", str(session_id), "lap_chart.json")
    data, meta = cache_fetch(path, lambda: client.get_lap_chart(session_id) or [], force_refresh=force_refresh)
    return data if isinstance(data, list) else [], meta



def write_ndjson_line(handle, payload: Dict[str, Any]) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False, default=str))
    handle.write("\n")

def org_refresh_state_path(org_id: int) -> Path:
    return cache_entry_path("orgs", str(org_id), "refresh_state.json")

def read_org_refresh_state(org_id: int) -> Dict[str, Any]:
    payload = read_json_file(org_refresh_state_path(org_id))
    if not isinstance(payload, dict):
        return {}

    full_dt = parse_iso_utc(payload.get("last_full_refresh_at"))
    incremental_dt = parse_iso_utc(payload.get("last_incremental_refresh_at"))
    explicit_last_dt = parse_iso_utc(payload.get("last_refresh_at"))
    last_dt = explicit_last_dt
    if not last_dt:
        candidates = [dt for dt in (full_dt, incremental_dt) if dt]
        if candidates:
            last_dt = max(candidates)

    age = cache_age_seconds(last_dt)
    return {
        "org_id": org_id,
        "last_refresh_mode": first_non_empty(payload.get("last_refresh_mode"), "full" if full_dt else None),
        "last_refresh_at": iso_utc(last_dt) if last_dt else None,
        "last_full_refresh_at": iso_utc(full_dt) if full_dt else None,
        "last_incremental_refresh_at": iso_utc(incremental_dt) if incremental_dt else None,
        "age_seconds": age,
        "age_hours": (age / 3600.0) if age is not None else None,
        "events_cached": safe_int(payload.get("events_cached"), 0),
        "sessions_cached": safe_int(payload.get("sessions_cached"), 0),
        "championships_cached": safe_int(payload.get("championships_cached"), 0),
        "new_events_detected": safe_int(payload.get("new_events_detected"), 0),
        "refreshed_events": safe_int(payload.get("refreshed_events"), 0),
        "refreshed_sessions": safe_int(payload.get("refreshed_sessions"), 0),
    }

def save_org_dump(org_id: int, force_refresh: bool = False, max_events: Optional[int] = None) -> Dict[str, Any]:
    dump_dir = DUMPS_ROOT / str(org_id)
    dump_dir.mkdir(parents=True, exist_ok=True)

    events, _ = get_events_cached(org_id, force_refresh=force_refresh)
    if max_events is not None:
        events = events[:max_events]

    events_path = dump_dir / "events.ndjson"
    sessions_path = dump_dir / "sessions.ndjson"
    laps_path = dump_dir / "laps.ndjson"
    anns_path = dump_dir / "announcements.ndjson"
    results_path = dump_dir / "results.ndjson"

    events_count = 0
    sessions_count = 0
    laps_records_count = 0

    with open(events_path, "w", encoding="utf-8") as events_fh, \
         open(sessions_path, "w", encoding="utf-8") as sessions_fh, \
         open(laps_path, "w", encoding="utf-8") as laps_fh, \
         open(anns_path, "w", encoding="utf-8") as anns_fh, \
         open(results_path, "w", encoding="utf-8") as results_fh:
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = event.get("id")
            if not event_id:
                continue
            event_name = event.get("name")
            base_event = {"org_id": org_id, "event_id": event_id, "event_name": event_name}
            write_ndjson_line(events_fh, {**base_event, "raw": event})
            events_count += 1

            sessions, _ = get_sessions_cached(int(event_id), force_refresh=force_refresh)
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                session_id = session.get("id")
                if not session_id:
                    continue
                session_id_int = int(session_id)
                write_ndjson_line(sessions_fh, {**base_event, "session_id": session_id_int, "raw": session})
                sessions_count += 1

                announcements, _ = get_announcements_cached(session_id_int, force_refresh=force_refresh)
                write_ndjson_line(anns_fh, {**base_event, "session_id": session_id_int, "announcements": announcements})

                results, _ = get_results_cached(session_id_int, force_refresh=force_refresh)
                write_ndjson_line(results_fh, {**base_event, "session_id": session_id_int, "results": results})

                laps, _ = get_laps_cached(session_id_int, force_refresh=force_refresh)
                write_ndjson_line(laps_fh, {**base_event, "session_id": session_id_int, "rows_count": len(laps), "rows": laps})
                laps_records_count += 1

    manifest = {
        "org_id": org_id,
        "saved_at": iso_utc(utc_now()),
        "events_count": events_count,
        "sessions_count": sessions_count,
        "laps_records_count": laps_records_count,
    }
    write_json_file(dump_dir / "manifest.json", manifest)
    return {"path": str(dump_dir), **manifest}

def format_saved_at_display(saved_at_value: Optional[str]) -> str:
    saved_dt = parse_iso_utc(saved_at_value)
    if not saved_dt:
        return "Never"
    return saved_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def cache_status_label(meta: Dict[str, Any]) -> str:
    saved_at = format_saved_at_display(meta.get("saved_at"))
    age_hours = meta.get("age_hours")
    if age_hours is None:
        return f"{saved_at} (not cached yet)"
    return f"{saved_at} ({age_hours:.1f}h old)"

def scan_track_records_cached(
    org_id: int,
    classification: str,
    start_date,
    end_date,
    limit_events: Optional[int],
    force_refresh: bool = False,
) -> tuple[List[Dict[str, Any]], int, Optional[str], Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    events_scanned = 0
    error: Optional[str] = None
    events, events_meta = get_events_cached(org_id, force_refresh=force_refresh)

    try:
        for event in events:
            if not isinstance(event, dict):
                continue
            if limit_events is not None and events_scanned >= limit_events:
                break

            eid = event.get("id")
            ename = event.get("name")
            if not eid:
                continue

            e_date_str = extract_event_datetime(event)
            e_date = parse_date_to_comparison(e_date_str)
            if start_date and e_date and e_date < start_date:
                continue
            if end_date and e_date and e_date > end_date:
                continue

            events_scanned += 1
            sessions, _ = get_sessions_cached(int(eid), force_refresh=force_refresh)
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                sid = session.get("id")
                sname = session.get("name")
                if not sid:
                    continue
                announcements, _ = get_announcements_cached(int(sid), force_refresh=force_refresh)
                for ann in announcements:
                    if not isinstance(ann, dict):
                        continue
                    text = ann.get("text") or ann.get("message") or ""
                    ts = ann.get("timestamp") or ann.get("time") or e_date_str
                    parsed = parse_track_record_text(text)
                    if not parsed:
                        continue
                    class_name = parsed.get("classification") or "Unknown"
                    if classification and class_name.upper() != classification.upper():
                        continue
                    ts_value = ts[:10] if isinstance(ts, str) and ts else "N/A"
                    records.append(
                        {
                            "event_id": eid,
                            "event_name": ename,
                            "session_id": sid,
                            "session_name": sname,
                            "classification": class_name,
                            "lap_time": parsed.get("lap_time"),
                            "lap_time_seconds": parsed.get("lap_time_seconds"),
                            "driver": parsed.get("driver"),
                            "marque": parsed.get("marque"),
                            "timestamp": ts_value,
                            "text": text,
                        }
                    )
    except Exception as exc:
        error = f"Unable to complete track record scan right now: {exc}"

    records.sort(key=lambda r: ((r.get("classification") or "").upper(), r.get("lap_time_seconds") or float("inf")))
    return records, events_scanned, error, events_meta

# Inject datetime into templates globally
@app.context_processor
def inject_now():
    return {
        "datetime": datetime,
        "parse_time_value": parse_time_value,
        "format_saved_at_display": format_saved_at_display,
        "cache_status_label": cache_status_label,
    }

@app.route("/")
def index():
    org_list = []
    orgs_dir = CACHE_ROOT / "orgs"
    if orgs_dir.exists():
        for p in orgs_dir.iterdir():
            if p.is_dir() and p.name.isdigit():
                org_id_int = int(p.name)
                org_list.append({
                    "id": org_id_int,
                    "name": _org_display_name_from_cache(org_id_int)
                })
    org_list.sort(key=lambda o: o["name"].lower())

    selected_org_id = request.args.get("org_id")
    if not selected_org_id:
        if org_list:
            p_ids = [o["id"] for o in org_list]
            if 30476 in p_ids:
                selected_org_id = 30476
            else:
                selected_org_id = org_list[0]["id"]
        else:
            selected_org_id = 30476

    try:
        selected_org_id = int(selected_org_id)
    except Exception:
        selected_org_id = 30476

    # Load organization details if cached
    org, _ = get_organization_cached(selected_org_id, force_refresh=False)
    org_view = dict(org) if isinstance(org, dict) else {"id": selected_org_id, "name": f"Organization #{selected_org_id}"}
    
    org_city = first_non_empty(
        org_view.get("city"),
        (org_view.get("location") or {}).get("city") if isinstance(org_view.get("location"), dict) else None,
        (org_view.get("address") or {}).get("city") if isinstance(org_view.get("address"), dict) else None,
    )
    org_country = _country_name_from_value(
        first_non_empty(
            org_view.get("country"),
            (org_view.get("location") or {}).get("country") if isinstance(org_view.get("location"), dict) else None,
            (org_view.get("address") or {}).get("country") if isinstance(org_view.get("address"), dict) else None,
        )
    )
    org_view["_display_city"] = org_city
    org_view["_display_country"] = org_country
    location_parts = [p for p in (org_city, org_country) if p]
    org_view["_display_location"] = ", ".join(location_parts)

    events = []
    championships = []
    org_refresh_state = read_org_refresh_state(selected_org_id)
    cache_status = None

    # Date filtering for events list
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    start_date = parse_date_to_comparison(start_date_str)
    end_date = parse_date_to_comparison(end_date_str)

    events_data, events_meta = get_events_cached(selected_org_id, force_refresh=False)
    cache_status = events_meta
    sortable_events = []
    for event in events_data:
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if not event_id:
            continue
        e_date_str = extract_event_datetime(event)
        e_date = parse_date_to_comparison(e_date_str)
        if start_date and e_date and e_date < start_date:
            continue
        if end_date and e_date and e_date > end_date:
            continue
        
        event_view = dict(event)
        event_view["_display_date"] = format_datetime_display(e_date_str, include_time=False) or "N/A"
        sortable_events.append((e_date or datetime(1970, 1, 1).date(), event_view))
    
    sortable_events.sort(key=lambda item: item[0], reverse=True)
    events = [event for _, event in sortable_events]

    championships_data, _ = get_championships_cached(selected_org_id, force_refresh=False)
    for champ in championships_data:
        if isinstance(champ, dict):
            championships.append(champ)

    # Get local dump manifest
    dump_manifest = None
    dump_dir = DUMPS_ROOT / str(selected_org_id)
    manifest_path = dump_dir / "manifest.json"
    if manifest_path.exists():
        dump_manifest = read_json_file(manifest_path)

    # Driver search inline
    driver_query = (request.args.get("q") or "").strip()
    driver_matches = []
    driver_search_error = None
    max_events = max(5, min(safe_int(request.args.get("max_events"), 15), MAX_ORG_EVENTS))

    if selected_org_id and driver_query:
        try:
            for event in events_data[:max_events]:
                if not isinstance(event, dict):
                    continue
                event_id = event.get("id")
                if not event_id:
                    continue
                event_name = event.get("name") or f"Event #{event_id}"
                sessions, _ = get_sessions_cached(int(event_id), force_refresh=False)
                for session in sessions:
                    if not isinstance(session, dict):
                        continue
                    session_id = session.get("id")
                    if not session_id:
                        continue
                    session_name = session.get("name") or f"Session #{session_id}"
                    results, _ = get_results_cached(int(session_id), force_refresh=False)
                    for result in results:
                        if not isinstance(result, dict):
                            continue
                        driver_name = first_non_empty(
                            result.get("name"),
                            result.get("driverName"),
                            (result.get("competitor") or {}).get("name"),
                            (result.get("driver") or {}).get("name"),
                            result.get("participantName"),
                        ) or ""
                        score = name_match_score(driver_query, driver_name)
                        if score < 0.45 and normalize_search_text(driver_query) not in normalize_search_text(driver_name):
                            continue
                        normalized = normalize_result_row(result, available_comp_ids=None, available_start_numbers=None)
                        driver_matches.append(
                            {
                                "driver_name": driver_name,
                                "score": round(score, 3),
                                "position": normalized.get("position"),
                                "car_class": normalized.get("car_class"),
                                "best_lap": normalized.get("best_lap_display"),
                                "laps": normalized.get("laps_display"),
                                "lap_driver_id": normalized.get("lap_driver_id"),
                                "event_id": event_id,
                                "event_name": event_name,
                                "session_id": session_id,
                                "session_name": session_name,
                            }
                        )
            query_norm = normalize_search_text(driver_query)
            driver_matches.sort(
                key=lambda row: (
                    0 if query_norm in normalize_search_text(row.get("driver_name") or "") else 1,
                    -float(row.get("score") or 0),
                    safe_int(row.get("position"), default=9999),
                )
            )
            driver_matches = driver_matches[:120]
        except Exception as exc:
            driver_search_error = str(exc)

    return render_template(
        "index.html",
        notice=request.args.get("notice"),
        error=request.args.get("error"),
        web_data_root=str(WEB_DATA_ROOT),
        org_list=org_list,
        org=org_view,
        selected_org_id=selected_org_id,
        events=events,
        championships=championships,
        org_refresh_state=org_refresh_state,
        cache_status=cache_status,
        dump_manifest=dump_manifest,
        start_date=start_date_str,
        end_date=end_date_str,
        incremental_backfill_events=DEFAULT_INCREMENTAL_BACKFILL_EVENTS,
        driver_query=driver_query,
        driver_matches=driver_matches,
        driver_search_error=driver_search_error,
        max_events=max_events,
    )

@app.route("/org-search")
def org_search():
    org_id = request.args.get("org_id")
    if org_id:
        return redirect(url_for("index", org_id=org_id))
    return redirect(url_for("index"))

@app.route("/org/<org_id>")
def org_details(org_id):
    return redirect(url_for("index", org_id=org_id, **request.args))

@app.route("/driver-search")
def driver_search():
    org_id = request.args.get("org_id")
    q = request.args.get("q")
    if org_id and q:
        return redirect(url_for("index", **request.args))
    if org_id:
        return redirect(url_for("index", org_id=org_id))
    return redirect(url_for("index"))

@app.route("/org/<org_id>/refresh", methods=["POST"])
def refresh_org(org_id):
    """Start an async refresh task and redirect to org page (legacy sync path kept for non-JS fallback)."""
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))

    mode = (request.form.get("mode") or "incremental").strip().lower()
    if mode not in {"full", "incremental"}:
        mode = "incremental"
    backfill_events = max(
        0,
        min(
            safe_int(request.form.get("backfill_events"), DEFAULT_INCREMENTAL_BACKFILL_EVENTS),
            25,
        ),
    )

    # If the request wants JSON (from fetch() in the new UI), start async task
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        task_id = _new_task(org_id_int, mode)
        t = threading.Thread(
            target=_run_refresh_task,
            args=(task_id, org_id_int, mode, backfill_events),
            daemon=True,
        )
        t.start()
        return jsonify({"task_id": task_id, "org_id": org_id_int, "mode": mode})

    # Legacy synchronous path (fallback for no-JS)
    try:
        summary = refresh_org_cache_bundle(
            client=client,
            cache_root=CACHE_ROOT,
            org_id=org_id_int,
            mode=mode,
            max_events=MAX_ORG_EVENTS,
            recent_backfill_events=backfill_events if mode == "incremental" else 0,
            cleanup_on_full=True,
        )
        refreshed = format_saved_at_display(summary.get("last_refresh_at"))
        mode_label = "Full" if mode == "full" else "Incremental"
        notice = (
            f"{mode_label} refresh complete for org {org_id_int}: "
            f"{summary.get('refreshed_events', 0)} events updated, "
            f"{summary.get('refreshed_sessions', 0)} sessions updated, "
            f"{summary.get('new_events_detected', 0)} new events found. "
            f"Completed at {refreshed}."
        )
        return redirect(url_for("org_details", org_id=org_id_int, notice=notice))
    except Exception as exc:
        return redirect(url_for("org_details", org_id=org_id_int, error=f"Refresh failed: {exc}"))


@app.route("/org/<org_id>/refresh/start", methods=["POST"])
def refresh_org_start(org_id):
    """Start an async background refresh task, returning task_id as JSON."""
    try:
        org_id_int = int(org_id)
    except Exception:
        return jsonify({"error": "Invalid organization ID"}), 400

    mode = (request.json.get("mode") if request.is_json else request.form.get("mode") or "incremental")
    mode = (mode or "incremental").strip().lower()
    if mode not in {"full", "incremental"}:
        mode = "incremental"
    backfill_events = max(
        0,
        min(
            safe_int(
                (request.json.get("backfill_events") if request.is_json else request.form.get("backfill_events")),
                DEFAULT_INCREMENTAL_BACKFILL_EVENTS,
            ),
            25,
        ),
    )

    task_id = _new_task(org_id_int, mode)
    t = threading.Thread(
        target=_run_refresh_task,
        args=(task_id, org_id_int, mode, backfill_events),
        daemon=True,
    )
    t.start()
    return jsonify({"task_id": task_id, "org_id": org_id_int, "mode": mode})


@app.route("/refresh/status/<task_id>")
def refresh_status(task_id):
    """Poll status of an async refresh task."""
    task = _get_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    # Don't leak internal cached_event_ids lists in the poll response
    task.pop("summary", None)
    return jsonify(task)


@app.route("/refresh/stop/<task_id>", methods=["POST"])
def refresh_stop(task_id):
    """Request cancellation of a running refresh task."""
    task = _get_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    _update_task(task_id, stop_requested=True)
    return jsonify({"task_id": task_id, "stop_requested": True})



@app.route("/org/<org_id>/save-local", methods=["POST"])
def save_local(org_id):
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))
    try:
        max_events_val = request.form.get("max_events")
        max_events = max(1, min(safe_int(max_events_val, 25), MAX_ORG_EVENTS)) if max_events_val else 25
        summary = save_org_dump(org_id_int, force_refresh=False, max_events=max_events)
        notice = (
            f"Saved local dump to {summary['path']} with {summary['events_count']} events, "
            f"{summary['sessions_count']} sessions, {summary['laps_records_count']} lap-record blocks."
        )
        return redirect(url_for("org_details", org_id=org_id_int, notice=notice))
    except Exception as exc:
        return redirect(url_for("org_details", org_id=org_id_int, error=f"Save-local failed: {exc}"))

@app.route("/org/<org_id>/download-local-dump.zip")
def download_local_dump(org_id):
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))
    dump_dir = DUMPS_ROOT / str(org_id_int)
    if not dump_dir.exists():
        return redirect(url_for("org_details", org_id=org_id_int, error="No local dump found. Run Save Local Data first."))

    tmp = tempfile.NamedTemporaryFile(prefix=f"speedhive_org_{org_id_int}_", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in dump_dir.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(dump_dir.parent)))

    @after_this_request
    def cleanup(response):
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return response

    return send_file(
        str(tmp_path),
        as_attachment=True,
        download_name=f"speedhive_org_{org_id_int}_dump.zip",
        mimetype="application/zip",
    )

@app.route("/org/<org_id>/export-lap-records.ndjson")
def export_org_lap_records(org_id):
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))

    max_events = max(1, min(safe_int(request.args.get("max_events"), 25), MAX_ORG_EVENTS))

    def generate():
        events, _ = get_events_cached(org_id_int, force_refresh=False)
        for event in events[:max_events]:
            if not isinstance(event, dict):
                continue
            event_id = event.get("id")
            if not event_id:
                continue
            event_name = event.get("name")
            base_event = {"org_id": org_id_int, "event_id": event_id, "event_name": event_name}
            sessions, _ = get_sessions_cached(int(event_id), force_refresh=False)
            for session in sessions:
                if not isinstance(session, dict) or not session.get("id"):
                    continue
                sid = int(session["id"])
                laps, _ = get_laps_cached(sid, force_refresh=False)
                payload = {**base_event, "session_id": sid, "rows_count": len(laps), "rows": laps}
                yield json.dumps(payload, ensure_ascii=False, default=str) + "\n"

    headers = {"Content-Disposition": f"attachment; filename=org_{org_id_int}_laps_top_{max_events}.ndjson"}
    return Response(generate(), mimetype="application/x-ndjson", headers=headers)

@app.route("/event/<event_id>")
def event_info(event_id):
    try:
        event_id_int = int(event_id)
        event, _ = get_event_cached(event_id_int, force_refresh=False)
        if not event:
            return render_template("event.html", error=f"Event #{event_id} not found", event_id=event_id), 404

        event_view = dict(event)
        organization = event.get("organization") if isinstance(event.get("organization"), dict) else {}
        location = event.get("location") if isinstance(event.get("location"), dict) else {}
        event_view["organizationId"] = first_non_empty(event.get("organizationId"), organization.get("id"))
        event_view["_display_date"] = format_datetime_display(
            first_non_empty(extract_event_datetime(event), event.get("updatedAt")),
            include_time=True,
        ) or "N/A"
        event_view["_display_venue"] = first_non_empty(
            event.get("venue"),
            location.get("name"),
            location.get("city"),
            event.get("eventRef"),
        ) or "N/A"
        event_view["_display_country"] = first_non_empty(
            event.get("country"),
            location.get("country"),
            location.get("countryCode"),
        ) or "N/A"

        sessions, sessions_meta = get_sessions_cached(event_id_int, force_refresh=False)
        sessions_view = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            session_view = dict(session)
            session_view["_display_start"] = format_datetime_display(
                first_non_empty(
                    session.get("startTime"),
                    session.get("scheduledStart"),
                    session.get("start_date"),
                    session.get("date"),
                ),
                include_time=True,
            ) or "N/A"
            sessions_view.append(session_view)
        return render_template(
            "event.html",
            event=event_view,
            sessions=sessions_view,
            event_id=event_id,
            cache_status=sessions_meta,
        )
    except Exception as exc:
        return render_template("event.html", error=str(exc), event_id=event_id), 500

@app.route("/session/<session_id>")
@app.route("/session/<session_id>/results")
def session_results(session_id):
    try:
        session_id_int = int(session_id)
        session, session_meta = get_session_cached(session_id_int, force_refresh=False)
        results, _ = get_results_cached(session_id_int, force_refresh=False)
        announcements, _ = get_announcements_cached(session_id_int, force_refresh=False)
        lap_chart, _ = get_lap_chart_cached(session_id_int, force_refresh=False)
        all_laps, laps_meta = get_laps_cached(session_id_int, force_refresh=False)

        session_view = dict(session) if isinstance(session, dict) else {}
        session_view["_display_start"] = format_datetime_display(
            first_non_empty(
                session_view.get("startTime"),
                session_view.get("scheduledStart"),
                session_view.get("start_date"),
                session_view.get("date"),
            ),
            include_time=True,
        ) or "Time N/A"

        available_comp_ids = {
            str(cid)
            for cid in (
                first_non_empty(lap.get("competitorId"), lap.get("competitor_id"), lap.get("id"))
                for lap in all_laps
                if isinstance(lap, dict)
            )
            if cid not in (None, "")
        }
        available_start_numbers = {
            str(sn)
            for sn in (
                first_non_empty(lap.get("startNumber"), lap.get("start_number"))
                for lap in all_laps
                if isinstance(lap, dict)
            )
            if sn not in (None, "")
        }

        normalized_results = []
        for row in results:
            if isinstance(row, dict):
                normalized_results.append(normalize_result_row(row, available_comp_ids, available_start_numbers))
        normalized_results.sort(key=lambda r: r.get("position_sort", 9999))

        lap_chart_rows = lap_chart if isinstance(lap_chart, list) else []
        if not lap_chart_rows:
            lap_chart_rows = build_lap_chart_from_laps(all_laps)

        return render_template(
            "results.html",
            session=session_view,
            results=normalized_results,
            announcements=announcements,
            lap_chart=lap_chart_rows,
            session_id=session_id,
            cache_status=laps_meta if laps_meta else session_meta,
        )
    except Exception as exc:
        return render_template("results.html", error=str(exc), session_id=session_id), 500

@app.route("/session/<session_id>/export-laps.json")
def export_session_laps(session_id):
    try:
        session_id_int = int(session_id)
        laps, _ = get_laps_cached(session_id_int, force_refresh=False)
        payload = {"session_id": session_id_int, "laps": laps}
        body = json.dumps(payload, indent=2, default=str)
        headers = {"Content-Disposition": f"attachment; filename=session_{session_id_int}_laps.json"}
        return Response(body, mimetype="application/json", headers=headers)
    except Exception as exc:
        return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")

@app.route("/session/<session_id>/driver/<driver_id>/laps")
def lap_times(session_id, driver_id):
    try:
        session_id_int = int(session_id)
        lookup_mode = "cid"
        lookup_value = str(driver_id)
        if ":" in lookup_value:
            prefix, token = lookup_value.split(":", 1)
            if prefix in {"cid", "sn", "pos"}:
                lookup_mode = prefix
                lookup_value = token

        all_laps, _ = get_laps_cached(session_id_int, force_refresh=False)
        driver_laps = []
        for lap in all_laps:
            if not isinstance(lap, dict):
                continue
            if lookup_mode == "cid":
                comp_id = first_non_empty(lap.get("competitorId"), lap.get("competitor_id"), lap.get("id"))
                if comp_id is not None and str(comp_id) == lookup_value:
                    driver_laps.append(lap)
            elif lookup_mode == "sn":
                start_number = first_non_empty(lap.get("startNumber"), lap.get("start_number"))
                if start_number is not None and str(start_number) == lookup_value:
                    driver_laps.append(lap)
            elif lookup_mode == "pos":
                if str(lap.get("position")) == lookup_value:
                    driver_laps.append(lap)

        driver_laps.sort(key=lambda x: safe_int(first_non_empty(x.get("lapNumber"), x.get("lap")), 0))
        driver_name = f"Competitor #{driver_id}"
        results, _ = get_results_cached(session_id_int, force_refresh=False)
        for result in results:
            if not isinstance(result, dict):
                continue
            if lookup_mode == "cid":
                r_comp_id = first_non_empty(result.get("competitorId"), result.get("id"), (result.get("competitor") or {}).get("id"))
                if r_comp_id is not None and str(r_comp_id) == lookup_value:
                    driver_name = first_non_empty(
                        result.get("name"),
                        result.get("driverName"),
                        (result.get("competitor") or {}).get("name"),
                        (result.get("driver") or {}).get("name"),
                    ) or driver_name
                    break
            elif lookup_mode == "sn":
                r_start_number = first_non_empty(result.get("startNumber"), result.get("transponder"))
                if r_start_number is not None and str(r_start_number) == lookup_value:
                    driver_name = first_non_empty(
                        result.get("name"),
                        result.get("driverName"),
                        (result.get("competitor") or {}).get("name"),
                        (result.get("driver") or {}).get("name"),
                    ) or driver_name
                    break
            elif lookup_mode == "pos":
                r_pos = first_non_empty(result.get("position"), result.get("pos"))
                if r_pos is not None and str(r_pos) == lookup_value:
                    driver_name = first_non_empty(
                        result.get("name"),
                        result.get("driverName"),
                        (result.get("competitor") or {}).get("name"),
                        (result.get("driver") or {}).get("name"),
                    ) or driver_name
                    break

        if driver_name.startswith("Competitor #"):
            if lookup_mode == "pos":
                driver_name = f"Position {lookup_value} Trace"
            elif lookup_mode == "sn":
                driver_name = f"Start Number {lookup_value}"

        stats = compute_lap_statistics(driver_laps)
        return render_template(
            "lap_times.html",
            laps=driver_laps,
            stats=stats,
            driver_name=driver_name,
            session_id=session_id,
            driver_id=driver_id,
        )
    except Exception as exc:
        return render_template("lap_times.html", error=str(exc), session_id=session_id, driver_id=driver_id), 500

@app.route("/championship/<championship_id>")
def championship_details(championship_id):
    try:
        championship_id_int = int(championship_id)
        path = cache_entry_path("championships", str(championship_id_int), "details.json")
        championship, _ = cache_fetch(path, lambda: client.get_championship(championship_id_int) or {}, force_refresh=False)
        if not championship:
            return render_template("championship.html", error=f"Championship #{championship_id} not found", championship_id=championship_id), 404
        return render_template("championship.html", championship=championship, championship_id=championship_id)
    except Exception as exc:
        return render_template("championship.html", error=str(exc), championship_id=championship_id), 500

@app.route("/track-records")
def track_records():
    org_id = request.args.get("org_id")
    if not org_id:
        return redirect(url_for("index"))

    classification = (request.args.get("classification") or "").strip()
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    limit_events_str = request.args.get("limit_events", "10")
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return render_template(
            "track_records.html",
            error="Invalid organization ID.",
            org_id=org_id,
            records=[],
            classification=classification,
            start_date=start_date_str,
            end_date=end_date_str,
            events_scanned_count=0,
        )

    start_date = parse_date_to_comparison(start_date_str)
    end_date = parse_date_to_comparison(end_date_str)
    limit_events = int(limit_events_str) if limit_events_str.isdigit() else 10
    if limit_events == 0:
        limit_events = None

    org, _ = get_organization_cached(org_id_int, force_refresh=False)
    org_name = org.get("name") if (org and isinstance(org, dict)) else f"Organization #{org_id_int}"

    records, events_scanned, error, cache_status = scan_track_records_cached(
        org_id=org_id_int,
        classification=classification,
        start_date=start_date,
        end_date=end_date,
        limit_events=limit_events,
        force_refresh=False,
    )

    return render_template(
        "track_records.html",
        org_id=org_id,
        org_name=org_name,
        records=records,
        classification=classification,
        start_date=start_date_str,
        end_date=end_date_str,
        events_scanned_count=events_scanned,
        error=error,
        cache_status=cache_status,
    )

@app.route("/track-records/export.json")
def export_track_records_json():
    org_id = request.args.get("org_id")
    if not org_id:
        return redirect(url_for("index", error="Missing org_id for export."))
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid org_id for export."))

    classification = (request.args.get("classification") or "").strip()
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    start_date = parse_date_to_comparison(start_date_str)
    end_date = parse_date_to_comparison(end_date_str)
    limit_events = safe_int(request.args.get("limit_events"), 10)
    if limit_events == 0:
        limit_events = None
    records, events_scanned, error, _ = scan_track_records_cached(
        org_id=org_id_int,
        classification=classification,
        start_date=start_date,
        end_date=end_date,
        limit_events=limit_events,
        force_refresh=False,
    )
    payload = {
        "org_id": org_id_int,
        "classification": classification or None,
        "start_date": start_date_str or None,
        "end_date": end_date_str or None,
        "events_scanned": events_scanned,
        "error": error,
        "records": records,
    }
    body = json.dumps(payload, indent=2, default=str)
    headers = {"Content-Disposition": f"attachment; filename=org_{org_id_int}_track_records.json"}
    return Response(body, mimetype="application/json", headers=headers)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8854, debug=True)
