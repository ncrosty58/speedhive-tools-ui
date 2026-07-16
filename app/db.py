from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from speedhive.utils.lap_analysis import first_non_empty, safe_int
from app.utils import cache_meta, utc_now, parse_iso_utc, iso_utc, _country_name_from_value

# Constants
MAX_ORG_EVENTS = 150


def store_fetch(
    fetcher: Callable[[], Any],
    force_refresh: bool = False,
    *,
    db_reader: Optional[Callable[[], tuple[Any, Optional[datetime]]]] = None,
    db_writer: Optional[Callable[[Any, Optional[datetime]], None]] = None,
) -> tuple[Any, Dict[str, Any]]:
    if not force_refresh and db_reader is not None:
        db_cached, db_saved_at = db_reader()
        if db_cached is not None:
            return db_cached, cache_meta(db_saved_at, source="db")

    try:
        data = fetcher()
        new_saved_at = utc_now()
        if db_writer is not None:
            try:
                db_writer(data, new_saved_at)
            except Exception:
                pass
        return data, cache_meta(new_saved_at, source="api")
    except Exception:
        raise


def db_stored_record(getter: Callable[[], Any]) -> tuple[Any, Optional[datetime]]:
    record = getter()
    saved_at = parse_iso_utc(record.saved_at) if getattr(record, "saved_at", None) else None
    return getattr(record, "payload", None), saved_at


def read_from_store(getter: Callable[[], Any], *, empty_value: Any) -> tuple[Any, Dict[str, Any]]:
    payload, saved_at = db_stored_record(getter)
    if payload is None:
        payload = empty_value
    return payload, cache_meta(saved_at, source="db")


def read_organization_from_store(org_id: int) -> tuple[Dict[str, Any], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_organization(org_id), empty_value={})
    return payload if isinstance(payload, dict) else {}, meta


def get_org_view(org_id: int, client: Any = None) -> Dict[str, Any]:
    """Org dict enriched with `_display_location` for the shared org header/nav.

    If nothing is cached and a Speedhive client is given, falls back to a live
    lookup (matching the dashboard's historical behavior); otherwise falls
    back to a bare id/name placeholder.
    """
    org, _ = read_organization_from_store(org_id)
    if not org and client is not None:
        org = client.get_organization(org_id) or {}
    org_view = dict(org) if isinstance(org, dict) and org else {"id": org_id, "name": f"Organization #{org_id}"}

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
    org_view["_display_location"] = ", ".join(p for p in (org_city, org_country) if p)
    return org_view


def read_championships_from_store(org_id: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_championships(org_id), empty_value=[])
    return payload if isinstance(payload, list) else [], meta


def read_events_from_store(org_id: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_events(org_id), empty_value=[])
    return payload if isinstance(payload, list) else [], meta


def read_event_from_store(event_id: int) -> tuple[Dict[str, Any], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_event(event_id), empty_value={})
    return payload if isinstance(payload, dict) else {}, meta


def read_event_sessions_from_store(event_id: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_event_sessions(event_id), empty_value=[])
    return payload if isinstance(payload, list) else [], meta


def read_session_from_store(session_id: int) -> tuple[Dict[str, Any], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_session(session_id), empty_value={})
    return payload if isinstance(payload, dict) else {}, meta


def read_results_from_store(session_id: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_results(session_id), empty_value=[])
    return payload if isinstance(payload, list) else [], meta


def read_announcements_from_store(session_id: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_announcements(session_id), empty_value=[])
    return payload if isinstance(payload, list) else [], meta


def read_laps_from_store(session_id: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_laps(session_id), empty_value=[])
    return payload if isinstance(payload, list) else [], meta


def read_lap_chart_from_store(session_id: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage
    payload, meta = read_from_store(lambda: storage.get_lap_chart(session_id), empty_value=[])
    return payload if isinstance(payload, list) else [], meta


def get_org_store_status(org_id: int) -> Dict[str, Any]:
    from app import storage
    status = storage.get_org_status(org_id)
    if status.get("last_refresh_at"):
        return {
            "saved_at": status.get("last_refresh_at"),
            "age_seconds": status.get("age_seconds"),
            "age_hours": status.get("age_hours"),
            "source": "org-refresh-state",
            "stale": False,
            "error": None,
            "events_cached": status.get("events_cached"),
            "sessions_cached": status.get("sessions_cached"),
            "championships_cached": status.get("championships_cached"),
        }
    return cache_meta(None, source="cache-status")


def get_organization_stored(org_id: int, force_refresh: bool = False) -> tuple[Dict[str, Any], Dict[str, Any]]:
    from app import storage, client
    data, meta = store_fetch(
        lambda: client.get_organization(org_id) or {"id": org_id, "name": f"Organization #{org_id}"},
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_organization(org_id)),
        db_writer=lambda payload, saved_at: storage.save_organization(
            org_id,
            payload if isinstance(payload, dict) else {"id": org_id, "name": f"Organization #{org_id}"},
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    if not data:
        data = {"id": org_id, "name": f"Organization #{org_id}"}
    return data, meta


def get_championships_stored(org_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage, client
    data, meta = store_fetch(
        lambda: client.get_championships(org_id) or [],
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_championships(org_id)),
        db_writer=lambda payload, saved_at: storage.save_championships(
            org_id,
            payload if isinstance(payload, list) else [],
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, list) else [], meta


def fetch_org_events(org_id: int) -> List[Dict[str, Any]]:
    from app import client
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


def get_events_stored(org_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage
    data, meta = store_fetch(
        lambda: fetch_org_events(org_id),
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_events(org_id)),
        db_writer=lambda payload, saved_at: storage.save_events(
            org_id,
            payload if isinstance(payload, list) else [],
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, list) else [], meta


def get_event_stored(event_id: int, force_refresh: bool = False) -> tuple[Dict[str, Any], Dict[str, Any]]:
    from app import storage, client
    data, meta = store_fetch(
        lambda: client.get_event(event_id, include_sessions=True) or {},
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_event(event_id)),
        db_writer=lambda payload, saved_at: storage.save_event(
            event_id,
            _infer_event_org_id(payload),
            payload if isinstance(payload, dict) else {},
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, dict) else {}, meta


def get_sessions_stored(event_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage, client
    event_payload, _ = get_event_stored(event_id, force_refresh=False)
    event_org_id = _infer_event_org_id(event_payload)
    data, meta = store_fetch(
        lambda: client.get_sessions(event_id) or [],
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_event_sessions(event_id)),
        db_writer=lambda payload, saved_at: storage.save_event_sessions(
            event_id,
            event_org_id,
            payload if isinstance(payload, list) else [],
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, list) else [], meta


def get_session_stored(session_id: int, force_refresh: bool = False) -> tuple[Dict[str, Any], Dict[str, Any]]:
    from app import storage, client
    data, meta = store_fetch(
        lambda: client.get_session(session_id) or {},
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_session(session_id)),
        db_writer=lambda payload, saved_at: storage.save_session(
            session_id,
            _infer_session_event_id(payload),
            None,
            payload if isinstance(payload, dict) else {},
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, dict) else {}, meta


def get_results_stored(session_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage, client
    session_payload, _ = get_session_stored(session_id, force_refresh=False)
    event_id = _infer_session_event_id(session_payload)
    data, meta = store_fetch(
        lambda: client.get_results(session_id) or [],
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_results(session_id)),
        db_writer=lambda payload, saved_at: storage.save_results(
            session_id,
            event_id,
            None,
            payload if isinstance(payload, list) else [],
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, list) else [], meta


def get_announcements_stored(session_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage, client
    session_payload, _ = get_session_stored(session_id, force_refresh=False)
    event_id = _infer_session_event_id(session_payload)
    data, meta = store_fetch(
        lambda: client.get_announcements(session_id) or [],
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_announcements(session_id)),
        db_writer=lambda payload, saved_at: storage.save_announcements(
            session_id,
            event_id,
            None,
            payload if isinstance(payload, list) else [],
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, list) else [], meta


def get_laps_stored(session_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage, client
    session_payload, _ = get_session_stored(session_id, force_refresh=False)
    event_id = _infer_session_event_id(session_payload)
    data, meta = store_fetch(
        lambda: client.get_laps(session_id) or [],
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_laps(session_id)),
        db_writer=lambda payload, saved_at: storage.save_laps(
            session_id,
            event_id,
            None,
            payload if isinstance(payload, list) else [],
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, list) else [], meta


def get_lap_chart_stored(session_id: int, force_refresh: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from app import storage, client
    session_payload, _ = get_session_stored(session_id, force_refresh=False)
    event_id = _infer_session_event_id(session_payload)
    data, meta = store_fetch(
        lambda: client.get_lap_chart(session_id) or [],
        force_refresh=force_refresh,
        db_reader=lambda: db_stored_record(lambda: storage.get_lap_chart(session_id)),
        db_writer=lambda payload, saved_at: storage.save_lap_chart(
            session_id,
            event_id,
            None,
            payload if isinstance(payload, list) else [],
            saved_at=iso_utc(saved_at) if saved_at else None,
        ),
    )
    return data if isinstance(data, list) else [], meta


def _infer_event_org_id(event_payload: Any) -> Optional[int]:
    if not isinstance(event_payload, dict):
        return None
    organization = event_payload.get("organization")
    if isinstance(organization, dict):
        return safe_int(
            first_non_empty(
                organization.get("id"),
                organization.get("organizationId"),
                organization.get("orgId"),
            ),
            None,
        )
    return safe_int(
        first_non_empty(
            event_payload.get("organizationId"),
            event_payload.get("orgId"),
            event_payload.get("org_id"),
        ),
        None,
    )


def _infer_session_event_id(session_payload: Any) -> Optional[int]:
    if not isinstance(session_payload, dict):
        return None
    return safe_int(
        first_non_empty(
            session_payload.get("eventId"),
            session_payload.get("event_id"),
        ),
        None,
    )


def read_org_refresh_state(org_id: int) -> Dict[str, Any]:
    from app import storage
    return storage.get_org_status(org_id)


def list_stored_orgs() -> List[Dict[str, Any]]:
    from app import storage
    org_map: Dict[int, Dict[str, Any]] = {}
    for row in storage.list_organizations():
        org_id_int = safe_int(row.get("org_id"), None)
        if org_id_int is None:
            continue
        name = row.get("name") or f"Organization #{org_id_int}"
        org_map[org_id_int] = {"id": org_id_int, "name": name}

    org_list = list(org_map.values())
    org_list.sort(key=lambda o: o["name"].lower())
    return org_list


def scan_track_records_from_synced_store(
    org_id: int,
    classification: str,
    start_date,
    end_date,
    limit_events: Optional[int],
) -> tuple[List[Dict[str, Any]], int, Optional[str], Dict[str, Any]]:
    from app import storage
    from app.utils import extract_event_datetime, parse_date_to_comparison
    from speedhive.utils.lap_analysis import parse_track_record_text

    records: List[Dict[str, Any]] = []
    events_scanned = 0
    error: Optional[str] = None
    events_payload, events_saved_at = db_stored_record(lambda: storage.get_events(org_id))
    events = events_payload if isinstance(events_payload, list) else []
    events_meta = cache_meta(events_saved_at, source="db") if events_saved_at else cache_meta(None, source="db")

    try:
        for event in events:
            if not isinstance(event, dict):
                continue
            if limit_events is not None and events_scanned >= limit_events:
                break

            eid = safe_int(event.get("id"), None)
            ename = event.get("name")
            if eid is None:
                continue

            e_date_str = extract_event_datetime(event)
            e_date = parse_date_to_comparison(e_date_str)
            if start_date and e_date and e_date < start_date:
                continue
            if end_date and e_date and e_date > end_date:
                continue

            sessions_payload, _ = db_stored_record(lambda: storage.get_event_sessions(eid))
            sessions = sessions_payload if isinstance(sessions_payload, list) else []
            events_scanned += 1
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                sid = safe_int(session.get("id"), None)
                sname = session.get("name")
                if sid is None:
                    continue
                announcements_payload, _ = db_stored_record(lambda: storage.get_announcements(sid))
                announcements = announcements_payload if isinstance(announcements_payload, list) else []
                for ann in announcements:
                    if not isinstance(ann, dict):
                        continue
                    text = ann.get("text") or ann.get("message") or ""
                    ts = ann.get("timestamp") or ann.get("time") or e_date_str
                    parsed = parse_track_record_text(text)
                    if not parsed:
                        continue
                    class_name = parsed.get("classification") or "Unknown"
                    if classification and classification.upper() not in class_name.upper():
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

    records.sort(key=lambda r: ((r.get("classification") or "").upper(), r.get("lap_time_seconds") or float('inf')))
    return records, events_scanned, error, events_meta


# Dump file helpers
def _dump_root_for_org(org_id: int) -> Path:
    from app.tasks import WEB_DATA_ROOT
    return WEB_DATA_ROOT / "saved_dumps" / str(org_id)


def _dump_history_root_for_org(org_id: int) -> Path:
    return _dump_root_for_org(org_id) / "history"


def _dump_manifest_path(dump_dir: Path) -> Path:
    return dump_dir / "manifest.json"


def _read_dump_manifest(dump_dir: Path) -> Optional[Dict[str, Any]]:
    from app.utils import read_json_file
    manifest_path = _dump_manifest_path(dump_dir)
    if not manifest_path.exists():
        return None
    return read_json_file(manifest_path)


def _dump_dir_name(saved_at: Optional[str]) -> str:
    from datetime import timezone
    saved_dt = parse_iso_utc(saved_at)
    if not saved_dt:
        return "unknown"
    return saved_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _archive_existing_latest_dump(org_id: int) -> Optional[Path]:
    import shutil
    from datetime import timezone
    dump_root = _dump_root_for_org(org_id)
    current_manifest = _read_dump_manifest(dump_root)
    if not current_manifest:
        return None

    archive_id = _dump_dir_name(current_manifest.get("saved_at"))
    if archive_id == "unknown":
        archive_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    archive_root = _dump_history_root_for_org(org_id) / archive_id
    archive_root.parent.mkdir(parents=True, exist_ok=True)
    suffix = 2
    while archive_root.exists():
        archive_root = _dump_history_root_for_org(org_id) / f"{archive_id}-{suffix}"
        suffix += 1
    if archive_root.exists():
        shutil.rmtree(archive_root, ignore_errors=True)
    archive_root.mkdir(parents=True, exist_ok=True)

    for child in dump_root.iterdir():
        if child.name == "history":
            continue
        target = archive_root / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)

    return archive_root


def _resolve_dump_dir_for_org(org_id: int, dump_key: Optional[str]) -> Optional[Path]:
    dump_root = _dump_root_for_org(org_id)
    if dump_key in (None, "", "latest"):
        return dump_root

    dump_dir = _dump_history_root_for_org(org_id) / dump_key
    try:
        dump_dir.resolve().relative_to(dump_root.resolve())
    except Exception:
        return None
    return dump_dir


def _replace_latest_dump_contents(org_id: int, source_dir: Path) -> None:
    import shutil
    dump_root = _dump_root_for_org(org_id)
    dump_root.mkdir(parents=True, exist_ok=True)
    for child in list(dump_root.iterdir()):
        if child.name == "history":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)

    for child in source_dir.iterdir():
        shutil.move(str(child), str(dump_root / child.name))


def _delete_latest_dump_contents(org_id: int) -> None:
    import shutil
    dump_root = _dump_root_for_org(org_id)
    if not dump_root.exists():
        return

    for child in list(dump_root.iterdir()):
        if child.name == "history":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _prune_empty_dump_roots(org_id: int) -> None:
    dump_root = _dump_root_for_org(org_id)
    history_root = _dump_history_root_for_org(org_id)
    if history_root.exists():
        try:
            next(history_root.iterdir())
        except StopIteration:
            history_root.rmdir()
    if dump_root.exists():
        try:
            next(dump_root.iterdir())
        except StopIteration:
            dump_root.rmdir()


def _list_org_dumps(org_id: int) -> List[Dict[str, Any]]:
    from flask import url_for
    dump_root = _dump_root_for_org(org_id)
    dumps: List[Dict[str, Any]] = []

    latest_manifest = _read_dump_manifest(dump_root)
    if latest_manifest:
        dumps.append(
            {
                "key": "latest",
                "is_latest": True,
                "label": "Latest dump",
                "download_url": url_for("download_local_dump", org_id=org_id),
                "manifest": latest_manifest,
            }
        )

    history_root = _dump_history_root_for_org(org_id)
    if history_root.exists():
        archived: List[Dict[str, Any]] = []
        for archive_dir in history_root.iterdir():
            if not archive_dir.is_dir():
                continue
            manifest = _read_dump_manifest(archive_dir)
            if not manifest:
                continue
            archived.append(
                {
                    "key": archive_dir.name,
                    "is_latest": False,
                    "label": format_saved_at_display(manifest.get("saved_at")),
                    "download_url": url_for("download_local_dump", org_id=org_id, dump_key=archive_dir.name),
                    "manifest": manifest,
                }
            )

        archived.sort(key=lambda dump: dump["manifest"].get("saved_at") or "", reverse=True)
        dumps.extend(archived)

    return dumps


def save_org_dump(org_id: int, force_refresh: bool = False, max_events: Optional[int] = None) -> Dict[str, Any]:
    import shutil
    import tempfile
    from app import storage, export_db_dump
    from app.tasks import WEB_DATA_ROOT
    
    dumps_root = WEB_DATA_ROOT / "saved_dumps"
    dump_root = _dump_root_for_org(org_id)
    dump_root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f"speedhive_org_{org_id}_", dir=str(dumps_root)))
    try:
        summary = export_db_dump(storage, org_id, staging_dir, max_events)
        _archive_existing_latest_dump(org_id)
        _replace_latest_dump_contents(org_id, staging_dir)
        return summary
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def format_saved_at_display(saved_at_value: Optional[str]) -> str:
    saved_dt = parse_iso_utc(saved_at_value)
    if not saved_dt:
        return "Never"
    return saved_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def store_status_label(meta: Dict[str, Any]) -> str:
    saved_at = format_saved_at_display(meta.get("saved_at"))
    age_hours = meta.get("age_hours")
    if age_hours is None:
        return f"{saved_at} (not cached yet)"
    return f"{saved_at} ({age_hours:.1f}h old)"



