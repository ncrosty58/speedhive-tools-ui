from datetime import datetime
from flask import render_template, request, session, redirect, url_for, jsonify
from app import client
from app.db import (
    list_stored_orgs,
    get_org_view,
    read_events_from_store,
    read_championships_from_store,
    read_org_refresh_state,
    scan_track_records_from_synced_store,
    read_event_sessions_from_store,
    read_results_from_store,
)
from app.utils import (
    parse_date_to_comparison,
    extract_event_datetime,
    format_datetime_display,
)
from app.tasks import MAX_ORG_EVENTS
from speedhive.utils.lap_analysis import (
    first_non_empty,
    normalize_search_text,
    name_match_score,
    normalize_result_row,
    safe_int,
)


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
            org_list=org_list,
            org=None,
            events=[],
            championships=[],
            start_date=None,
            end_date=None,
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

    org_view = get_org_view(selected_org_id, client=client)

    # Date filtering for events list
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    start_date = parse_date_to_comparison(start_date_str)
    end_date = parse_date_to_comparison(end_date_str)

    events_data, _ = read_events_from_store(selected_org_id)
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

    championships = []
    championships_data, _ = read_championships_from_store(selected_org_id)
    for champ in championships_data:
        if isinstance(champ, dict):
            championships.append(champ)

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
        org_list=org_list,
        org=org_view,
        events=events,
        championships=championships,
        start_date=start_date_str,
        end_date=end_date_str,
        driver_query=driver_query,
        driver_matches=driver_matches,
        driver_search_error=driver_search_error,
        max_events=max_events,
        active_tab=active_tab,
    )


def lap_records(org_id):
    """Live, filterable "fastest lap per class" browser, computed on-the-fly
    from the synced session cache. Distinct from the curated Track Records
    list -- see app/routes/track_records.py and track_records/curation.py.
    """
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    org_view = get_org_view(org_id_int, client=client)
    org_refresh_state = read_org_refresh_state(org_id_int)
    ready = bool(org_refresh_state.get("last_refresh_at"))

    classification = (request.args.get("classification") or "").strip()
    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    start_date = parse_date_to_comparison(start_date_str)
    end_date = parse_date_to_comparison(end_date_str)
    driver_filter = (request.args.get("driver_filter") or "").strip()

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

    if ready:
        try:
            records, events_scanned_count, records_error, _ = scan_track_records_from_synced_store(
                org_id=org_id_int,
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

    return render_template(
        "lap_records.html",
        org=org_view,
        org_id=org_id_int,
        active_tab="track_records",
        active_track_tab="live",
        records=records,
        records_ready=ready,
        classification=classification,
        driver_filter=driver_filter,
        start_date=start_date_str,
        end_date=end_date_str,
        limit_events=limit_events,
        events_scanned_count=events_scanned_count,
        records_error=records_error,
    )


def track_records_redirect():
    org_id = (request.args.get("org_id") or "").strip()
    classification = request.args.get("classification", "")
    if not org_id:
        return redirect(url_for("index"))
    return redirect(url_for("lap_records", org_id=org_id, classification=classification))


def track_records_export_json():
    """JSON export of the raw announcer-flagged records shown on the Lap
    Records tab (same filters/data as scan_track_records_from_synced_store).
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
    app.add_url_rule("/org/<org_id>/lap-records", "lap_records", lap_records)
    app.add_url_rule("/track-records", "track_records_redirect", track_records_redirect)
    app.add_url_rule("/track-records/export.json", "track_records_export_json", track_records_export_json)
    app.add_url_rule("/org-search", "org_search_redirect", org_search_redirect)
    app.add_url_rule("/driver-search", "driver_search_redirect", driver_search_redirect)
