import os
import json
import threading
from pathlib import Path
from datetime import datetime
from flask import request, redirect, url_for, render_template, jsonify, Response
from app import client, storage
from app.db import get_org_view
from app.tasks import (
    _get_running_track_records_task_for_org,
    _new_track_records_task,
    _run_track_records_sync_task,
    _run_track_records_scan_only_task,
    _get_track_records_task,
    _trigger_track_records_rescan,
    TRACK_RECORDS_ROOT,
)
from app.utils import (
    iso_utc,
    utc_now,
    read_json_file,
)
from app.notifications import _send_resend_notification
from speedhive.settings import (
    get_org_env_var,
    get_org_env_var_override,
    get_org_env_var_with_source,
)
from speedhive.workflows.track_records import curation as track_records
from speedhive.exporters.export_curated_track_records import export_curated_track_records_ndjson
from speedhive.workflows.track_records.import_curated import import_curated_track_records_ndjson


def _track_records_candidate_identity(candidate):
    proposed = candidate.get("proposed", {})
    return (
        proposed.get("classAbbreviation"),
        proposed.get("lapTime"),
        proposed.get("driverName"),
        proposed.get("date"),
    )


def org_track_records_status(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return jsonify({"error": "Invalid org_id"}), 400
    status = track_records.get_cache_status(org_id_int, storage, TRACK_RECORDS_ROOT, client=client)
    running_task = _get_running_track_records_task_for_org(org_id_int)
    status["task_running"] = running_task is not None
    status["task_id"] = running_task["task_id"] if running_task else None
    return jsonify(status)


def org_track_records_sync(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return jsonify({"error": "Invalid org_id"}), 400

    body = request.get_json(silent=True) or {}
    force = request.args.get("force") in ("1", "true", "True") or bool(body.get("force"))
    full = request.args.get("full") in ("1", "true", "True") or bool(body.get("full"))

    running_task = _get_running_track_records_task_for_org(org_id_int)
    if running_task:
        return jsonify({"task_id": running_task["task_id"], "org_id": org_id_int, "already_running": True})

    status = track_records.get_cache_status(org_id_int, storage, TRACK_RECORDS_ROOT, client=client)
    if not force and not status["needs_sync"]:
        age_str = f"{status['age_hours']:.1f}h" if status.get('age_hours') is not None else "unknown age"
        return jsonify({
            "skipped": True,
            "reason": f"refresh skipped (needs_sync is false, cache is {age_str} old, source: {status.get('check_source')})",
            "status": status,
        })

    task_id = _new_track_records_task(org_id_int)
    t = threading.Thread(target=_run_track_records_sync_task, args=(task_id, org_id_int, full, force), daemon=True)
    t.start()
    return jsonify({"task_id": task_id, "org_id": org_id_int})


def org_track_records_sync_status(org_id, task_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return jsonify({"error": "Invalid org_id"}), 400
    task = _get_track_records_task(org_id_int, task_id)
    if task is None:
        return jsonify({"error": "Unknown task_id"}), 404
    return jsonify(task)


def org_track_records_scan_only(org_id):
    """Diff the already-synced cache against the curated list. Never contacts
    Speedhive -- use Sync from Speedhive (refresh_org_start) first if the
    cache needs updating. Distinct from org_track_records_sync, which is the
    public/automation entrypoint that syncs-then-scans in one call.
    """
    try:
        org_id_int = int(org_id)
    except ValueError:
        return jsonify({"error": "Invalid org_id"}), 400

    running_task = _get_running_track_records_task_for_org(org_id_int)
    if running_task:
        return jsonify({"task_id": running_task["task_id"], "org_id": org_id_int, "already_running": True})

    task_id = _new_track_records_task(org_id_int)
    t = threading.Thread(target=_run_track_records_scan_only_task, args=(task_id, org_id_int), daemon=True)
    t.start()
    return jsonify({"task_id": task_id, "org_id": org_id_int})


def org_track_records_review(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    payload = track_records.load_candidates(p)
    return render_template(
        "track_records_review.html",
        org=get_org_view(org_id_int),
        org_id=org_id_int,
        active_tab="track_records",
        active_track_tab="review",
        generated_at=payload.get("generated_at"),
        candidates=payload.get("candidates", []),
        pending_count=len(payload.get("candidates", [])),
        notice=request.args.get("notice"),
        error=request.args.get("error"),
    )


def org_track_records_review_apply(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)

    identity = (
        request.form.get("orig_classAbbreviation"),
        request.form.get("orig_lapTime"),
        request.form.get("orig_driverName"),
        request.form.get("orig_date"),
    )
    final_record = {
        "classAbbreviation": (request.form.get("classAbbreviation") or "").strip(),
        "lapTime": (request.form.get("lapTime") or "").strip(),
        "driverName": (request.form.get("driverName") or "").strip(),
        "marque": (request.form.get("marque") or "").strip() or None,
        "date": (request.form.get("date") or "").strip(),
        "addedAt": iso_utc(utc_now()),
        "source": "speedhive",
    }
    if not final_record["classAbbreviation"] or not final_record["lapTime"] or not final_record["date"]:
        return redirect(url_for("org_track_records_review", org_id=org_id_int, error="Class, lap time, and date are required to approve a candidate."))

    curated = track_records.load_curated(p)
    curated["records"].append(final_record)
    curated["date"] = utc_now().strftime("%Y-%m-%d")
    track_records.save_curated(p, curated)

    payload = track_records.load_candidates(p)
    payload["candidates"] = [c for c in payload.get("candidates", []) if _track_records_candidate_identity(c) != identity]
    track_records.save_candidates(p, payload)

    _trigger_track_records_rescan(org_id_int)
    return redirect(url_for("org_track_records_review", org_id=org_id_int, notice=f"Approved {final_record['classAbbreviation']} — {final_record['lapTime']} by {final_record['driverName']}."))


def org_track_records_review_reject(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)

    identity = (
        request.form.get("orig_classAbbreviation"),
        request.form.get("orig_lapTime"),
        request.form.get("orig_driverName"),
        request.form.get("orig_date"),
    )

    rejected_payload = track_records.load_rejected(p)
    rejected_payload.setdefault("rejected", []).append({
        "classAbbreviation": identity[0],
        "lapTime": identity[1],
        "driverName": identity[2],
        "date": identity[3],
        "rejected_at": iso_utc(utc_now()),
    })
    track_records.save_rejected(p, rejected_payload)

    payload = track_records.load_candidates(p)
    payload["candidates"] = [c for c in payload.get("candidates", []) if _track_records_candidate_identity(c) != identity]
    track_records.save_candidates(p, payload)

    return redirect(url_for("org_track_records_review", org_id=org_id_int, notice=f"Rejected {identity[0]} — {identity[1]} by {identity[2]}."))


def org_track_records_review_approve_all(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)

    result = track_records.approve_all_candidates(p)
    notice = f"Approved {result['approved']} record(s)."
    if result["skipped"]:
        notice += f" {result['skipped']} unmapped-classification candidate(s) still need manual review."

    _trigger_track_records_rescan(org_id_int)
    return redirect(url_for("org_track_records_review", org_id=org_id_int, notice=notice))


def org_track_records_curated_dedupe(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)

    result = track_records.dedupe_curated_speedhive_additions(p)
    return jsonify(result)


def org_track_records_curated(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    curated = track_records.load_curated(p)
    records = sorted(curated.get("records", []), key=lambda r: (r.get("classAbbreviation") or "", r.get("date") or ""))

    pending_count = len(track_records.load_candidates(p).get("candidates", []))

    return render_template(
        "track_records_curated.html",
        org=get_org_view(org_id_int),
        org_id=org_id_int,
        active_tab="track_records",
        active_track_tab="curated",
        curated_date=curated.get("date"),
        records=records,
        pending_count=pending_count,
        notice=request.args.get("notice"),
        error=request.args.get("error"),
    )


def org_track_records_curated_add(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)

    record = track_records.add_curated_record(p, request.form)
    if record is None:
        return redirect(url_for("org_track_records_curated", org_id=org_id_int, error="Class, lap time, and date are required to add a record."))

    _trigger_track_records_rescan(org_id_int)
    return redirect(url_for("org_track_records_curated", org_id=org_id_int, notice=f"Added {record['classAbbreviation']} — {record['lapTime']} by {record['driverName'] or 'unknown driver'}."))


def org_track_records_curated_edit(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)

    orig_identity = (
        request.form.get("orig_classAbbreviation"),
        request.form.get("orig_lapTime"),
        request.form.get("orig_driverName"),
        request.form.get("orig_date"),
    )

    record = track_records.edit_curated_record(p, orig_identity, request.form)
    if record is None:
        return redirect(url_for("org_track_records_curated", org_id=org_id_int, error="Record not found, or class/lap time/date missing."))

    _trigger_track_records_rescan(org_id_int)
    return redirect(url_for("org_track_records_curated", org_id=org_id_int, notice=f"Updated {record['classAbbreviation']} — {record['lapTime']} by {record['driverName'] or 'unknown driver'}."))


def org_track_records_curated_delete(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)

    identity = (
        request.form.get("classAbbreviation"),
        request.form.get("lapTime"),
        request.form.get("driverName"),
        request.form.get("date"),
    )

    result = track_records.delete_curated_record(p, identity)
    if not result["found"]:
        return redirect(url_for("org_track_records_curated", org_id=org_id_int, error="Record not found (already removed?)."))

    if result["permanent"]:
        notice = f"Permanently deleted {identity[0]} — {identity[1]} by {identity[2]}."
    else:
        notice = f"Removed {identity[0]} — {identity[1]} by {identity[2]}. Blocked from future scans."

    _trigger_track_records_rescan(org_id_int)
    return redirect(url_for("org_track_records_curated", org_id=org_id_int, notice=notice))


def org_track_records_export_ndjson(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return jsonify({"error": "Invalid org_id"}), 400
    body = export_curated_track_records_ndjson(org_id_int, TRACK_RECORDS_ROOT)
    headers = {"Content-Disposition": f"attachment; filename=org_{org_id_int}_track_records.ndjson"}
    return Response(body, mimetype="application/x-ndjson", headers=headers)


def org_track_records_import(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return redirect(url_for("org_track_records_curated", org_id=org_id_int, error="No file selected."))
    replace = request.form.get("mode") == "replace"

    try:
        text = upload.read().decode("utf-8")
    except UnicodeDecodeError:
        return redirect(url_for("org_track_records_curated", org_id=org_id_int, error="File is not valid UTF-8 text."))

    try:
        notice = import_curated_track_records_ndjson(
            org_id_int,
            TRACK_RECORDS_ROOT,
            text,
            replace=replace,
        )
    except ValueError as exc:
        return redirect(url_for("org_track_records_curated", org_id=org_id_int, error=str(exc)))

    _trigger_track_records_rescan(org_id_int)
    return redirect(url_for("org_track_records_curated", org_id=org_id_int, notice=notice))


def org_track_records_rejected(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    rejected_payload = track_records.load_rejected(p)
    records = rejected_payload.get("rejected", [])
    records = sorted(records, key=lambda r: (r.get("classAbbreviation") or "", r.get("date") or ""))
    return render_template(
        "track_records_rejected.html",
        org=get_org_view(org_id_int),
        org_id=org_id_int,
        active_tab="track_records",
        active_track_tab="rejected",
        records=records,
        notice=request.args.get("notice"),
        error=request.args.get("error"),
    )


def org_track_records_rejected_restore(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))

    identity = (
        request.form.get("classAbbreviation"),
        request.form.get("lapTime"),
        request.form.get("driverName"),
        request.form.get("date"),
    )

    result = track_records.restore_rejected_record(org_id_int, storage, TRACK_RECORDS_ROOT, identity)
    if not result["found"]:
        return redirect(url_for("org_track_records_rejected", org_id=org_id_int, error="Record not found (already restored?)."))

    base_notice = f"Restored {identity[0]} — {identity[1]} by {identity[2]}."
    if result.get("rescan_error"):
        notice = f"{base_notice} Automatic rescan failed ({result['rescan_error']}); it'll reappear on the next scan instead."
    elif result.get("reappeared"):
        notice = f"{base_notice} It's back in the review queue."
    else:
        notice = f"{base_notice} Rescanned, but it isn't a candidate right now (a curated time may already be faster)."

    return redirect(url_for("org_track_records_rejected", org_id=org_id_int, notice=notice))


def _mask_secret(value):
    if not value:
        return None
    if len(value) <= 4:
        return "••••••••••••"
    return f"••••••••••••{value[-4:]}"


def _get_setting_info(name: str, org_id: int, is_secret: bool = False, default_fallback: str = None):
    override = get_org_env_var_override(name, org_id)
    global_val = os.environ.get(name)
    effective_val, source = get_org_env_var_with_source(name, org_id)
    if not effective_val and default_fallback:
        effective_val = default_fallback
        source = "code_default"

    return {
        "name": name,
        "override": override,
        "global_val": global_val,
        "has_global": bool(global_val),
        "effective_val": effective_val,
        "source": source,
        "display_override": _mask_secret(override) if is_secret else override,
        "display_global": _mask_secret(global_val) if is_secret else global_val,
        "display_effective": _mask_secret(effective_val) if is_secret else effective_val,
    }


def _read_org_settings(org_id_int):
    from app import data_root
    settings_file = Path(data_root) / "orgs" / str(org_id_int) / "settings.json"
    config_data = read_json_file(settings_file) or {}
    toggles = config_data.get("notifications", {"enabled": True, "de_duplicate": True})
    parsing_data = config_data.get("parsing", {"engine": "regex"})
    stats_data = config_data.get("stats", {"min_laps": 20})

    env_settings = {
        "RESEND_API_KEY": _get_setting_info("RESEND_API_KEY", org_id_int, is_secret=True),
        "NOTIFICATION_FROM_EMAIL": _get_setting_info("NOTIFICATION_FROM_EMAIL", org_id_int),
        "NOTIFICATION_TO_EMAILS": _get_setting_info("NOTIFICATION_TO_EMAILS", org_id_int),
        "GEMINI_API_KEY": _get_setting_info("GEMINI_API_KEY", org_id_int, is_secret=True),
        "GEMINI_MODEL": _get_setting_info("GEMINI_MODEL", org_id_int),
    }

    notif_data = {
        "enabled": toggles.get("enabled", True),
        "de_duplicate": toggles.get("de_duplicate", True),
    }
    return notif_data, parsing_data, stats_data, env_settings


def org_track_records_settings(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))

    from app import data_root
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    settings_file = Path(data_root) / "orgs" / str(org_id_int) / "settings.json"
    alias_map_file = p["alias_map"]

    if request.method == "POST":
        enabled = request.form.get("enabled") == "on"
        de_duplicate = request.form.get("de_duplicate") == "on"
        resend_api_key = request.form.get("resend_api_key", "").strip() or None
        from_email = request.form.get("from_email", "").strip() or None

        to_emails_raw = request.form.get("to_emails", "").strip()
        to_emails = [email.strip() for email in to_emails_raw.split(",") if email.strip()]

        gemini_api_key = request.form.get("gemini_api_key", "").strip() or None
        gemini_model = request.form.get("gemini_model", "").strip() or None

        alias_map_json_str = request.form.get("alias_map_json", "").strip()
        parser_engine = "llm" if request.form.get("parser_engine") == "llm" else "regex"
        try:
            min_laps = int(request.form.get("stats_min_laps") or "20")
        except ValueError:
            min_laps = 20

        try:
            alias_map_data = json.loads(alias_map_json_str)
        except Exception as exc:
            notif_data, parsing_data, stats_data, env_settings = _read_org_settings(org_id_int)
            return render_template(
                "track_records_settings.html",
                org=get_org_view(org_id_int),
                org_id=org_id_int,
                active_tab="settings",
                active_settings_tab="general",
                notif_config=notif_data,
                alias_map_json=alias_map_json_str,
                parsing_config=parsing_data,
                stats_config=stats_data,
                env_settings=env_settings,
                error=f"Invalid Alias Map JSON: {str(exc)}"
            )

        # Safely preserve other keys in settings.json while updating toggles/overrides
        config_data = read_json_file(settings_file) or {}
        config_data["notifications"] = {"enabled": enabled, "de_duplicate": de_duplicate}
        config_data["parsing"] = {"engine": parser_engine}
        config_data["stats"] = {"min_laps": min_laps}

        if "overrides" not in config_data:
            config_data["overrides"] = {}

        for key, val in [
            ("RESEND_API_KEY", resend_api_key),
            ("NOTIFICATION_FROM_EMAIL", from_email),
            ("NOTIFICATION_TO_EMAILS", ",".join(to_emails) if to_emails else None),
            ("GEMINI_API_KEY", gemini_api_key),
            ("GEMINI_MODEL", gemini_model),
        ]:
            if val:
                if val.startswith("••••"):
                    continue
                config_data["overrides"][key] = val
                os.environ[f"{key}_{org_id_int}"] = val
            else:
                config_data["overrides"].pop(key, None)
                os.environ.pop(f"{key}_{org_id_int}", None)

        if not config_data["overrides"]:
            config_data.pop("overrides", None)

        track_records.save_json(settings_file, config_data)
        track_records.save_json(alias_map_file, alias_map_data)

        notif_data, parsing_data, stats_data, env_settings = _read_org_settings(org_id_int)
        return render_template(
            "track_records_settings.html",
            org=get_org_view(org_id_int),
            org_id=org_id_int,
            active_tab="settings",
            active_settings_tab="general",
            notif_config=notif_data,
            alias_map_json=json.dumps(alias_map_data, indent=2, ensure_ascii=False),
            parsing_config=parsing_data,
            stats_config=stats_data,
            env_settings=env_settings,
            notice="Configuration saved successfully."
        )

    notif_data, parsing_data, stats_data, env_settings = _read_org_settings(org_id_int)
    alias_map_data = read_json_file(alias_map_file) or {
        "aliases": {},
        "always_review": []
    }
    alias_map_json_str = json.dumps(alias_map_data, indent=2, ensure_ascii=False)

    return render_template(
        "track_records_settings.html",
        org=get_org_view(org_id_int),
        org_id=org_id_int,
        active_tab="settings",
        active_settings_tab="general",
        notif_config=notif_data,
        alias_map_json=alias_map_json_str,
        parsing_config=parsing_data,
        stats_config=stats_data,
        env_settings=env_settings
    )


def org_track_records_history(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))

    tasks = []
    try:
        with storage.connect() as conn:
            cursor = conn.execute(
                "SELECT task_id, status, payload, started_at, finished_at FROM background_tasks "
                "WHERE org_id = ? AND task_type = 'track_records' "
                "ORDER BY started_at DESC",
                (org_id_int,)
            )
            for row in cursor.fetchall():
                task_data = {
                    "task_id": row["task_id"],
                    "org_id": org_id_int,
                    "task_type": "track_records",
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                }
                if row["payload"]:
                    try:
                        task_data.update(json.loads(row["payload"]))
                    except Exception:
                        pass

                duration_str = "—"
                if task_data.get("started_at") and task_data.get("finished_at"):
                    try:
                        start_t = datetime.fromisoformat(task_data["started_at"].replace("Z", "+00:00"))
                        finish_t = datetime.fromisoformat(task_data["finished_at"].replace("Z", "+00:00"))
                        diff = finish_t - start_t
                        total_seconds = int(diff.total_seconds())
                        mins = total_seconds // 60
                        secs = total_seconds % 60
                        if mins > 0:
                            duration_str = f"{mins}m {secs}s"
                        else:
                            duration_str = f"{secs}s"
                    except Exception:
                        pass

                task_data["duration_str"] = duration_str
                task_data["raw_json"] = json.dumps(task_data, indent=2, ensure_ascii=False)
                tasks.append(task_data)
    except Exception:
        pass

    return render_template(
        "track_records_history.html",
        org=get_org_view(org_id_int),
        org_id=org_id_int,
        active_tab="track_records",
        active_track_tab="history",
        tasks=tasks
    )


def org_track_records_json(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return jsonify({"error": "Invalid org_id"}), 400
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    curated = track_records.load_curated(p)
    body = json.dumps(curated, ensure_ascii=False, indent=2)
    resp = Response(body, mimetype="application/json")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET"
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


def org_track_records_notify(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return jsonify({"error": "Invalid org_id"}), 400

    body = request.get_json(silent=True) or {}

    env_secret = os.environ.get("SYNC_SECRET")
    secret = request.args.get("secret") or body.get("secret")
    if env_secret and secret != env_secret:
        return jsonify({"error": "Unauthorized"}), 401

    resend_api_key = body.get("resend_api_key") or get_org_env_var("RESEND_API_KEY", org_id_int)
    from_email = body.get("from_email") or get_org_env_var("NOTIFICATION_FROM_EMAIL", org_id_int)

    to_emails = body.get("to_emails")
    if not to_emails:
        env_to = get_org_env_var("NOTIFICATION_TO_EMAILS", org_id_int)
        if env_to:
            to_emails = [email.strip() for email in env_to.split(",") if email.strip()]

    if isinstance(to_emails, str):
        to_emails = [email.strip() for email in to_emails.split(",") if email.strip()]

    if not resend_api_key:
        return jsonify({"error": "Missing resend_api_key (neither provided in POST body nor organization config)"}), 400
    if not from_email:
        return jsonify({"error": "Missing from_email (neither provided in POST body nor organization config)"}), 400
    if not to_emails:
        return jsonify({"error": "Missing to_emails (neither provided in POST body nor organization config)"}), 400

    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)

    try:
        candidates_data = track_records.load_candidates(p)
    except Exception as exc:
        return jsonify({"error": f"Failed to read candidates file: {str(exc)}"}), 500

    candidates = candidates_data.get("candidates", [])
    if not candidates:
        return jsonify({"skipped": True, "reason": "No pending candidates to notify about"}), 200

    try:
        resend_response = _send_resend_notification(org_id_int, candidates, resend_api_key, from_email, to_emails)
        
        fingerprint_list = sorted([
            f"{c.get('type')}:{c.get('proposed', {}).get('classAbbreviation')}:{c.get('proposed', {}).get('lapTime')}:{c.get('proposed', {}).get('date')}"
            for c in candidates
        ])
        fingerprint = ",".join(fingerprint_list)
        candidates_data["last_notified_fingerprint"] = fingerprint
        track_records.save_candidates(p, candidates_data)

        return jsonify({"success": True, "resend_response": resend_response})
    except Exception as exc:
        return jsonify({"error": f"Failed to send email via Resend: {str(exc)}"}), 500


def register_routes(app):
    app.add_url_rule("/org/<org_id>/track-records/update/status", "org_track_records_status", org_track_records_status)
    app.add_url_rule("/org/<org_id>/track-records/update", "org_track_records_sync", org_track_records_sync, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/update/<task_id>", "org_track_records_sync_status", org_track_records_sync_status)
    app.add_url_rule("/org/<org_id>/track-records/scan", "org_track_records_scan_only", org_track_records_scan_only, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/review", "org_track_records_review", org_track_records_review)
    app.add_url_rule("/org/<org_id>/track-records/review/approve", "org_track_records_review_apply", org_track_records_review_apply, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/review/apply", "org_track_records_review_apply", org_track_records_review_apply, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/review/reject", "org_track_records_review_reject", org_track_records_review_reject, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/review/approve-all", "org_track_records_review_approve_all", org_track_records_review_approve_all, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/curated/dedupe", "org_track_records_curated_dedupe", org_track_records_curated_dedupe, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/curated", "org_track_records_curated", org_track_records_curated)
    app.add_url_rule("/org/<org_id>/track-records/curated/add", "org_track_records_curated_add", org_track_records_curated_add, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/curated/edit", "org_track_records_curated_edit", org_track_records_curated_edit, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/curated/remove", "org_track_records_curated_delete", org_track_records_curated_delete, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/curated/delete", "org_track_records_curated_delete", org_track_records_curated_delete, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/curated.ndjson", "org_track_records_export_ndjson", org_track_records_export_ndjson)
    app.add_url_rule("/org/<org_id>/track-records/curated/import", "org_track_records_import", org_track_records_import, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/rejected", "org_track_records_rejected", org_track_records_rejected)
    app.add_url_rule("/org/<org_id>/track-records/rejected/restore", "org_track_records_rejected_restore", org_track_records_rejected_restore, methods=["POST"])
    app.add_url_rule("/org/<org_id>/settings", "org_track_records_settings", org_track_records_settings, methods=["GET", "POST"])
    app.add_url_rule("/org/<org_id>/track-records/history", "org_track_records_history", org_track_records_history)
    app.add_url_rule("/org/<org_id>/track-records/curated.json", "org_track_records_json", org_track_records_json)
    app.add_url_rule("/org/<org_id>/track-records.json", "org_track_records_json", org_track_records_json)
    app.add_url_rule("/org/<org_id>/track-records/notify", "org_track_records_notify", org_track_records_notify, methods=["POST"])
