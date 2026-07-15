import json
from datetime import datetime
from flask import render_template, Response
from app.db import (
    read_event_from_store,
    read_event_sessions_from_store,
    read_session_from_store,
    read_results_from_store,
    read_announcements_from_store,
    read_lap_chart_from_store,
    read_laps_from_store,
)
from app.utils import format_datetime_display, extract_event_datetime
from speedhive.utils.lap_analysis import (
    first_non_empty,
    normalize_result_row,
    build_lap_chart_from_laps,
    safe_int,
    parse_time_value,
    compute_lap_statistics,
)


def event_info(event_id):
    try:
        event_id_int = int(event_id)
        event, _ = read_event_from_store(event_id_int)
        if not event:
            return render_template("event.html", error=f"Event #{event_id} not found", event_id=event_id, event={}), 404

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

        sessions, sessions_meta = read_event_sessions_from_store(event_id_int)
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


def session_results(session_id):
    try:
        session_id_int = int(session_id)
        session, session_meta = read_session_from_store(session_id_int)
        if not session:
            return render_template("results.html", error=f"Session #{session_id} not found", session_id=session_id, session={}), 404
        results, _ = read_results_from_store(session_id_int)
        announcements, _ = read_announcements_from_store(session_id_int)
        lap_chart, _ = read_lap_chart_from_store(session_id_int)
        all_laps, laps_meta = read_laps_from_store(session_id_int)

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
        return render_template("results.html", error=str(exc), session_id=session_id, session={}), 500


def export_session_laps(session_id):
    try:
        session_id_int = int(session_id)
        laps, _ = read_laps_from_store(session_id_int)
        payload = {"session_id": session_id_int, "laps": laps}
        body = json.dumps(payload, indent=2, default=str)
        headers = {"Content-Disposition": f"attachment; filename=session_{session_id_int}_laps.json"}
        return Response(body, mimetype="application/json", headers=headers)
    except Exception as exc:
        return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")


def lap_times(session_id, driver_id):
    try:
        session_id_int = int(session_id)
        session, _ = read_session_from_store(session_id_int)
        if not session:
            return render_template("lap_times.html", error=f"Session #{session_id} not found", session_id=session_id, driver_id=driver_id), 404
        lookup_mode = "cid"
        lookup_value = str(driver_id)
        if ":" in lookup_value:
            prefix, token = lookup_value.split(":", 1)
            if prefix in {"cid", "sn", "pos"}:
                lookup_mode = prefix
                lookup_value = token

        all_laps, _ = read_laps_from_store(session_id_int)
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

        if not driver_laps and lookup_mode in ("cid", "sn"):
            results, _ = read_results_from_store(session_id_int)
            resolved_position = None
            for result in results:
                if not isinstance(result, dict):
                    continue
                if lookup_mode == "cid":
                    r_comp_id = first_non_empty(result.get("competitorId"), result.get("id"), (result.get("competitor") or {}).get("id"))
                    if r_comp_id is not None and str(r_comp_id) == lookup_value:
                        resolved_position = first_non_empty(result.get("position"), result.get("pos"))
                        break
                elif lookup_mode == "sn":
                    r_start_number = first_non_empty(result.get("startNumber"), result.get("transponder"))
                    if r_start_number is not None and str(r_start_number) == lookup_value:
                        resolved_position = first_non_empty(result.get("position"), result.get("pos"))
                        break
            if resolved_position is not None:
                for lap in all_laps:
                    if not isinstance(lap, dict):
                        continue
                    if str(lap.get("position")) == str(resolved_position):
                        driver_laps.append(lap)

        driver_laps.sort(key=lambda x: safe_int(first_non_empty(x.get("lapNumber"), x.get("lap")), 0))
        driver_name = f"Competitor #{driver_id}"
        results, _ = read_results_from_store(session_id_int)
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

        ignore_outliers = request.args.get("ignore_outliers") in ("1", "true", "True")
        if ignore_outliers:
            times = []
            for lap in driver_laps:
                time_str = lap.get("lapTime") or lap.get("lap_time")
                if time_str:
                    sec = parse_time_value(time_str)
                    if sec is not None and sec > 0:
                        times.append((lap, sec))
            from speedhive.utils.lap_analysis import filter_outliers_iqr
            filtered_seconds = filter_outliers_iqr([t[1] for t in times])
            filtered_seconds_pool = list(filtered_seconds)
            for lap, sec in times:
                if sec in filtered_seconds_pool:
                    filtered_seconds_pool.remove(sec)
                    lap["is_outlier"] = False
                else:
                    lap["is_outlier"] = True

        stats = compute_lap_statistics(driver_laps, ignore_outliers=ignore_outliers)
        return render_template(
            "lap_times.html",
            laps=driver_laps,
            stats=stats,
            driver_name=driver_name,
            session_id=session_id,
            driver_id=driver_id,
            ignore_outliers=ignore_outliers,
        )
    except Exception as exc:
        return render_template("lap_times.html", error=str(exc), session_id=session_id, driver_id=driver_id), 500


def register_routes(app):
    app.add_url_rule("/event/<event_id>", "event_info", event_info)
    app.add_url_rule("/session/<session_id>", "session_results", session_results)
    app.add_url_rule("/session/<session_id>/results", "session_results", session_results)
    app.add_url_rule("/session/<session_id>/export-laps.json", "export_session_laps", export_session_laps)
    app.add_url_rule("/session/<session_id>/driver/<driver_id>/laps", "lap_times", lap_times)
