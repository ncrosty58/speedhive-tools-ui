import os
import json
import threading
from datetime import datetime
from flask import request, redirect, url_for, render_template, jsonify, Response
from app import client, storage
from app.tasks import (
    _get_running_track_records_task_for_org,
    _new_track_records_task,
    _run_track_records_sync_task,
    _get_track_records_task,
    TRACK_RECORDS_ROOT,
)
from app.utils import (
    iso_utc,
    utc_now,
    read_json_file,
)
from app.notifications import _send_resend_notification
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


def org_track_records_review(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    payload = track_records.load_candidates(p)
    return render_template(
        "track_records_review.html",
        org_id=org_id_int,
        generated_at=payload.get("generated_at"),
        candidates=payload.get("candidates", []),
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


def org_track_records_curated(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    curated = track_records.load_curated(p)
    records = sorted(curated.get("records", []), key=lambda r: (r.get("classAbbreviation") or "", r.get("date") or ""))
    return render_template(
        "track_records_curated.html",
        org_id=org_id_int,
        curated_date=curated.get("date"),
        records=records,
        notice=request.args.get("notice"),
        error=request.args.get("error"),
    )


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

    curated = track_records.load_curated(p)
    before_count = len(curated.get("records", []))
    curated["records"] = [
        r for r in curated.get("records", [])
        if (r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"), r.get("date")) != identity
    ]
    removed = before_count - len(curated["records"])
    if removed == 0:
        return redirect(url_for("org_track_records_curated", org_id=org_id_int, error="Record not found (already removed?)."))

    curated["date"] = utc_now().strftime("%Y-%m-%d")
    track_records.save_curated(p, curated)

    rejected_payload = track_records.load_rejected(p)
    rejected_payload.setdefault("rejected", []).append({
        "classAbbreviation": identity[0],
        "lapTime": identity[1],
        "driverName": identity[2],
        "date": identity[3],
        "rejected_at": iso_utc(utc_now()),
        "reason": "deleted_from_curated",
    })
    track_records.save_rejected(p, rejected_payload)

    return redirect(url_for("org_track_records_curated", org_id=org_id_int, notice=f"Removed {identity[0]} — {identity[1]} by {identity[2]}."))


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
        org_id=org_id_int,
        records=records,
        notice=request.args.get("notice"),
        error=request.args.get("error"),
    )


def org_track_records_rejected_restore(org_id):
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

    rejected_payload = track_records.load_rejected(p)
    before_count = len(rejected_payload.get("rejected", []))
    rejected_payload["rejected"] = [
        r for r in rejected_payload.get("rejected", [])
        if (r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"), r.get("date")) != identity
    ]
    removed = before_count - len(rejected_payload["rejected"])
    if removed == 0:
        return redirect(url_for("org_track_records_rejected", org_id=org_id_int, error="Record not found (already restored?)."))

    track_records.save_rejected(p, rejected_payload)

    base_notice = f"Restored {identity[0]} — {identity[1]} by {identity[2]}."
    try:
        track_records.run_sync_and_diff(org_id_int, storage, TRACK_RECORDS_ROOT)
        reappeared = any(
            c.get("proposed", {}).get("classAbbreviation") == identity[0]
            and c.get("proposed", {}).get("lapTime") == identity[1]
            and c.get("proposed", {}).get("driverName") == identity[2]
            for c in track_records.load_candidates(p).get("candidates", [])
        )
        notice = (
            f"{base_notice} It's back in the review queue."
            if reappeared
            else f"{base_notice} Rescanned, but it isn't a candidate right now (a curated time may already be faster)."
        )
    except Exception as exc:
        notice = f"{base_notice} Automatic rescan failed ({exc}); it'll reappear on the next scan instead."

    return redirect(url_for("org_track_records_rejected", org_id=org_id_int, notice=notice))


def org_track_records_settings(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return redirect(url_for("index", error="Invalid organization ID."))

    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    config_file = p["dir"] / "config.json"
    alias_map_file = p["alias_map"]

    if request.method == "POST":
        enabled = request.form.get("enabled") == "on"
        de_duplicate = request.form.get("de_duplicate") == "on"
        resend_api_key = request.form.get("resend_api_key", "").strip() or None
        from_email = request.form.get("from_email", "").strip() or None

        to_emails_raw = request.form.get("to_emails", "").strip()
        to_emails = [email.strip() for email in to_emails_raw.split(",") if email.strip()]

        alias_map_json_str = request.form.get("alias_map_json", "").strip()

        try:
            alias_map_data = json.loads(alias_map_json_str)
        except Exception as exc:
            notif_config = read_json_file(config_file) or {}
            notif_data = notif_config.get("notifications", {})
            return render_template(
                "track_records_settings.html",
                org_id=org_id_int,
                notif_config=notif_data,
                alias_map_json=alias_map_json_str,
                error=f"Invalid Alias Map JSON: {str(exc)}"
            )

        notif_config = {
            "notifications": {
                "enabled": enabled,
                "de_duplicate": de_duplicate,
                "resend_api_key": resend_api_key,
                "from_email": from_email,
                "to_emails": to_emails
            }
        }
        track_records.save_json(config_file, notif_config)
        track_records.save_json(alias_map_file, alias_map_data)

        return render_template(
            "track_records_settings.html",
            org_id=org_id_int,
            notif_config=notif_config["notifications"],
            alias_map_json=json.dumps(alias_map_data, indent=2, ensure_ascii=False),
            notice="Configuration saved successfully."
        )

    notif_config = read_json_file(config_file) or {}
    notif_data = notif_config.get("notifications", {
        "enabled": True,
        "de_duplicate": True,
        "resend_api_key": None,
        "from_email": None,
        "to_emails": []
    })

    alias_map_data = read_json_file(alias_map_file) or {
        "aliases": {},
        "always_review": []
    }
    alias_map_json_str = json.dumps(alias_map_data, indent=2, ensure_ascii=False)

    return render_template(
        "track_records_settings.html",
        org_id=org_id_int,
        notif_config=notif_data,
        alias_map_json=alias_map_json_str
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
        org_id=org_id_int,
        tasks=tasks
    )


def org_track_records_json(org_id):
    try:
        org_id_int = int(org_id)
    except ValueError:
        return jsonify({"error": "Invalid org_id"}), 400
    p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    curated = track_records.load_curated(p)
    body = json.dumps(curated, ensure_ascii=False)
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

    resend_api_key = body.get("resend_api_key") or os.environ.get("RESEND_API_KEY")
    from_email = body.get("from_email") or os.environ.get("NOTIFICATION_FROM_EMAIL")

    to_emails = body.get("to_emails")
    if not to_emails:
        env_to = os.environ.get("NOTIFICATION_TO_EMAILS")
        if env_to:
            if env_to.strip().startswith("["):
                try:
                    to_emails = json.loads(env_to)
                except Exception:
                    to_emails = [email.strip() for email in env_to.split(",") if email.strip()]
            else:
                to_emails = [email.strip() for email in env_to.split(",") if email.strip()]

    if isinstance(to_emails, str):
        to_emails = [email.strip() for email in to_emails.split(",") if email.strip()]

    if not resend_api_key:
        return jsonify({"error": "Missing resend_api_key (neither provided in POST body nor environment)"}), 400
    if not from_email:
        return jsonify({"error": "Missing from_email (neither provided in POST body nor environment)"}), 400
    if not to_emails:
        return jsonify({"error": "Missing to_emails (neither provided in POST body nor environment)"}), 400

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
    app.add_url_rule("/org/<org_id>/track-records/review", "org_track_records_review", org_track_records_review)
    app.add_url_rule("/org/<org_id>/track-records/review/approve", "org_track_records_review_apply", org_track_records_review_apply, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/review/apply", "org_track_records_review_apply", org_track_records_review_apply, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/review/reject", "org_track_records_review_reject", org_track_records_review_reject, methods=["POST"])
    app.add_url_rule("/org/<org_id>/track-records/curated", "org_track_records_curated", org_track_records_curated)
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
