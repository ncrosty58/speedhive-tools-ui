import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from flask import request, redirect, url_for, render_template, jsonify, Response, send_file, after_this_request
from app import client, storage
from app.tasks import (
    _get_running_task_for_org,
    _new_task,
    _run_refresh_task,
    _get_task,
    _update_task,
    MAX_ORG_EVENTS,
)
from app.db import (
    _dump_root_for_org,
    _resolve_dump_dir_for_org,
    _delete_latest_dump_contents,
    _prune_empty_dump_roots,
    save_org_dump,
    format_saved_at_display,
    _list_org_dumps,
)
from speedhive.workflows.refresh_org_cache import refresh_org_cache as refresh_org_cache_bundle
from speedhive.exporters.export_lap_records import get_lap_records
from speedhive.ndjson import dumps_ndjson_record
from speedhive.utils.lap_analysis import safe_int
from speedhive.storage import SpeedhiveStorage

DEFAULT_INCREMENTAL_BACKFILL_EVENTS = int(os.environ.get("SPEEDHIVE_INCREMENTAL_BACKFILL_EVENTS", "3"))


def org_add():
    lookup = None
    error = None
    org_id_input = ""
    if request.method == "POST":
        org_id_input = (request.form.get("org_id") or "").strip()
        if not org_id_input.isdigit():
            error = "Enter a numeric Speedhive organization ID."
        else:
            try:
                found = client.get_organization(int(org_id_input))
            except Exception as exc:
                found = None
                error = f"Speedhive lookup failed: {exc}"
            if found:
                lookup = {"id": int(org_id_input), "name": found.get("name") or f"Organization #{org_id_input}"}
            elif not error:
                error = f"No Speedhive organization found with ID {org_id_input}."
    return render_template("org_add.html", lookup=lookup, error=error, org_id_input=org_id_input)


def org_details(org_id):
    return redirect(url_for("index", org_id=org_id, **request.args))


def refresh_org(org_id):
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

    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        running_task = _get_running_task_for_org(org_id_int)
        if running_task:
            return jsonify({
                "task_id": running_task["task_id"],
                "org_id": org_id_int,
                "mode": running_task["mode"],
                "already_running": True
            })
        task_id = _new_task(org_id_int, mode)
        t = threading.Thread(
            target=_run_refresh_task,
            args=(task_id, org_id_int, mode, backfill_events),
            daemon=True,
        )
        t.start()
        return jsonify({"task_id": task_id, "org_id": org_id_int, "mode": mode})

    # Legacy synchronous path (fallback for no-JS)
    running_task = _get_running_task_for_org(org_id_int)
    if running_task:
        return redirect(url_for("org_details", org_id=org_id_int, notice="A refresh is already running for this organization."))

    try:
        summary = refresh_org_cache_bundle(
            client=client,
            org_id=org_id_int,
            mode=mode,
            max_events=MAX_ORG_EVENTS,
            recent_backfill_events=backfill_events if mode == "incremental" else 0,
            cleanup_on_full=True,
            storage=storage,
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


def refresh_org_start(org_id):
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

    running_task = _get_running_task_for_org(org_id_int)
    if running_task:
        return jsonify({
            "task_id": running_task["task_id"],
            "org_id": org_id_int,
            "mode": running_task["mode"],
            "already_running": True
        })

    task_id = _new_task(org_id_int, mode)
    t = threading.Thread(
        target=_run_refresh_task,
        args=(task_id, org_id_int, mode, backfill_events),
        daemon=True,
    )
    t.start()
    return jsonify({"task_id": task_id, "org_id": org_id_int, "mode": mode})


def clear_org_cache_files(org_id: int):
    global storage
    from app import DB_PATH
    from app.tasks import DATA_ROOT
    
    legacy_cache_root = DATA_ROOT / "cache"
    dumps_root = DATA_ROOT / "saved_dumps"

    def _registered_org_ids() -> set[int]:
        return {
            safe_int(row.get("org_id"), None)
            for row in storage.list_organizations()
            if safe_int(row.get("org_id"), None) is not None
        }

    def _wipe_dir_contents(root: Path) -> None:
        if not root.exists():
            return
        for child in root.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except Exception:
                pass

    dump_dir = dumps_root / str(org_id)

    db_path = dumps_root / str(org_id) / f"laps_{org_id}.db"
    if db_path.exists():
        try:
            db_path.unlink()
        except Exception:
            pass

    if dump_dir.exists():
        try:
            shutil.rmtree(dump_dir, ignore_errors=True)
        except Exception:
            pass

    # Wipe task state records from database
    try:
        with storage.connect() as conn:
            conn.execute("DELETE FROM background_tasks WHERE org_id = ?", (org_id,))
            conn.commit()
    except Exception:
        pass

    storage.delete_org(org_id)

    remaining_org_ids = _registered_org_ids()
    if not remaining_org_ids:
        try:
            DB_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        import app as app_module
        app_module.storage = SpeedhiveStorage(DB_PATH)
        storage = app_module.storage
        _wipe_dir_contents(legacy_cache_root)
        _wipe_dir_contents(dumps_root)
        return
    _wipe_dir_contents(legacy_cache_root)


def clear_cache(org_id):
    try:
        org_id_int = int(org_id)
        clear_org_cache_files(org_id_int)
        return redirect(url_for("index", notice="Organization removed from the local store successfully."))
    except Exception as exc:
        return redirect(url_for("index", error=f"Failed to delete local data: {exc}"))


def refresh_status(task_id):
    task = _get_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    task.pop("summary", None)
    return jsonify(task)


def refresh_stop(task_id):
    task = _get_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    _update_task(task_id, status="stopping", stop_requested=True)
    return jsonify({"task_id": task_id, "stop_requested": True})


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
            f"Exported offline dump to {summary['path']} with {summary['events_count']} events, "
            f"{summary['sessions_count']} sessions, {summary['laps_records_count']} lap-record blocks."
        )
        return redirect(url_for("org_details", org_id=org_id_int, notice=notice))
    except Exception as exc:
        return redirect(url_for("org_details", org_id=org_id_int, error=f"Dump generation failed: {exc}"))


def download_local_dump(org_id, dump_key: str = "latest"):
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))
    
    dump_root = _dump_root_for_org(org_id_int)
    dump_dir = _resolve_dump_dir_for_org(org_id_int, dump_key)
    if dump_dir is None:
        return redirect(url_for("org_details", org_id=org_id_int, error="Invalid dump selection."))
    download_name = f"speedhive_org_{org_id_int}_dump.zip" if dump_key in (None, "", "latest") else f"speedhive_org_{org_id_int}_{dump_key}.zip"

    if not dump_dir.exists():
        return redirect(url_for("org_details", org_id=org_id_int, error="No local dump found. Generate an offline dump first."))

    tmp = tempfile.NamedTemporaryFile(prefix=f"speedhive_org_{org_id_int}_", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in dump_dir.rglob("*"):
            if path.is_file() and (dump_dir != dump_root or "history" not in path.relative_to(dump_dir).parts):
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
        download_name=download_name,
        mimetype="application/zip",
    )


def delete_local_dump(org_id, dump_key: str = "latest"):
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))

    dump_dir = _resolve_dump_dir_for_org(org_id_int, dump_key)
    if dump_dir is None:
        return redirect(url_for("org_details", org_id=org_id_int, error="Invalid dump selection."))
    if not dump_dir.exists():
        return redirect(url_for("org_details", org_id=org_id_int, error="No dump found to delete."))

    if dump_key in (None, "", "latest"):
        _delete_latest_dump_contents(org_id_int)
    else:
        shutil.rmtree(dump_dir, ignore_errors=True)

    _prune_empty_dump_roots(org_id_int)
    return redirect(url_for("org_operations", org_id=org_id_int, notice="Deleted dump snapshot from disk."))


def upload_local_dump(org_id):
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))

    if "file" not in request.files:
        return redirect(url_for("org_operations", org_id=org_id_int, error="No file part in request."))
    file = request.files["file"]
    if file.filename == "":
        return redirect(url_for("org_operations", org_id=org_id_int, error="No file selected for upload."))

    if not file.filename.lower().endswith(".zip"):
        return redirect(url_for("org_operations", org_id=org_id_int, error="Invalid file format. Please upload a ZIP archive."))

    import shutil
    import tempfile
    from speedhive.workflows.import_sqlite_dump import import_dump_to_storage

    staging_dir = Path(tempfile.mkdtemp(prefix=f"speedhive_import_{org_id_int}_"))
    try:
        # Save ZIP file
        zip_path = staging_dir / "uploaded.zip"
        file.save(zip_path)

        # Unzip
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(staging_dir)

        # Find where events.ndjson or events.ndjson.gz is
        events_file = None
        for p in staging_dir.rglob("events.ndjson*"):
            if p.is_file():
                events_file = p
                break

        if not events_file:
            return redirect(url_for("org_operations", org_id=org_id_int, error="Invalid dump archive: events.ndjson not found in ZIP file."))

        source_dir = events_file.parent
        target_org_dir = staging_dir / str(org_id_int)
        if source_dir != target_org_dir:
            target_org_dir.mkdir(parents=True, exist_ok=True)
            for f in source_dir.glob("*.ndjson*"):
                if f.is_file():
                    shutil.move(str(f), str(target_org_dir / f.name))

        # Perform the import
        summary = import_dump_to_storage(org=org_id_int, dump_dir=staging_dir, storage=storage)
        notice = (
            f"Successfully imported offline dump: "
            f"{summary.get('events', 0)} events, "
            f"{summary.get('sessions', 0)} sessions, "
            f"{summary.get('results', 0)} results, "
            f"{summary.get('laps', 0)} laps, "
            f"{summary.get('announcements', 0)} announcements."
        )
        return redirect(url_for("org_operations", org_id=org_id_int, notice=notice))
    except Exception as exc:
        return redirect(url_for("org_operations", org_id=org_id_int, error=f"Import failed: {exc}"))
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def export_org_lap_records(org_id):
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))

    max_events = max(1, min(safe_int(request.args.get("max_events"), 25), MAX_ORG_EVENTS))

    def generate():
        for record in get_lap_records(storage, org_id_int, max_events):
            yield dumps_ndjson_record(record) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


def org_operations(org_id):
    try:
        org_id_int = int(org_id)
    except Exception:
        return redirect(url_for("index", error="Invalid organization ID."))

    from app.db import get_org_view, read_events_from_store, read_org_refresh_state
    org_view = get_org_view(org_id_int)
    org_refresh_state = read_org_refresh_state(org_id_int)
    events_data, events_meta = read_events_from_store(org_id_int)
    cache_status = events_meta
    dumps_list = _list_org_dumps(org_id_int)

    from app.tasks import _get_running_track_records_task_for_org
    running_task = _get_running_task_for_org(org_id_int)
    running_task_id = running_task["task_id"] if running_task else None
    running_track_records_task = _get_running_track_records_task_for_org(org_id_int)
    running_track_records_task_id = running_track_records_task["task_id"] if running_track_records_task else None

    return render_template(
        "org_operations.html",
        org=org_view,
        org_id=org_id_int,
        org_name=org_view.get("name"),
        org_refresh_state=org_refresh_state,
        dumps=dumps_list,
        dump_history=dumps_list,
        incremental_backfill_events=DEFAULT_INCREMENTAL_BACKFILL_EVENTS,
        active_tab="settings",
        active_settings_tab="data",
        cache_status=cache_status,
        running_task_id=running_task_id,
        running_track_records_task_id=running_track_records_task_id,
    )


def register_routes(app):
    app.add_url_rule("/organizations/add", "org_add", org_add, methods=["GET", "POST"])
    app.add_url_rule("/org/<org_id>", "org_details", org_details)
    app.add_url_rule("/org/<org_id>/refresh", "refresh_org", refresh_org, methods=["POST"])
    app.add_url_rule("/org/<org_id>/refresh/start", "refresh_org_start", refresh_org_start, methods=["POST"])
    app.add_url_rule("/org/<org_id>/clear-cache", "clear_cache", clear_cache, methods=["POST"])
    app.add_url_rule("/refresh/status/<task_id>", "refresh_status", refresh_status)
    app.add_url_rule("/refresh/stop/<task_id>", "refresh_stop", refresh_stop, methods=["POST"])
    app.add_url_rule("/org/<org_id>/dumps", "save_local", save_local, methods=["POST"])
    app.add_url_rule("/org/<org_id>/dumps/import", "upload_local_dump", upload_local_dump, methods=["POST"])
    app.add_url_rule("/org/<org_id>/dumps/latest.zip", "download_local_dump", download_local_dump)
    app.add_url_rule("/org/<org_id>/dumps/<dump_key>.zip", "download_local_dump", download_local_dump)
    app.add_url_rule("/org/<org_id>/dumps/<dump_key>/delete", "delete_local_dump", delete_local_dump, methods=["POST"])
    app.add_url_rule("/org/<org_id>/dumps/delete", "delete_local_dump", delete_local_dump, methods=["POST"])
    app.add_url_rule("/org/<org_id>/export-lap-records.ndjson", "export_org_lap_records", export_org_lap_records)
    app.add_url_rule("/org/<org_id>/operations", "org_operations", org_operations)
