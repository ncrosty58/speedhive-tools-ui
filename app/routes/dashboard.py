import os
from datetime import datetime
from flask import render_template, request, session, redirect, url_for, jsonify
from app import client
from app.db import (
    list_stored_orgs,
    read_organization_from_store,
    read_events_from_store,
    read_championships_from_store,
    read_org_refresh_state,
    scan_track_records_from_synced_store,
    read_event_sessions_from_store,
    read_results_from_store,
    _read_dump_manifest,
    _dump_root_for_org,
)
from app.utils import (
    parse_date_to_comparison,
    _country_name_from_value,
    extract_event_datetime,
    format_datetime_display,
)
from app.tasks import WEB_DATA_ROOT, MAX_ORG_EVENTS
from speedhive.utils.lap_analysis import (
    first_non_empty,
    normalize_search_text,
    name_match_score,
    normalize_result_row,
    safe_int,
)

DEFAULT_INCREMENTAL_BACKFILL_EVENTS = int(os.environ.get("SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS", "3"))


def index():
    active_tab = "dashboard"
    org_list = list_stored_orgs()

    selected_org_id = request.args.get("org_id")
    if not selected_org_id:
        selected_org_id = session.get("org_id")
    if not selected_org_id:
        if org_list:
            selected_org_id = org_list[0]["id"]
        else:
            selected_org_id = None

    if selected_org_id is None:
        return render_template(
            "index.html",
            notice=request.args.get("notice"),
            error=request.args.get("error"),
            web_data_root=str(WEB_DATA_ROOT),
            org_list=org_list,
            org=None,
            selected_org_id=None,
            events=[],
            championships=[],
            org_refresh_state=None,
            cache_status=None,
            dump_manifest=None,
            start_date=None,
            end_date=None,
            incremental_backfill_events=DEFAULT_INCREMENTAL_BACKFILL_EVENTS,
            driver_query=None,
            driver_matches=[],
            driver_search_error=None,
            max_events=25,
            active_tab=active_tab,
        )

    try:
        selected_org_id = int(selected_org_id)
    except Exception:
        return redirect(url_for("index"))

    # Load organization details if cached
    org, _ = read_organization_from_store(selected_org_id)
    if not org:
        org = client.get_organization(selected_org_id) or {"id": selected_org_id, "name": f"Organization #{selected_org_id}"}
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

    events_data, events_meta = read_events_from_store(selected_org_id)
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

    championships_data, _ = read_championships_from_store(selected_org_id)
    for champ in championships_data:
        if isinstance(champ, dict):
            championships.append(champ)

    # Get local dump manifest
    dump_manifest = _read_dump_manifest(_dump_root_for_org(selected_org_id))

    # Driver search inline
    driver_query = (request.args.get("q") or "").strip()
    driver_matches = []
    driver_search_error = None
    max_events = max(5, min(safe_int(request.args.get("max_events"), 15), MAX_ORG_EVENTS))

    # Track records parameters (combined onto Dashboard)
    classification = (request.args.get("classification") or "").strip()
    
    limit_events_str = request.args.get("limit_events")
    if limit_events_str is not None:
        session["limit_events"] = limit_events_str
    else:
        limit_events_str = session.get("limit_events", "0")
        
    limit_events = int(limit_events_str) if limit_events_str.isdigit() else 0
    if limit_events == 0:
        limit_events = None
    records = []
    records_error = None
    events_scanned_count = 0
    track_records_ready = bool(org_refresh_state.get("last_refresh_at"))

    driver_filter = (request.args.get("driver_filter") or "").strip()

    if selected_org_id and track_records_ready:
        try:
            records, events_scanned_count, records_error, _ = scan_track_records_from_synced_store(
                org_id=selected_org_id,
                classification=classification,
                start_date=start_date,
                end_date=end_date,
                limit_events=limit_events,
            )
            if driver_filter and records:
                norm_filter = normalize_search_text(driver_filter)
                records = [
                    r for r in records
                    if norm_filter in normalize_search_text(r.get("driver") or "")
                ]
        except Exception as exc:
            records_error = str(exc)

    if selected_org_id and driver_query:
        try:
            for event in events_data[:max_events]:
                if not isinstance(event, dict):
                    continue
                event_id = event.get("id")
                if not event_id:
                    continue
                event_name = event.get("name") or f"Event #{event_id}"
                sessions, _ = read_event_sessions_from_store(int(event_id))
                for sess in sessions:
                    if not isinstance(sess, dict):
                        continue
                    session_id = sess.get("id")
                    if not session_id:
                        continue
                    session_name = sess.get("name") or f"Session #{session_id}"
                    results, _ = read_results_from_store(int(session_id))
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
        records=records,
        track_records_ready=track_records_ready,
        classification=classification,
        driver_filter=driver_filter,
        limit_events=limit_events,
        events_scanned_count=events_scanned_count,
        records_error=records_error,
        active_tab=active_tab,
    )


def track_records_redirect():
    org_id = request.args.get("org_id", "")
    classification = request.args.get("classification", "")
    return redirect(url_for("index", org_id=org_id, classification=classification))


def track_records_export_json():
    """JSON export of the raw announcer-flagged records shown on the Dashboard's
    Track Records tab (same filters/data as scan_track_records_from_synced_store).
    This is the ad-hoc scan of synced session data, not the curated review list --
    see /org/<org_id>/track-records/curated.json for the human-approved records.
    """
    try:
        org_id_int = int(request.args.get("org_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid or missing org_id"}), 400

    classification = (request.args.get("classification") or "").strip()
    driver_filter = (request.args.get("driver_filter") or "").strip()
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    start_date = parse_date_to_comparison(start_date_str)
    end_date = parse_date_to_comparison(end_date_str)

    limit_events_str = request.args.get("limit_events")
    limit_events = int(limit_events_str) if limit_events_str and limit_events_str.isdigit() else 0
    if limit_events == 0:
        limit_events = None

    org_refresh_state = read_org_refresh_state(org_id_int)
    if not org_refresh_state.get("last_refresh_at"):
        return jsonify({
            "org_id": org_id_int,
            "record_count": 0,
            "records": [],
            "error": "Organization has not been synced yet.",
        })

    try:
        records, events_scanned_count, records_error, _ = scan_track_records_from_synced_store(
            org_id=org_id_int,
            classification=classification,
            start_date=start_date,
            end_date=end_date,
            limit_events=limit_events,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if driver_filter and records:
        norm_filter = normalize_search_text(driver_filter)
        records = [r for r in records if norm_filter in normalize_search_text(r.get("driver") or "")]

    return jsonify({
        "org_id": org_id_int,
        "classification": classification or None,
        "driver_filter": driver_filter or None,
        "start_date": start_date_str or None,
        "end_date": end_date_str or None,
        "events_scanned": events_scanned_count,
        "record_count": len(records),
        "records": records,
        "error": records_error,
    })


def org_search_redirect():
    org_id = request.args.get("org_id", "")
    if org_id:
        session["org_id"] = org_id
        return redirect(url_for("index", org_id=org_id))
    return redirect(url_for("index"))


def driver_search_redirect():
    org_id = request.args.get("org_id", "")
    q = request.args.get("q", "")
    return redirect(url_for("index", org_id=org_id, q=q))


def register_routes(app):
    app.add_url_rule("/", "index", index)
    app.add_url_rule("/track-records", "track_records_redirect", track_records_redirect)
    app.add_url_rule("/track-records/export.json", "track_records_export_json", track_records_export_json)
    app.add_url_rule("/org-search", "org_search_redirect", org_search_redirect)
    app.add_url_rule("/driver-search", "driver_search_redirect", driver_search_redirect)
