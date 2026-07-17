import json
import re
from flask import request, redirect, url_for, render_template, current_app
from app import storage
from app.db import get_org_view, read_events_from_store
from app.utils import (
    format_datetime_display,
    format_seconds,
    iso_utc,
    utc_now,
)
from app.tasks import DATA_ROOT, TRACK_RECORDS_ROOT
from speedhive.settings import get_stats_min_laps, read_org_settings
from speedhive.utils.lap_analysis import first_non_empty

MAX_DISPLAYED_CLASSES = 8


def _select_classes_for_chart(chart_data, selected_classes):
    """Narrow a (possibly large, uncapped) cached class-pace payload down to
    the classes actually shown: the org's explicit picks from Settings if
    any, else the top MAX_DISPLAYED_CLASSES by lap volume (chart_data's
    classes are already volume-sorted). Either way, truncated to
    MAX_DISPLAYED_CLASSES to protect the validated categorical palette in
    class_pace.html (see dataviz skill) -- past that, adjacent-pair
    colorblind-safety can't be guaranteed.
    """
    all_classes = chart_data.get("classes", [])
    if selected_classes:
        selected_set = set(selected_classes)
        classes = [c for c in all_classes if c in selected_set][:MAX_DISPLAYED_CLASSES]
    else:
        classes = all_classes[:MAX_DISPLAYED_CLASSES]

    series = chart_data.get("series", {})
    counts = chart_data.get("counts", {})
    return {
        "years": chart_data.get("years", []),
        "classes": classes,
        "series": {c: series[c] for c in classes if c in series},
        "counts": {c: counts[c] for c in classes if c in counts},
    }


def org_stats(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    ignore_outliers = request.args.get("ignore_outliers", "1") in ("1", "true", "True")

    session_types_raw = request.args.getlist("session_types")
    if len(session_types_raw) == 1 and "," in session_types_raw[0]:
        session_types_list = [t.strip() for t in session_types_raw[0].split(",") if t.strip()]
    elif session_types_raw:
        session_types_list = [t.strip() for t in session_types_raw if t.strip()]
    else:
        session_types_list = []
        
    if not session_types_list and request.args.get("session_type"):
        st = request.args.get("session_type")
        if st == "all":
            session_types_list = ["race", "qualifying", "practice"]
        else:
            session_types_list = [st]

    session_types_list = [t for t in session_types_list if t in ("race", "qualifying", "practice")]
    if not session_types_list:
        session_types_list = ["race"]
        
    session_types_list.sort()
    session_types_str = ",".join(session_types_list)
    session_types_key = f"{session_types_str}:ignore_outliers" if ignore_outliers else session_types_str

    org_view = get_org_view(org_id_int)

    events_data, events_meta = read_events_from_store(org_id_int)
    cache_status = events_meta

    has_db_stats = storage.org_has_sessions(org_id_int)

    if not has_db_stats:
        return render_template(
            "org_stats.html",
            org=org_view,
            org_id=org_id_int,
            org_name=org_view.get("name"),
            manifest_exists=False,
            active_tab="stats",
            active_stats_tab="overview",
            cache_status=cache_status,
            session_types=session_types_list,
            session_types_str=session_types_str,
            ignore_outliers=ignore_outliers,
        )

    # Check if stats are already calculated and stored in SQLite org_stats table
    clustered = None
    calculated_at = None
    try:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT payload, calculated_at FROM org_stats WHERE org_id = ? AND session_type = ?",
                (org_id_int, session_types_key)
            ).fetchone()
        if row:
            clustered = json.loads(row["payload"])
            calculated_at = row["calculated_at"]
            _touch_org_stats_access(org_id_int, session_types_key)
    except Exception as e:
        current_app.logger.warning(f"Error loading stats from DB for org {org_id_int}: {e}")

    if not clustered:
        return render_template(
            "org_stats.html",
            org=org_view,
            org_id=org_id_int,
            org_name=org_view.get("name"),
            manifest_exists=True,
            has_persisted_stats=False,
            min_laps=get_stats_min_laps(org_id_int),
            active_tab="stats",
            active_stats_tab="overview",
            cache_status=cache_status,
            session_types=session_types_list,
            session_types_str=session_types_str,
            ignore_outliers=ignore_outliers,
            error=request.args.get("error"),
        )

    try:
        from speedhive.utils.lap_analysis import normalize_name

        min_laps = get_stats_min_laps(org_id_int)

        total_drivers = len(clustered)
        total_laps_analyzed = sum(d.get("lap_count", 0) for d in clustered.values())

        # Consistency ranks for the CV column, from the same filtered
        # population as the driver report's rank tile
        cv_rank_rows = _consistency_rank_rows(clustered)
        cv_ranks = {name: idx + 1 for idx, (name, _cv) in enumerate(cv_rank_rows)}
        consistency_total = len(cv_rank_rows)

        # All-drivers directory (race starts/wins/podiums with ranks)
        directory, directory_calculated_at = _load_driver_directory(org_id_int)

        # Map normalized keys back to the clustered representative name so
        # table links hit the report's exact-match path and the CV join works
        norm_to_rep = {}
        for rep, entry in clustered.items():
            norm_to_rep.setdefault(normalize_name(rep), rep)
            for alias in entry.get("aliases", []) or []:
                norm_to_rep.setdefault(normalize_name(alias), rep)

        directory_rows = []
        for r in (directory or {}).get("drivers", []):
            rep = norm_to_rep.get(r["key"])
            entry = clustered.get(rep) if rep else None
            cv = entry.get("cv") if entry else None
            cv_rank = cv_ranks.get(rep) if rep else None
            directory_rows.append({
                "key": r["key"],
                "name": rep or r["name"],
                "starts": r["starts"],
                "wins": r["wins"],
                "podiums": r["podiums"],
                "win_pct": r["win_pct"],
                "podium_pct": r["podium_pct"],
                "starts_rank": r["starts_rank"],
                "wins_rank": r["wins_rank"],
                "podiums_rank": r["podiums_rank"],
                # CV shown only for drivers in the ranked population; others
                # get a dash in the table
                "cv_pct": round(cv * 100, 2) if cv is not None and cv_rank is not None else None,
                "cv_rank": cv_rank,
            })

        return render_template(
            "org_stats.html",
            org=org_view,
            org_id=org_id_int,
            org_name=org_view.get("name"),
            manifest_exists=True,
            has_persisted_stats=True,
            calculated_at=calculated_at,
            total_drivers=total_drivers,
            total_laps_analyzed=total_laps_analyzed,
            directory_rows=directory_rows,
            directory_total=(directory or {}).get("total_drivers", 0),
            consistency_total=consistency_total,
            min_laps=min_laps,
            active_tab="stats",
            active_stats_tab="overview",
            cache_status=cache_status,
            session_types=session_types_list,
            session_types_str=session_types_str,
            ignore_outliers=ignore_outliers,
            freshness=stats_freshness(org_id_int, variant_calculated_at=calculated_at),
            error=request.args.get("error"),
        )
    except Exception as exc:
        return render_template(
            "org_stats.html",
            org=org_view,
            org_id=org_id_int,
            org_name=org_view.get("name"),
            manifest_exists=True,
            error=f"Failed to load consistency statistics: {exc}",
            active_tab="stats",
            active_stats_tab="overview",
            cache_status=cache_status,
            session_types=session_types_list,
            session_types_str=session_types_str,
        )


def _store_org_stats_payload(org_id_int, session_types_key, payload_obj):
    calculated_at = iso_utc(utc_now())
    payload_str = json.dumps(payload_obj, default=str)
    with storage.connect() as conn:
        # Preserve accessed_at across recalculations — it drives stale-variant pruning.
        conn.execute(
            "INSERT OR REPLACE INTO org_stats (org_id, session_type, payload, calculated_at, accessed_at) "
            "VALUES (?, ?, ?, ?, (SELECT accessed_at FROM org_stats WHERE org_id = ? AND session_type = ?))",
            (org_id_int, session_types_key, payload_str, calculated_at, org_id_int, session_types_key)
        )
        conn.commit()


def _recalc_consistency_stats(org_id_int, session_types_list, ignore_outliers):
    """Compute and store the clustered per-driver consistency payload for one
    session-types/ignore-outliers combination. Shared by the Stats page's
    Recalculate button and the Operations page's recalculate-all action."""
    from speedhive.analyzers.analyze_consistency import (
        load_session_types_from_storage,
        aggregate_by_name,
        cluster_names,
    )
    from app.analysis_cache import get_org_analysis

    session_types_list = sorted(session_types_list)
    session_types_str = ",".join(session_types_list)
    session_types_key = f"{session_types_str}:ignore_outliers" if ignore_outliers else session_types_str

    enriched = get_org_analysis(storage, org_id_int, ignore_outliers)["enriched"]
    session_map = load_session_types_from_storage(storage, org_id_int)
    by_name = aggregate_by_name(enriched, session_map, session_types=session_types_list)
    clustered = cluster_names(by_name, threshold=0.85)
    _store_org_stats_payload(org_id_int, session_types_key, clustered)

    # Prune older file cache if present
    cache_file = DATA_ROOT / f"org_{org_id_int}_stats_cache.json"
    if cache_file.exists():
        try:
            cache_file.unlink()
        except Exception:
            pass


# The all-drivers directory counts race starts only and has no lap-time
# computation, so like wins_podiums it has a single fixed cache key with no
# session-type/outlier variants. CV is joined in at request time from the
# consistency payload instead of being cached here.
DRIVER_DIRECTORY_CACHE_KEY = "driver_directory"

# The views every org gets by default. Automatic post-sync recalculation
# covers exactly these; any other (session_types, ignore_outliers) variant a
# user has explored refreshes on demand from its own page instead, so sync
# cost doesn't grow with every filter combination ever viewed.
PRIMARY_STATS_KEYS = (
    "race:ignore_outliers",
    "wins_podiums",
    "driver_directory",
    "most_improved_race:ignore_outliers",
    "classpace_race:ignore_outliers",
)

# Incidental variants nobody has opened in this many days are dropped during
# a full recalculation instead of being recomputed forever.
STALE_VARIANT_PRUNE_DAYS = 45


def _touch_org_stats_access(org_id_int, session_types_key):
    """Record that a cached stats view was actually served, for pruning."""
    try:
        with storage.connect() as conn:
            conn.execute(
                "UPDATE org_stats SET accessed_at = ? WHERE org_id = ? AND session_type = ?",
                (iso_utc(utc_now()), org_id_int, session_types_key),
            )
            conn.commit()
    except Exception as e:
        current_app.logger.warning(f"Failed to touch org_stats access for {org_id_int}/{session_types_key}: {e}")


def _recalc_driver_directory(org_id_int):
    """Compute and store the all-drivers starts/wins/podiums directory with
    precomputed ranks. Shared by the Drivers page, the driver report's rank
    tiles, and the recalculate-all action."""
    from speedhive.analyzers.analyze_consistency import load_session_types_from_storage
    from speedhive.analyzers.analyze_results import compute_driver_directory
    from app.analysis_cache import get_org_analysis

    # bundle results are already deduped, so no laps_payloads needed
    results_payloads = get_org_analysis(storage, org_id_int, True)["results_payloads"]
    session_map = load_session_types_from_storage(storage, org_id_int)
    payload = compute_driver_directory(results_payloads, session_map)
    _store_org_stats_payload(org_id_int, DRIVER_DIRECTORY_CACHE_KEY, payload)
    return payload


def _load_driver_directory(org_id_int, auto_recalc=True):
    """Read the cached driver directory, computing it inline once for orgs
    that predate the cache key (mirrors the wins/podiums auto-backfill).
    Returns (payload, calculated_at) or (None, None)."""
    try:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT payload, calculated_at FROM org_stats WHERE org_id = ? AND session_type = ?",
                (org_id_int, DRIVER_DIRECTORY_CACHE_KEY)
            ).fetchone()
        if row:
            return json.loads(row["payload"]), row["calculated_at"]
    except Exception as e:
        current_app.logger.warning(f"Error loading driver directory for org {org_id_int}: {e}")
    if not auto_recalc:
        return None, None
    try:
        return _recalc_driver_directory(org_id_int), iso_utc(utc_now())
    except Exception as e:
        current_app.logger.warning(f"Driver directory backfill failed for org {org_id_int}: {e}")
        return None, None


def _parse_iso_ts(val):
    from datetime import datetime

    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def stats_freshness(org_id_int, variant_calculated_at=None):
    """Freshness of the cached stats vs the last data sync.

    "stale" considers only the PRIMARY_STATS_KEYS views -- incidental filter
    variants going stale is expected under primary-only auto-recalc and is
    reported per page via variant_stale instead. Pass the current page's
    variant_calculated_at to get "variant_stale" for that specific view.
    "recalc_running" is True while a background recalc task is in flight
    (pages show an "updating" note instead of a stale warning)."""
    from app.db import read_org_refresh_state
    from app.tasks import _get_running_recalc_stats_task_for_org

    placeholders = ", ".join(["?"] * len(PRIMARY_STATS_KEYS))
    with storage.connect() as conn:
        row = conn.execute(
            f"SELECT MIN(calculated_at) AS oldest, COUNT(*) AS n FROM org_stats "
            f"WHERE org_id = ? AND session_type IN ({placeholders})",
            (org_id_int, *PRIMARY_STATS_KEYS)
        ).fetchone()
    oldest = row["oldest"] if row else None
    count = row["n"] if row else 0

    data_synced_at = (read_org_refresh_state(org_id_int) or {}).get("last_refresh_at")
    synced_dt = _parse_iso_ts(data_synced_at)
    oldest_dt = _parse_iso_ts(oldest)

    stale = False
    if synced_dt is not None:
        stale = count == 0 or oldest_dt is None or oldest_dt < synced_dt

    variant_stale = False
    if synced_dt is not None and variant_calculated_at is not None:
        variant_dt = _parse_iso_ts(variant_calculated_at)
        variant_stale = variant_dt is None or variant_dt < synced_dt

    return {
        "stats_calculated_at": oldest,
        "data_synced_at": data_synced_at,
        "stats_view_count": count,
        "stale": stale,
        "variant_stale": variant_stale,
        "recalc_running": _get_running_recalc_stats_task_for_org(org_id_int) is not None,
    }


def _consistency_rank_rows(clustered):
    """The consistency-ranked population as (name, cv) sorted ascending,
    using the same anomaly filter everywhere (cv above noise floor, 10+
    laps) so the "of N drivers" totals can never drift between the Drivers
    table and the driver report's rank tile."""
    rows = [
        (name, stats.get("cv"))
        for name, stats in clustered.items()
        if stats.get("cv") is not None and stats.get("cv") > 0.0002 and stats.get("lap_count", 0) >= 10
    ]
    rows.sort(key=lambda x: x[1])
    return rows


def generate_org_stats(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    ignore_outliers = (request.form.get("ignore_outliers") or request.args.get("ignore_outliers", "1")) in ("1", "true", "True")

    has_db_stats = storage.org_has_sessions(org_id_int)

    session_types_raw = request.form.getlist("session_types") or request.args.getlist("session_types")
    if len(session_types_raw) == 1 and "," in session_types_raw[0]:
        session_types_list = [t.strip() for t in session_types_raw[0].split(",") if t.strip()]
    elif session_types_raw:
        session_types_list = [t.strip() for t in session_types_raw if t.strip()]
    else:
        session_types_list = []

    if not session_types_list:
        st = request.form.get("session_type") or request.args.get("session_type")
        if st:
            if st == "all":
                session_types_list = ["race", "qualifying", "practice"]
            else:
                session_types_list = [st]

    session_types_list = [t for t in session_types_list if t in ("race", "qualifying", "practice")]
    if not session_types_list:
        session_types_list = ["race"]

    session_types_list.sort()

    if not has_db_stats:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": "No synced session data available to analyze."}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for("org_stats", **redirect_args))

    try:
        _recalc_consistency_stats(org_id_int, session_types_list, ignore_outliers)
        # The Drivers page joins the directory with this consistency payload,
        # so refresh both halves together
        _recalc_driver_directory(org_id_int)
    except Exception as exc:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": f"Analysis failed: {exc}"}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for("org_stats", **redirect_args))

    redirect_args = {"org_id": org_id_int, "session_types": session_types_list}
    redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
    return redirect(url_for("org_stats", **redirect_args))


def driver_stats_breakdown(org_id, driver_name):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    ignore_outliers = request.args.get("ignore_outliers", "1") in ("1", "true", "True")

    session_types_raw = request.args.getlist("session_types")
    if len(session_types_raw) == 1 and "," in session_types_raw[0]:
        session_types_list = [t.strip() for t in session_types_raw[0].split(",") if t.strip()]
    elif session_types_raw:
        session_types_list = [t.strip() for t in session_types_raw if t.strip()]
    else:
        session_types_list = []
        
    if not session_types_list and request.args.get("session_type"):
        st = request.args.get("session_type")
        if st == "all":
            session_types_list = ["race", "qualifying", "practice"]
        else:
            session_types_list = [st]

    session_types_list = [t for t in session_types_list if t in ("race", "qualifying", "practice")]
    if not session_types_list:
        session_types_list = ["race"]
        
    session_types_list.sort()
    session_types_str = ",".join(session_types_list)
    session_types_key = f"{session_types_str}:ignore_outliers" if ignore_outliers else session_types_str
    
    min_laps = get_stats_min_laps(org_id_int)

    org_view = get_org_view(org_id_int)

    from speedhive.workflows.track_records import curation as track_records
    from speedhive.utils.lap_analysis import normalize_classification
    
    # Load class alias map and curated track records
    tr_paths = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)
    alias_map = track_records.load_json(tr_paths["alias_map"], {"aliases": {}, "always_review": []})
    curated_fastest = track_records.build_curated_fastest_index(track_records.load_curated(tr_paths))

    aliases = {driver_name}
    overall_stats = None
    try:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM org_stats WHERE org_id = ? AND session_type = ?",
                (org_id_int, session_types_key)
            ).fetchone()
            
        total_drivers_consistency = 0
        consistency_rank = None
        
        if row:
            clustered = json.loads(row["payload"])
            if driver_name in clustered:
                overall_stats = dict(clustered[driver_name])
                if overall_stats.get("aliases"):
                    aliases.update(overall_stats["aliases"])
                mean_v = overall_stats.get("mean")
                stdev_v = overall_stats.get("stdev")
                cv_v = overall_stats.get("cv")
                overall_stats["mean_display"] = format_seconds(mean_v) if mean_v else "N/A"
                overall_stats["stdev_display"] = f"{stdev_v:.3f}s" if stdev_v else "N/A"
                overall_stats["cv_display"] = f"{cv_v * 100:.2f}%" if cv_v is not None else "N/A"

            # Compute consistency ranking
            from speedhive.utils.lap_analysis import normalize_name
            normalized_aliases = {normalize_name(a) for a in aliases}
            driver_cvs = _consistency_rank_rows(clustered)
            total_drivers_consistency = len(driver_cvs)
            
            # Find the rank of our driver
            for idx, (name, cv) in enumerate(driver_cvs):
                if name == driver_name or name in aliases or normalize_name(name) in normalized_aliases:
                    consistency_rank = idx + 1
                    break
    except Exception as e:
        current_app.logger.warning(f"Error loading stats for aliases of driver {driver_name}: {e}")

    has_db_stats = storage.org_has_sessions(org_id_int)

    if not has_db_stats:
        return redirect(url_for("org_stats", org_id=org_id_int, error="No synced session data available."))

    try:
        from collections import defaultdict
        from speedhive.utils.lap_analysis import normalize_name
        from speedhive.analyzers.analyze_consistency import (
            load_session_types_from_storage,
            matches_session_type,
        )
        from app.analysis_cache import get_org_analysis

        bundle = get_org_analysis(storage, org_id_int, ignore_outliers)
        laps_by_driver = bundle["laps_by_driver"]
        enriched = bundle["enriched"]
        results_payloads = bundle["results_payloads"]
        session_map = load_session_types_from_storage(storage, org_id_int)

        driver_sessions = []
        normalized_aliases = {normalize_name(a) for a in aliases}
        
        total_starts = 0
        total_wins = 0
        total_podiums = 0
        class_stats = defaultdict(lambda: {"starts": 0, "wins": 0, "podiums": 0, "best_lap": None})
        all_time_best_seconds = None
        all_time_best_session = ""
        all_time_best_date = ""

        for key, value in enriched.items():
            name = value.get("name")
            if not name:
                continue
                
            if name in aliases or normalize_name(name) in normalized_aliases or normalize_name(name) == normalize_name(driver_name):
                sess_match = re.match(r"session(\d+)_pos(\d+)", key)
                if sess_match:
                    sid = sess_match.group(1)
                    session_raw = session_map.get(sid, {})
                    
                    matched_types = [t for t in session_types_list if matches_session_type(session_raw, t)]
                    if matched_types:
                        # Find results for this session
                        driver_result = None
                        for r in results_payloads.get(sid, []):
                            r_name = r.get("name") or (r.get("competitor") or {}).get("name")
                            if r_name and (r_name in aliases or normalize_name(r_name) in normalized_aliases):
                                driver_result = r
                                break

                        finish_pos = None
                        class_pos = None
                        status = None
                        total_time = None
                        best_lap_time = None
                        start_number = None
                        is_race = (matched_types[0] == "race")

                        if driver_result:
                            status = driver_result.get("status")
                            total_time = driver_result.get("totalTime")
                            best_lap_time = driver_result.get("bestTime")
                            start_number = driver_result.get("startNumber")
                            
                            try:
                                finish_pos = int(driver_result.get("position"))
                            except (TypeError, ValueError):
                                pass
                                
                            try:
                                class_pos = int(driver_result.get("positionInClass"))
                            except (TypeError, ValueError):
                                pass

                        # Fetch the driver's class name from driver_result or fallback to session
                        driver_cls = None
                        if driver_result:
                            driver_cls = first_non_empty(
                                driver_result.get("resultClass"),
                                driver_result.get("class"),
                            )

                        raw_class_name = first_non_empty(
                            driver_cls,
                            session_raw.get("classification"),
                            session_raw.get("class"),
                            session_raw.get("classificationName"),
                            session_raw.get("className")
                        )
                        class_name = "Unknown Class"
                        if raw_class_name:
                            status_cls, resolved_cls = normalize_classification(raw_class_name, alias_map)
                            if status_cls == "ok":
                                class_name = resolved_cls

                        laps = laps_by_driver.get(key, [])
                        best_lap = min(laps) if laps else None

                        if is_race and status != "DNS":
                            total_starts += 1
                            class_stats[class_name]["starts"] += 1
                            if status == "Normal" and class_pos is not None:
                                if class_pos == 1:
                                    total_wins += 1
                                    class_stats[class_name]["wins"] += 1
                                if class_pos <= 3:
                                    total_podiums += 1
                                    class_stats[class_name]["podiums"] += 1
                            
                            if best_lap is not None:
                                if class_stats[class_name]["best_lap"] is None or best_lap < class_stats[class_name]["best_lap"]:
                                    class_stats[class_name]["best_lap"] = best_lap

                        session_name = session_raw.get("name") or session_raw.get("sessionName") or f"Session #{sid}"
                        
                        start_time_raw = first_non_empty(
                            session_raw.get("startTime"),
                            session_raw.get("scheduledStart"),
                            session_raw.get("start_date"),
                            session_raw.get("date"),
                        )
                        date_display = format_datetime_display(start_time_raw, include_time=True) or "N/A"
                        date_only = format_datetime_display(start_time_raw, include_time=False) or "N/A"
                        
                        if best_lap is not None:
                            if all_time_best_seconds is None or best_lap < all_time_best_seconds:
                                all_time_best_seconds = best_lap
                                all_time_best_session = session_name
                                all_time_best_date = date_only
                        
                        formatted_laps = []
                        filtered_laps = value.get("filtered_laps", laps)
                        non_outliers_pool = list(filtered_laps) if filtered_laps else []

                        for i, lap in enumerate(laps):
                            is_outlier = False
                            if ignore_outliers:
                                if lap in non_outliers_pool:
                                    non_outliers_pool.remove(lap)
                                else:
                                    is_outlier = True

                            formatted_laps.append({
                                "number": i + 1,
                                "seconds": lap,
                                "display": format_seconds(lap),
                                "is_best": lap == best_lap,
                                "is_outlier": is_outlier,
                            })

                        mean_val = value.get("mean")
                        stdev_val = value.get("stdev")
                        cv_val = value.get("cv")

                        driver_sessions.append({
                            "session_id": sid,
                            "session_name": session_name,
                            "class_name": class_name,
                            "session_type": matched_types[0].title(),
                            "date_display": date_display,
                            "date_only": date_only,
                            "lap_count": len(laps) if not ignore_outliers else len(filtered_laps),
                            "mean_display": format_seconds(mean_val) if mean_val else "N/A",
                            "stdev_display": f"{stdev_val:.3f}s" if stdev_val else "N/A",
                            "cv_display": f"{cv_val * 100:.2f}%" if cv_val is not None else "N/A",
                            "laps": formatted_laps,
                            "finish_pos": finish_pos,
                            "class_pos": class_pos,
                            "status": status,
                            "total_time": total_time,
                            "best_lap_time": best_lap_time,
                            "start_number": start_number,
                        })

        driver_sessions.sort(key=lambda s: s["session_id"], reverse=True)

        win_rate = (total_wins / total_starts * 100) if total_starts > 0 else 0.0
        podium_rate = (total_podiums / total_starts * 100) if total_starts > 0 else 0.0
        
        # Class stats will be enriched with class-specific best lap ranks and sorted lower down
        if all_time_best_seconds is not None:
            all_time_best_display = format_seconds(all_time_best_seconds)
            if all_time_best_date and all_time_best_date != "N/A":
                all_time_best_event = f"{all_time_best_session} ({all_time_best_date})"
            else:
                all_time_best_event = all_time_best_session
        else:
            all_time_best_display = "N/A"
            all_time_best_event = ""

        # Peak career consistency (lowest CV in any session)
        valid_cvs = []
        for s in driver_sessions:
            try:
                if s.get("cv_display") != "N/A":
                    cv_val = float(s["cv_display"].replace("%", ""))
                    # Filter out exactly 0.00% or extremely small CVs (<= 0.02%)
                    # as these represent data/timing anomalies rather than real lap consistency.
                    if cv_val > 0.02:
                        valid_cvs.append((cv_val, s["session_name"], s.get("date_only", "N/A")))
            except (TypeError, ValueError):
                pass
        if valid_cvs:
            best_cv, best_cv_sess, best_cv_date = min(valid_cvs, key=lambda x: x[0])
            peak_consistency_val = f"{best_cv:.2f}%"
            if best_cv_date and best_cv_date != "N/A":
                peak_consistency_sess = f"{best_cv_sess} ({best_cv_date})"
            else:
                peak_consistency_sess = best_cv_sess
        else:
            peak_consistency_val = "N/A"
            peak_consistency_sess = ""

        # Consistency Trend (Earliest vs. Recent session averages)
        # Filter out anomaly CVs <= 0.02% from trend calculations too
        def get_valid_cv_float(s):
            try:
                val = float(s.get("cv_display", "").replace("%", ""))
                return val if val > 0.02 else None
            except (TypeError, ValueError):
                return None

        chrono_sessions = sorted(
            [s for s in driver_sessions if s.get("cv_display") != "N/A" and get_valid_cv_float(s) is not None],
            key=lambda s: (s.get("date_only") or "9999-99-99", s["session_id"])
        )
        trend_text = "N/A"
        trend_direction = "stable"
        trend_timeframe = ""
        
        if len(chrono_sessions) >= 2:
            # Extract timeframe years
            def get_session_year(s_id):
                s_raw = session_map.get(s_id, {})
                for field in ["startTime", "scheduledStart", "start_date", "date"]:
                    val = s_raw.get(field)
                    if val:
                        match = re.search(r'\b(19|20)\d{2}\b', str(val))
                        if match:
                            return match.group(0)
                return None
                
            first_year = get_session_year(chrono_sessions[0]["session_id"])
            last_year = get_session_year(chrono_sessions[-1]["session_id"])
            if first_year and last_year:
                if first_year == last_year:
                    trend_timeframe = f"({first_year})"
                else:
                    trend_timeframe = f"({first_year}–{last_year})"

            def parse_cv(s):
                try:
                    return float(s["cv_display"].replace("%", ""))
                except (TypeError, ValueError):
                    return None
            cv_vals = [parse_cv(s) for s in chrono_sessions]
            cv_vals = [v for v in cv_vals if v is not None]
            if len(cv_vals) >= 2:
                # Compare absolute first data point to absolute last data point
                first_val = cv_vals[0]
                last_val = cv_vals[-1]
                delta = first_val - last_val  # Positive is improvement (lower CV)
                if delta > 0.05:
                    trend_text = f"Improving by {delta:.2f}pp ({first_val:.2f}% → {last_val:.2f}%)"
                    trend_direction = "improving"
                elif delta < -0.05:
                    trend_text = f"Declining by {abs(delta):.2f}pp ({first_val:.2f}% → {last_val:.2f}%)"
                    trend_direction = "declining"
                else:
                    trend_text = f"Stable ({first_val:.2f}% → {last_val:.2f}%)"
                    trend_direction = "stable"

        # Compute best lap for all drivers, grouped by resolved class
        # driver_class_best_laps[class_name][driver_name_norm] = best_lap_seconds
        # We cache class_rankings directly on the bundle dict in memory to avoid
        # recalculating this across different driver pages (huge performance win).
        # We key it by the serialized alias_map so it refreshes if aliases change.
        alias_map_key = json.dumps(alias_map, sort_keys=True)
        if "class_rankings_cache" not in bundle:
            bundle["class_rankings_cache"] = {}

        if alias_map_key in bundle["class_rankings_cache"]:
            class_rankings = bundle["class_rankings_cache"][alias_map_key]
        else:
            driver_class_best_laps = defaultdict(dict)
            for key, val in enriched.items():
                name = val.get("name")
                if not name:
                    continue
                
                # Resolve session class name using alias_map
                raw_cls = val.get("class_name") or val.get("resultClass") or val.get("class")
                if not raw_cls:
                    s_id = key.split("_")[0].replace("session", "")
                    s_raw = session_map.get(s_id, {})
                    raw_cls = first_non_empty(
                        s_raw.get("classification"),
                        s_raw.get("class"),
                        s_raw.get("classificationName"),
                        s_raw.get("className")
                    )
                
                resolved_cls = "Unknown Class"
                if raw_cls:
                    status_c, resolved_c = normalize_classification(raw_cls, alias_map)
                    if status_c == "ok":
                        resolved_cls = resolved_c
                
                laps = laps_by_driver.get(key, [])
                filtered_laps = val.get("filtered_laps", laps)
                non_outliers = [lap for lap in filtered_laps if lap > 0.1]
                if non_outliers:
                    best = min(non_outliers)
                    norm_name = normalize_name(name)
                    current_best = driver_class_best_laps[resolved_cls].get(norm_name)
                    if current_best is None or best < current_best:
                        driver_class_best_laps[resolved_cls][norm_name] = best
                        
            # Sort each class's best laps
            class_rankings = {}
            for cls, driver_laps in driver_class_best_laps.items():
                class_rankings[cls] = sorted(driver_laps.items(), key=lambda x: x[1])
            
            bundle["class_rankings_cache"][alias_map_key] = class_rankings

        # Starts/wins/podiums ranks across all drivers at this track, from the
        # cached directory (backfilled inline for orgs that predate the key)
        directory, _directory_calculated_at = _load_driver_directory(org_id_int)
        starts_rank = None
        wins_rank = None
        podiums_rank = None
        total_drivers_starts = 0
        total_drivers_wins = 0
        if directory:
            dir_rows = directory.get("drivers", [])
            total_drivers_starts = directory.get("total_drivers", len(dir_rows))
            total_drivers_wins = total_drivers_starts
            norm_self = normalize_name(driver_name)
            matches = [r for r in dir_rows if r["key"] == norm_self or r["key"] in normalized_aliases]
            if matches:
                # min == the first match in rank order, same as the old
                # first-match-in-sorted-list behavior
                starts_rank = min(r["starts_rank"] for r in matches)
                wins_rank = min(r["wins_rank"] for r in matches)
                podiums_rank = min(r["podiums_rank"] for r in matches)

        # Enrich class_stats with best lap rank and rates
        for cls, cstats in class_stats.items():
            starts = cstats["starts"]
            cstats["win_rate"] = f"{(cstats['wins'] / starts * 100):.1f}%" if starts > 0 else "0.0%"
            cstats["podium_rate"] = f"{(cstats['podiums'] / starts * 100):.1f}%" if starts > 0 else "0.0%"
            
            # Find fastest lap rank for this driver in this class
            rank_display = ""
            if cls in class_rankings and cstats["best_lap"] is not None:
                sorted_laps = class_rankings[cls]
                total_in_class = len(sorted_laps)
                driver_rank = None
                for idx, (norm_name, lap_time) in enumerate(sorted_laps):
                    if norm_name == normalize_name(driver_name) or norm_name in normalized_aliases:
                        driver_rank = idx + 1
                        break
                if driver_rank:
                    rank_display = f" (P{driver_rank} of {total_in_class})"

            cstats["best_lap_display"] = f"{format_seconds(cstats['best_lap'])}{rank_display}" if cstats["best_lap"] else "N/A"

            # Compare against the org's curated official track record for this class
            record_gap_display = ""
            holds_record = False
            record_entry = curated_fastest.get(cls)
            if record_entry and cstats["best_lap"] is not None:
                gap = cstats["best_lap"] - record_entry["_seconds"]
                if gap <= 0.0005:
                    record_gap_display = "Track Record"
                    holds_record = True
                else:
                    record_gap_display = f"+{gap:.3f}s"
            cstats["record_gap_display"] = record_gap_display
            cstats["holds_record"] = holds_record

        sorted_class_stats = dict(sorted(class_stats.items(), key=lambda item: item[1]["starts"], reverse=True))

        return render_template(
            "driver_stats_breakdown.html",
            org=org_view,
            org_id=org_id_int,
            driver_name=driver_name,
            aliases=sorted(list(aliases)),
            overall_stats=overall_stats,
            driver_sessions=driver_sessions,
            session_types=session_types_list,
            min_laps=min_laps,
            active_tab="stats",
            ignore_outliers=ignore_outliers,
            total_starts=total_starts,
            total_wins=total_wins,
            total_podiums=total_podiums,
            win_rate_display=f"{win_rate:.1f}%" if total_starts > 0 else "0.0%",
            podium_rate_display=f"{podium_rate:.1f}%" if total_starts > 0 else "0.0%",
            class_stats=sorted_class_stats,
            all_time_best_display=all_time_best_display,
            all_time_best_event=all_time_best_event,
            peak_consistency_val=peak_consistency_val,
            peak_consistency_sess=peak_consistency_sess,
            trend_text=trend_text,
            trend_direction=trend_direction,
            trend_timeframe=trend_timeframe,
            consistency_rank=consistency_rank,
            total_drivers_consistency=total_drivers_consistency,
            starts_rank=starts_rank,
            total_drivers_starts=total_drivers_starts,
            wins_rank=wins_rank,
            total_drivers_wins=total_drivers_wins,
            podiums_rank=podiums_rank,
        )
    except Exception as exc:
        return render_template(
            "driver_stats_breakdown.html",
            org=org_view,
            org_id=org_id_int,
            driver_name=driver_name,
            error=f"Failed to load breakdown: {exc}",
            session_types=session_types_list,
            min_laps=min_laps,
            active_tab="stats",
            ignore_outliers=ignore_outliers,
        )


def org_class_pace(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    ignore_outliers = request.args.get("ignore_outliers", "1") in ("1", "true", "True")

    session_types_raw = request.args.getlist("session_types")
    if len(session_types_raw) == 1 and "," in session_types_raw[0]:
        session_types_list = [t.strip() for t in session_types_raw[0].split(",") if t.strip()]
    elif session_types_raw:
        session_types_list = [t.strip() for t in session_types_raw if t.strip()]
    else:
        session_types_list = []

    session_types_list = [t for t in session_types_list if t in ("race", "qualifying", "practice")]
    if not session_types_list:
        session_types_list = ["race"]

    session_types_list.sort()
    session_types_str = ",".join(session_types_list)
    # "classpace_" prefix keeps this cache entry from ever colliding with the
    # driver-consistency cache entries in the same org_stats table, which key
    # on the bare session_types string.
    session_types_key = f"classpace_{session_types_str}" + (":ignore_outliers" if ignore_outliers else "")

    org_view = get_org_view(org_id_int)
    has_db_stats = storage.org_has_sessions(org_id_int)

    if not has_db_stats:
        return render_template(
            "class_pace.html",
            org=org_view,
            org_id=org_id_int,
            manifest_exists=False,
            active_tab="stats",
            active_stats_tab="class_pace",
            session_types=session_types_list,
            session_types_str=session_types_str,
            ignore_outliers=ignore_outliers,
        )

    chart_data = None
    calculated_at = None
    try:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT payload, calculated_at FROM org_stats WHERE org_id = ? AND session_type = ?",
                (org_id_int, session_types_key)
            ).fetchone()
        if row:
            chart_data = json.loads(row["payload"])
            calculated_at = row["calculated_at"]
            _touch_org_stats_access(org_id_int, session_types_key)
    except Exception as e:
        current_app.logger.warning(f"Error loading class-pace stats from DB for org {org_id_int}: {e}")

    class_pace_settings = read_org_settings(org_id_int).get("class_pace", {})
    class_pace_config = {
        "classes": class_pace_settings.get("classes") or [],
        "regression": bool(class_pace_settings.get("regression")),
    }
    available_classes = chart_data.get("classes", []) if chart_data else []

    table_rows = None
    if chart_data:
        chart_data = _select_classes_for_chart(chart_data, class_pace_settings.get("classes") or [])
        classes = chart_data.get("classes", [])
        years = chart_data.get("years", [])
        series = chart_data.get("series", {})
        counts = chart_data.get("counts", {})
        table_rows = []
        for i, year in enumerate(years):
            cells = []
            for cls in classes:
                secs = series.get(cls, [None] * len(years))[i]
                cells.append({
                    "display": format_seconds(secs) if secs else "—",
                    "count": counts.get(cls, [0] * len(years))[i],
                })
            table_rows.append({"year": year, "cells": cells})

    return render_template(
        "class_pace.html",
        org=org_view,
        org_id=org_id_int,
        manifest_exists=True,
        has_persisted_stats=bool(chart_data),
        calculated_at=calculated_at,
        chart_data=chart_data,
        class_pace_config=class_pace_config,
        available_classes=available_classes,
        table_rows=table_rows,
        active_tab="stats",
        active_stats_tab="class_pace",
        session_types=session_types_list,
        session_types_str=session_types_str,
        ignore_outliers=ignore_outliers,
    )


def org_participation(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    # Participation is a distinct-driver headcount per year, not a lap-time
    # statistic -- outlier lap filtering can never change who raced, so
    # unlike Class Pace this tab has no ignore_outliers control of its own.
    # It always reads the ignore_outliers-filtered cache variant, since
    # that's the one guaranteed to exist (Class Pace defaults to it too) and
    # the participation numbers within it are identical to the unfiltered
    # variant regardless.
    session_types_raw = request.args.getlist("session_types")
    if len(session_types_raw) == 1 and "," in session_types_raw[0]:
        session_types_list = [t.strip() for t in session_types_raw[0].split(",") if t.strip()]
    elif session_types_raw:
        session_types_list = [t.strip() for t in session_types_raw if t.strip()]
    else:
        session_types_list = []

    session_types_list = [t for t in session_types_list if t in ("race", "qualifying", "practice")]
    if not session_types_list:
        session_types_list = ["race"]

    session_types_list.sort()
    session_types_str = ",".join(session_types_list)
    session_types_key = f"classpace_{session_types_str}:ignore_outliers"

    org_view = get_org_view(org_id_int)
    has_db_stats = storage.org_has_sessions(org_id_int)

    if not has_db_stats:
        return render_template(
            "participation.html",
            org=org_view,
            org_id=org_id_int,
            manifest_exists=False,
            active_tab="stats",
            active_stats_tab="participation",
            session_types=session_types_list,
            session_types_str=session_types_str,
        )

    participation_data = None
    participation_by_class = None
    calculated_at = None
    try:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT payload, calculated_at FROM org_stats WHERE org_id = ? AND session_type = ?",
                (org_id_int, session_types_key)
            ).fetchone()
        if row:
            payload = json.loads(row["payload"])
            participation_data = payload.get("participation")
            participation_by_class = payload.get("participation_by_class")
            calculated_at = row["calculated_at"]
            _touch_org_stats_access(org_id_int, session_types_key)
    except Exception as e:
        current_app.logger.warning(f"Error loading participation stats from DB for org {org_id_int}: {e}")

    return render_template(
        "participation.html",
        org=org_view,
        org_id=org_id_int,
        manifest_exists=True,
        has_persisted_stats=bool(participation_data),
        calculated_at=calculated_at,
        participation_data=participation_data,
        participation_by_class=participation_by_class,
        active_tab="stats",
        active_stats_tab="participation",
        session_types=session_types_list,
        session_types_str=session_types_str,
    )


def set_class_pace_config(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    from speedhive.settings import write_org_settings

    classes = request.form.getlist("class_pace_classes")
    regression = request.form.get("class_pace_regression") == "on"

    config_data = read_org_settings(org_id_int)
    config_data["class_pace"] = {"classes": classes, "regression": regression}
    write_org_settings(org_id_int, config_data)

    redirect_args = {"org_id": org_id_int}
    session_types = request.form.getlist("session_types")
    if session_types:
        redirect_args["session_types"] = session_types
    redirect_args["ignore_outliers"] = request.form.get("ignore_outliers", "1")
    return redirect(url_for("org_class_pace", **redirect_args))


def _recalc_class_pace(org_id_int, session_types_list, ignore_outliers):
    """Compute and store the class pace + participation payload for one
    session-types/ignore-outliers combination. Shared by the Class Pace and
    Participation pages' Recalculate buttons and the Operations page's
    recalculate-all action."""
    from speedhive.analyzers.analyze_consistency import load_session_types_from_storage
    from speedhive.analyzers.analyze_class_pace import (
        compute_avg_lap_by_class_year,
        compute_participation_by_class_year,
        compute_participation_by_year,
    )
    from speedhive.workflows.track_records import curation as track_records
    from app.analysis_cache import get_org_analysis

    session_types_list = sorted(session_types_list)
    session_types_str = ",".join(session_types_list)
    session_types_key = f"classpace_{session_types_str}" + (":ignore_outliers" if ignore_outliers else "")

    bundle = get_org_analysis(storage, org_id_int, ignore_outliers)
    enriched = bundle["enriched"]
    results_map = bundle["results_payloads"]
    session_map = load_session_types_from_storage(storage, org_id_int)
    # Same alias map (and resolution logic) track-record curation uses,
    # so "Spec Miata" and "SM" group together consistently everywhere,
    # not just in curated records -- see analyze_class_pace docstrings.
    alias_map_path = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id_int)["alias_map"]
    alias_map = track_records.load_json(alias_map_path, {"aliases": {}, "always_review": []})
    # Cache every qualifying class here, uncapped -- the chart's own
    # inline class picker and the 8-class display cap (validated
    # categorical palette in class_pace.html, see dataviz skill) are both
    # applied at display time in org_class_pace, over this same cached
    # payload, so neither needs a recompute to change what's shown.
    chart_data = compute_avg_lap_by_class_year(
        enriched, session_map, results_map, session_types=session_types_list, max_classes=None, alias_map=alias_map
    )
    # Combined-across-classes participation trend, and the per-class
    # breakdown behind it, cached alongside the per-class pace data since
    # they all share the same Generate/Recalculate action.
    chart_data["participation"] = compute_participation_by_year(enriched, session_map, session_types=session_types_list)
    chart_data["participation_by_class"] = compute_participation_by_class_year(
        enriched, session_map, results_map, session_types=session_types_list, max_classes=None, alias_map=alias_map
    )
    _store_org_stats_payload(org_id_int, session_types_key, chart_data)


def generate_org_class_pace(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    ignore_outliers = (request.form.get("ignore_outliers") or request.args.get("ignore_outliers", "1")) in ("1", "true", "True")

    # Both the Class Pace and Participation tabs share this one computation
    # (they're cached together in the same org_stats row), so each page's own
    # Generate/Recalculate button can send you back to itself instead of
    # always bouncing to Class Pace.
    redirect_endpoint = request.form.get("redirect_to") or "org_class_pace"
    if redirect_endpoint not in ("org_class_pace", "org_participation"):
        redirect_endpoint = "org_class_pace"

    session_types_raw = request.form.getlist("session_types") or request.args.getlist("session_types")
    if len(session_types_raw) == 1 and "," in session_types_raw[0]:
        session_types_list = [t.strip() for t in session_types_raw[0].split(",") if t.strip()]
    elif session_types_raw:
        session_types_list = [t.strip() for t in session_types_raw if t.strip()]
    else:
        session_types_list = []

    session_types_list = [t for t in session_types_list if t in ("race", "qualifying", "practice")]
    if not session_types_list:
        session_types_list = ["race"]

    session_types_list.sort()

    has_db_stats = storage.org_has_sessions(org_id_int)
    if not has_db_stats:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": "No synced session data available to analyze."}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for(redirect_endpoint, **redirect_args))

    try:
        _recalc_class_pace(org_id_int, session_types_list, ignore_outliers)
    except Exception as exc:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": f"Analysis failed: {exc}"}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for(redirect_endpoint, **redirect_args))

    redirect_args = {"org_id": org_id_int, "session_types": session_types_list}
    redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
    return redirect(url_for(redirect_endpoint, **redirect_args))


def org_most_improved(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    ignore_outliers = request.args.get("ignore_outliers", "1") in ("1", "true", "True")

    session_types_raw = request.args.getlist("session_types")
    if len(session_types_raw) == 1 and "," in session_types_raw[0]:
        session_types_list = [t.strip() for t in session_types_raw[0].split(",") if t.strip()]
    elif session_types_raw:
        session_types_list = [t.strip() for t in session_types_raw if t.strip()]
    else:
        session_types_list = []

    session_types_list = [t for t in session_types_list if t in ("race", "qualifying", "practice")]
    if not session_types_list:
        session_types_list = ["race"]

    session_types_list.sort()
    session_types_str = ",".join(session_types_list)
    session_types_key = f"most_improved_{session_types_str}" + (":ignore_outliers" if ignore_outliers else "")

    org_view = get_org_view(org_id_int)
    has_db_stats = storage.org_has_sessions(org_id_int)

    if not has_db_stats:
        return render_template(
            "most_improved.html",
            org=org_view,
            org_id=org_id_int,
            manifest_exists=False,
            active_tab="stats",
            active_stats_tab="most_improved",
            session_types=session_types_list,
            session_types_str=session_types_str,
            ignore_outliers=ignore_outliers,
        )

    most_improved = None
    most_declined = None
    calculated_at = None
    try:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT payload, calculated_at FROM org_stats WHERE org_id = ? AND session_type = ?",
                (org_id_int, session_types_key)
            ).fetchone()
        if row:
            payload = json.loads(row["payload"])
            most_improved = payload.get("most_improved")
            most_declined = payload.get("most_declined")
            calculated_at = row["calculated_at"]
            _touch_org_stats_access(org_id_int, session_types_key)

            # If the database payload was generated with the old limit=15 cap,
            # automatically recalculate inline to cache the full list of drivers.
            if most_improved is not None and len(most_improved) <= 15:
                try:
                    from speedhive.analyzers.analyze_consistency import get_most_improved_rankings, load_session_types_from_storage
                    from app.analysis_cache import get_org_analysis

                    enriched = get_org_analysis(storage, org_id_int, ignore_outliers)["enriched"]
                    s_map = load_session_types_from_storage(storage, org_id_int)
                    min_laps = get_stats_min_laps(org_id_int)
                    
                    full_improved, full_declined = get_most_improved_rankings(
                        enriched, s_map, session_types=session_types_list, min_laps=min_laps, limit=None
                    )
                    
                    _store_org_stats_payload(
                        org_id_int, session_types_key,
                        {"most_improved": full_improved, "most_declined": full_declined},
                    )

                    most_improved = full_improved
                    most_declined = full_declined
                except Exception as e:
                    current_app.logger.warning(f"Failed to auto-recalculate full most_improved: {e}")
    except Exception as e:
        current_app.logger.warning(f"Error loading most-improved stats from DB for org {org_id_int}: {e}")

    return render_template(
        "most_improved.html",
        org=org_view,
        org_id=org_id_int,
        manifest_exists=True,
        has_persisted_stats=most_improved is not None,
        calculated_at=calculated_at,
        most_improved=most_improved or [],
        most_declined=most_declined or [],
        min_laps=get_stats_min_laps(org_id_int),
        active_tab="stats",
        active_stats_tab="most_improved",
        session_types=session_types_list,
        session_types_str=session_types_str,
        ignore_outliers=ignore_outliers,
        freshness=stats_freshness(org_id_int, variant_calculated_at=calculated_at),
        error=request.args.get("error"),
    )


def _recalc_most_improved(org_id_int, session_types_list, ignore_outliers):
    """Compute and store the most improved/declined payload for one
    session-types/ignore-outliers combination. Shared by the Most Improved
    page's Recalculate button and the Operations page's recalculate-all
    action."""
    from speedhive.analyzers.analyze_consistency import load_session_types_from_storage, get_most_improved_rankings
    from app.analysis_cache import get_org_analysis

    session_types_list = sorted(session_types_list)
    session_types_str = ",".join(session_types_list)
    session_types_key = f"most_improved_{session_types_str}" + (":ignore_outliers" if ignore_outliers else "")

    enriched = get_org_analysis(storage, org_id_int, ignore_outliers)["enriched"]
    session_map = load_session_types_from_storage(storage, org_id_int)
    min_laps = get_stats_min_laps(org_id_int)
    most_improved, most_declined = get_most_improved_rankings(
        enriched, session_map, session_types=session_types_list, min_laps=min_laps, limit=None
    )
    _store_org_stats_payload(org_id_int, session_types_key, {"most_improved": most_improved, "most_declined": most_declined})


def generate_org_most_improved(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    ignore_outliers = (request.form.get("ignore_outliers") or request.args.get("ignore_outliers", "1")) in ("1", "true", "True")

    session_types_raw = request.form.getlist("session_types") or request.args.getlist("session_types")
    if len(session_types_raw) == 1 and "," in session_types_raw[0]:
        session_types_list = [t.strip() for t in session_types_raw[0].split(",") if t.strip()]
    elif session_types_raw:
        session_types_list = [t.strip() for t in session_types_raw if t.strip()]
    else:
        session_types_list = []

    session_types_list = [t for t in session_types_list if t in ("race", "qualifying", "practice")]
    if not session_types_list:
        session_types_list = ["race"]

    session_types_list.sort()

    has_db_stats = storage.org_has_sessions(org_id_int)
    if not has_db_stats:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": "No synced session data available to analyze."}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for("org_most_improved", **redirect_args))

    try:
        _recalc_most_improved(org_id_int, session_types_list, ignore_outliers)
    except Exception as exc:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": f"Analysis failed: {exc}"}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for("org_most_improved", **redirect_args))

    redirect_args = {"org_id": org_id_int, "session_types": session_types_list}
    redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
    return redirect(url_for("org_most_improved", **redirect_args))


# "Wins" only makes sense for race sessions, and there's no lap-time
# computation here to filter outliers from -- so unlike the tabs above,
# this one has no session-types or ignore-outliers knobs, and no cache-key
# variants (a single fixed key covers it).
WINS_PODIUMS_CACHE_KEY = "wins_podiums"


def org_wins_podiums(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    org_view = get_org_view(org_id_int)
    has_db_stats = storage.org_has_sessions(org_id_int)

    if not has_db_stats:
        return render_template(
            "wins_podiums.html",
            org=org_view,
            org_id=org_id_int,
            manifest_exists=False,
            active_tab="stats",
            active_stats_tab="wins_podiums",
        )

    most_wins = None
    most_podiums = None
    calculated_at = None
    try:
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT payload, calculated_at FROM org_stats WHERE org_id = ? AND session_type = ?",
                (org_id_int, WINS_PODIUMS_CACHE_KEY)
            ).fetchone()
        if row:
            payload = json.loads(row["payload"])
            most_wins = payload.get("most_wins")
            most_podiums = payload.get("most_podiums")
            calculated_at = row["calculated_at"]
            _touch_org_stats_access(org_id_int, WINS_PODIUMS_CACHE_KEY)

            # If the database payload was generated with the old limit=15 cap,
            # automatically recalculate inline to cache the full list of drivers.
            if most_wins is not None and len(most_wins) <= 15:
                try:
                    from speedhive.analyzers.analyze_consistency import load_session_types_from_storage
                    from speedhive.analyzers.analyze_results import get_wins_podiums_rankings
                    from app.analysis_cache import get_org_analysis

                    # bundle results are already deduped, so no laps_payloads needed
                    results_payloads = get_org_analysis(storage, org_id_int, True)["results_payloads"]
                    s_map = load_session_types_from_storage(storage, org_id_int)

                    full_wins, full_podiums = get_wins_podiums_rankings(results_payloads, s_map, limit=None)
                    
                    _store_org_stats_payload(
                        org_id_int, WINS_PODIUMS_CACHE_KEY,
                        {"most_wins": full_wins, "most_podiums": full_podiums},
                    )

                    most_wins = full_wins
                    most_podiums = full_podiums
                except Exception as e:
                    current_app.logger.warning(f"Failed to auto-recalculate full wins/podiums: {e}")
    except Exception as e:
        current_app.logger.warning(f"Error loading wins/podiums stats from DB for org {org_id_int}: {e}")

    return render_template(
        "wins_podiums.html",
        org=org_view,
        org_id=org_id_int,
        manifest_exists=True,
        has_persisted_stats=most_wins is not None,
        calculated_at=calculated_at,
        most_wins=most_wins or [],
        most_podiums=most_podiums or [],
        active_tab="stats",
        active_stats_tab="wins_podiums",
        freshness=stats_freshness(org_id_int, variant_calculated_at=calculated_at),
        error=request.args.get("error"),
    )


def _recalc_wins_podiums(org_id_int):
    """Compute and store the wins/podiums leaderboard payload. Shared by the
    Wins & Podiums page's Recalculate button and the Operations page's
    recalculate-all action."""
    from speedhive.analyzers.analyze_consistency import load_session_types_from_storage
    from speedhive.analyzers.analyze_results import get_wins_podiums_rankings
    from app.analysis_cache import get_org_analysis

    # bundle results are already deduped, so no laps_payloads needed
    results_payloads = get_org_analysis(storage, org_id_int, True)["results_payloads"]
    session_map = load_session_types_from_storage(storage, org_id_int)
    most_wins, most_podiums = get_wins_podiums_rankings(results_payloads, session_map, limit=None)
    _store_org_stats_payload(org_id_int, WINS_PODIUMS_CACHE_KEY, {"most_wins": most_wins, "most_podiums": most_podiums})


def generate_org_wins_podiums(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    has_db_stats = storage.org_has_sessions(org_id_int)
    if not has_db_stats:
        return redirect(url_for("org_wins_podiums", org_id=org_id_int, error="No synced session data available to analyze."))

    try:
        _recalc_wins_podiums(org_id_int)
    except Exception as exc:
        return redirect(url_for("org_wins_podiums", org_id=org_id_int, error=f"Analysis failed: {exc}"))

    return redirect(url_for("org_wins_podiums", org_id=org_id_int))


VALID_SESSION_TYPES = ("race", "qualifying", "practice")


def _recalc_all_stats_for_org(org_id_int, scope="all", progress_cb=None, logger=None):
    """Recalculate cached stat views. scope="primary" covers exactly the
    PRIMARY_STATS_KEYS defaults (what automatic post-sync recalculation
    uses); scope="all" additionally refreshes every other cached variant and
    prunes variants nobody has opened in STALE_VARIANT_PRUNE_DAYS days.
    Also leaves the org-analysis cache warm, so driver reports load fast.
    Returns (recalculated_count, failed_keys, pruned_count).

    Shared by the Settings > Data recalculate button and the automatic
    post-sync/post-import recalc task (app/tasks.py)."""
    keys = set(PRIMARY_STATS_KEYS)
    pruned = 0
    if scope == "all":
        from datetime import datetime, timedelta, timezone

        cutoff = iso_utc(datetime.now(timezone.utc) - timedelta(days=STALE_VARIANT_PRUNE_DAYS))
        with storage.connect() as conn:
            primary_placeholders = ", ".join(["?"] * len(PRIMARY_STATS_KEYS))
            cur = conn.execute(
                f"DELETE FROM org_stats WHERE org_id = ? AND session_type NOT IN ({primary_placeholders}) "
                "AND COALESCE(accessed_at, calculated_at, '') < ?",
                (org_id_int, *PRIMARY_STATS_KEYS, cutoff),
            )
            pruned = cur.rowcount or 0
            conn.commit()
            rows = conn.execute("SELECT session_type FROM org_stats WHERE org_id = ?", (org_id_int,)).fetchall()
        keys.update(row["session_type"] for row in rows)

    key_labels = {
        WINS_PODIUMS_CACHE_KEY: "wins & podiums",
        DRIVER_DIRECTORY_CACHE_KEY: "driver directory",
    }

    def describe(key):
        if key in key_labels:
            return key_labels[key]
        if key.startswith("classpace_"):
            return "class pace"
        if key.startswith("most_improved_"):
            return "most improved"
        return "driver consistency"

    ordered_keys = sorted(keys)
    recalculated = 0
    failures = []
    for i, key in enumerate(ordered_keys):
        if progress_cb:
            progress_cb(f"Recalculating {describe(key)}... ({i + 1} of {len(ordered_keys)})", i, len(ordered_keys))
        base = key
        ignore_outliers = base.endswith(":ignore_outliers")
        if ignore_outliers:
            base = base[: -len(":ignore_outliers")]
        try:
            if key == WINS_PODIUMS_CACHE_KEY:
                _recalc_wins_podiums(org_id_int)
            elif key == DRIVER_DIRECTORY_CACHE_KEY:
                _recalc_driver_directory(org_id_int)
            elif base.startswith("classpace_"):
                types = [t for t in base[len("classpace_"):].split(",") if t in VALID_SESSION_TYPES]
                if not types:
                    continue
                _recalc_class_pace(org_id_int, types, ignore_outliers)
            elif base.startswith("most_improved_"):
                types = [t for t in base[len("most_improved_"):].split(",") if t in VALID_SESSION_TYPES]
                if not types:
                    continue
                _recalc_most_improved(org_id_int, types, ignore_outliers)
            else:
                types = [t for t in base.split(",") if t in VALID_SESSION_TYPES]
                if not types:
                    continue
                _recalc_consistency_stats(org_id_int, types, ignore_outliers)
            recalculated += 1
        except Exception as exc:
            if logger:
                logger.warning(f"Recalculate-all failed for org {org_id_int} view '{key}': {exc}")
            failures.append(key)

    return recalculated, failures, pruned


def generate_org_all_stats(org_id):
    """Start the recalculate-all background task from the Settings > Data
    button. Falls back to redirect-with-notice responses for non-JS forms."""
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    if not storage.org_has_sessions(org_id_int):
        return redirect(url_for("org_operations", org_id=org_id_int, error="No synced session data available to analyze."))

    from app.tasks import trigger_stats_recalc

    task_id = trigger_stats_recalc(org_id_int, scope="all")
    if task_id is None:
        return redirect(url_for("org_operations", org_id=org_id_int, notice="Stats recalculation is already running - it will run once more when finished to pick up your request."))
    return redirect(url_for("org_operations", org_id=org_id_int, notice="Recalculating stats in the background. Progress is shown in the Statistics row."))


def register_routes(app):
    app.add_url_rule("/org/<org_id>/stats", "org_stats", org_stats)
    app.add_url_rule("/org/<org_id>/stats/generate", "generate_org_stats", generate_org_stats, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/generate-all", "generate_org_all_stats", generate_org_all_stats, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/driver/<driver_name>", "driver_stats_breakdown", driver_stats_breakdown)
    app.add_url_rule("/org/<org_id>/stats/class-pace", "org_class_pace", org_class_pace)
    app.add_url_rule("/org/<org_id>/stats/class-pace/generate", "generate_org_class_pace", generate_org_class_pace, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/class-pace/settings", "set_class_pace_config", set_class_pace_config, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/participation", "org_participation", org_participation)
    app.add_url_rule("/org/<org_id>/stats/most-improved", "org_most_improved", org_most_improved)
    app.add_url_rule("/org/<org_id>/stats/most-improved/generate", "generate_org_most_improved", generate_org_most_improved, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/wins-podiums", "org_wins_podiums", org_wins_podiums)
    app.add_url_rule("/org/<org_id>/stats/wins-podiums/generate", "generate_org_wins_podiums", generate_org_wins_podiums, methods=["POST"])
