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
        )

    try:
        min_laps = get_stats_min_laps(org_id_int)

        from speedhive.analyzers.analyze_consistency import get_consistency_rankings
        top_consistent, least_consistent, total_drivers, total_laps_analyzed = get_consistency_rankings(
            clustered, min_laps=min_laps, limit=15
        )
        
        driver_search = (request.args.get("driver_search") or "").strip()
        search_result = None
        if driver_search:
            from speedhive.analyzers.analyze_consistency import find_driver_percentile
            res = find_driver_percentile(clustered, driver_search, min_laps=min_laps, threshold=0.85)
            if res:
                nearby_formatted = []
                for name, laps, mean_v, stdev_v, cv in res["nearby"]:
                    nearby_formatted.append({
                        "name": name,
                        "lap_count": laps,
                        "mean_display": format_seconds(mean_v) if mean_v else "N/A",
                        "stdev_display": f"{stdev_v:.3f}s" if stdev_v else "N/A",
                        "cv_display": f"{cv * 100:.2f}%" if cv is not None else "N/A",
                    })
                search_result = {
                    "matched": res["matched"],
                    "score": res["score"],
                    "rank": res["rank"],
                    "total": res["total"],
                    "percentile": round(res["percentile"], 1),
                    "nearby": nearby_formatted,
                }
        
        return render_template(
            "org_stats.html",
            org=org_view,
            org_id=org_id_int,
            org_name=org_view.get("name"),
            manifest_exists=True,
            has_persisted_stats=True,
            calculated_at=calculated_at,
            top_consistent=top_consistent,
            least_consistent=least_consistent,
            total_drivers=total_drivers,
            total_laps_analyzed=total_laps_analyzed,
            min_laps=min_laps,
            driver_search=driver_search,
            search_result=search_result,
            active_tab="stats",
            active_stats_tab="overview",
            cache_status=cache_status,
            session_types=session_types_list,
            session_types_str=session_types_str,
            ignore_outliers=ignore_outliers,
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
    session_types_str = ",".join(session_types_list)
    session_types_key = f"{session_types_str}:ignore_outliers" if ignore_outliers else session_types_str

    if not has_db_stats:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": "No synced session data available to analyze."}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for("org_stats", **redirect_args))

    try:
        from speedhive.utils.lap_analysis import compute_laps_and_enriched_from_storage
        from speedhive.analyzers.analyze_consistency import (
            load_session_types_from_storage,
            aggregate_by_name,
            cluster_names,
        )

        _, enriched = compute_laps_and_enriched_from_storage(storage, org_id_int, ignore_outliers=ignore_outliers)
        session_map = load_session_types_from_storage(storage, org_id_int)
        by_name = aggregate_by_name(enriched, session_map, session_types=session_types_list)
        clustered = cluster_names(by_name, threshold=0.85)

        calculated_at = iso_utc(utc_now())
        payload_str = json.dumps(clustered, default=str)
        with storage.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO org_stats (org_id, session_type, payload, calculated_at) VALUES (?, ?, ?, ?)",
                (org_id_int, session_types_key, payload_str, calculated_at)
            )
            conn.commit()

        # Prune older file cache if present
        cache_file = DATA_ROOT / f"org_{org_id_int}_stats_cache.json"
        if cache_file.exists():
            try:
                cache_file.unlink()
            except Exception:
                pass
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
            driver_cvs = []
            for name, stats in clustered.items():
                cv = stats.get("cv")
                lap_count = stats.get("lap_count", 0)
                if cv is not None and cv > 0.0002 and lap_count >= 10:
                    driver_cvs.append((name, cv))
            
            driver_cvs.sort(key=lambda x: x[1])
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
        from speedhive.utils.lap_analysis import (
            compute_laps_and_enriched_from_storage,
            normalize_name,
        )
        from speedhive.analyzers.analyze_consistency import (
            load_session_types_from_storage,
            matches_session_type,
        )

        laps_by_driver, enriched = compute_laps_and_enriched_from_storage(storage, org_id_int, ignore_outliers=ignore_outliers)
        session_map = load_session_types_from_storage(storage, org_id_int)
        results_payloads = storage.load_results_payloads(org_id_int)

        driver_sessions = []
        normalized_aliases = {normalize_name(a) for a in aliases}
        
        total_starts = 0
        total_wins = 0
        total_podiums = 0
        class_stats = defaultdict(lambda: {"starts": 0, "wins": 0, "podiums": 0, "best_lap": None})
        all_time_best_seconds = None

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

                        class_name = first_non_empty(
                            driver_cls,
                            session_raw.get("classification"),
                            session_raw.get("class"),
                            session_raw.get("classificationName"),
                            session_raw.get("className")
                        ) or "Unknown Class"

                        laps = laps_by_driver.get(key, [])
                        best_lap = min(laps) if laps else None

                        if best_lap is not None:
                            if all_time_best_seconds is None or best_lap < all_time_best_seconds:
                                all_time_best_seconds = best_lap

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
        
        # Calculate win/podium rates and format best lap for class_stats
        for cls, cstats in class_stats.items():
            starts = cstats["starts"]
            cstats["win_rate"] = f"{(cstats['wins'] / starts * 100):.1f}%" if starts > 0 else "0.0%"
            cstats["podium_rate"] = f"{(cstats['podiums'] / starts * 100):.1f}%" if starts > 0 else "0.0%"
            cstats["best_lap_display"] = format_seconds(cstats["best_lap"]) if cstats["best_lap"] else "N/A"

        sorted_class_stats = dict(sorted(class_stats.items(), key=lambda item: item[1]["starts"], reverse=True))
        all_time_best_display = format_seconds(all_time_best_seconds) if all_time_best_seconds else "N/A"

        # Peak career consistency (lowest CV in any session)
        valid_cvs = []
        for s in driver_sessions:
            try:
                if s.get("cv_display") != "N/A":
                    cv_val = float(s["cv_display"].replace("%", ""))
                    # Filter out exactly 0.00% or extremely small CVs (<= 0.02%)
                    # as these represent data/timing anomalies rather than real lap consistency.
                    if cv_val > 0.02:
                        valid_cvs.append((cv_val, s["session_name"]))
            except:
                pass
        if valid_cvs:
            best_cv, best_cv_sess = min(valid_cvs, key=lambda x: x[0])
            peak_consistency_val = f"{best_cv:.2f}%"
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
            except:
                return None

        chrono_sessions = sorted(
            [s for s in driver_sessions if s.get("cv_display") != "N/A" and get_valid_cv_float(s) is not None],
            key=lambda s: s["session_id"]
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
                except:
                    return None
            cv_vals = [parse_cv(s) for s in chrono_sessions]
            cv_vals = [v for v in cv_vals if v is not None]
            if len(cv_vals) >= 2:
                n = min(3, len(cv_vals) // 2)
                if n == 0:
                    n = 1
                first_avg = sum(cv_vals[:n]) / n
                last_avg = sum(cv_vals[-n:]) / n
                delta = first_avg - last_avg  # Positive is improvement (lower CV)
                if delta > 0.05:
                    trend_text = f"Improving by {delta:.2f}pp ({first_avg:.2f}% → {last_avg:.2f}%)"
                    trend_direction = "improving"
                elif delta < -0.05:
                    trend_text = f"Declining by {abs(delta):.2f}pp ({first_avg:.2f}% → {last_avg:.2f}%)"
                    trend_direction = "declining"
                else:
                    trend_text = f"Stable ({first_avg:.2f}% → {last_avg:.2f}%)"
                    trend_direction = "stable"

        # Compute best lap ranking for all drivers at this track
        driver_best_laps = {}
        for key, val in enriched.items():
            name = val.get("name")
            if not name:
                continue
            laps = laps_by_driver.get(key, [])
            filtered_laps = val.get("filtered_laps", laps)
            non_outliers = [l for l in filtered_laps if l > 0.1]
            if non_outliers:
                best = min(non_outliers)
                norm_name = normalize_name(name)
                if norm_name not in driver_best_laps or best < driver_best_laps[norm_name]["best_seconds"]:
                    driver_best_laps[norm_name] = {
                        "name": name,
                        "best_seconds": best
                    }
        
        sorted_best_laps = sorted(driver_best_laps.values(), key=lambda x: x["best_seconds"])
        total_drivers_laps = len(sorted_best_laps)
        best_lap_rank = None
        
        for idx, item in enumerate(sorted_best_laps):
            if item["name"] == driver_name or item["name"] in aliases or normalize_name(item["name"]) in normalized_aliases:
                best_lap_rank = idx + 1
                break

        # Compute starts/wins/podiums for all drivers at this track (to rank wins)
        driver_stats_all = defaultdict(lambda: {"starts": 0, "wins": 0, "podiums": 0})
        for sid, results in results_payloads.items():
            s_raw = session_map.get(sid, {})
            if matches_session_type(s_raw, "race"):
                for r in results:
                    r_name = r.get("name") or (r.get("competitor") or {}).get("name")
                    if not r_name:
                        continue
                    status = r.get("status")
                    if status != "DNS":
                        norm_name = normalize_name(r_name)
                        driver_stats_all[norm_name]["starts"] += 1
                        
                        class_pos = None
                        try:
                            class_pos = int(r.get("positionInClass"))
                        except:
                            pass
                            
                        if status == "Normal" and class_pos is not None:
                            if class_pos == 1:
                                driver_stats_all[norm_name]["wins"] += 1
                            if class_pos <= 3:
                                driver_stats_all[norm_name]["podiums"] += 1

        sorted_wins = sorted(
            [{"name_key": k, "wins": v["wins"]} for k, v in driver_stats_all.items()],
            key=lambda x: x["wins"],
            reverse=True
        )
        total_drivers_wins = len(sorted_wins)
        wins_rank = None
        for idx, item in enumerate(sorted_wins):
            norm_k = item["name_key"]
            if norm_k == normalize_name(driver_name) or norm_k in normalized_aliases:
                wins_rank = idx + 1
                break

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
            peak_consistency_val=peak_consistency_val,
            peak_consistency_sess=peak_consistency_sess,
            trend_text=trend_text,
            trend_direction=trend_direction,
            trend_timeframe=trend_timeframe,
            consistency_rank=consistency_rank,
            total_drivers_consistency=total_drivers_consistency,
            best_lap_rank=best_lap_rank,
            total_drivers_laps=total_drivers_laps,
            wins_rank=wins_rank,
            total_drivers_wins=total_drivers_wins,
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
    session_types_str = ",".join(session_types_list)
    session_types_key = f"classpace_{session_types_str}" + (":ignore_outliers" if ignore_outliers else "")

    has_db_stats = storage.org_has_sessions(org_id_int)
    if not has_db_stats:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": "No synced session data available to analyze."}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for(redirect_endpoint, **redirect_args))

    try:
        from speedhive.utils.lap_analysis import compute_laps_and_enriched_from_storage
        from speedhive.analyzers.analyze_consistency import load_session_types_from_storage
        from speedhive.analyzers.analyze_class_pace import (
            compute_avg_lap_by_class_year,
            compute_participation_by_class_year,
            compute_participation_by_year,
        )
        from speedhive.workflows.track_records import curation as track_records

        _, enriched = compute_laps_and_enriched_from_storage(storage, org_id_int, ignore_outliers=ignore_outliers)
        session_map = load_session_types_from_storage(storage, org_id_int)
        results_map = storage.load_results_payloads(org_id_int)
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

        calculated_at = iso_utc(utc_now())
        payload_str = json.dumps(chart_data, default=str)
        with storage.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO org_stats (org_id, session_type, payload, calculated_at) VALUES (?, ?, ?, ?)",
                (org_id_int, session_types_key, payload_str, calculated_at)
            )
            conn.commit()
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

            # If the database payload was generated with the old limit=15 cap,
            # automatically recalculate inline to cache the full list of drivers.
            if most_improved is not None and len(most_improved) <= 15:
                try:
                    from speedhive.utils.lap_analysis import compute_laps_and_enriched_from_storage
                    from speedhive.analyzers.analyze_consistency import get_most_improved_rankings, load_session_types_from_storage
                    
                    _, enriched = compute_laps_and_enriched_from_storage(storage, org_id_int, ignore_outliers=ignore_outliers)
                    s_map = load_session_types_from_storage(storage, org_id_int)
                    min_laps = get_stats_min_laps(org_id_int)
                    
                    full_improved, full_declined = get_most_improved_rankings(
                        enriched, s_map, session_types=session_types_list, min_laps=min_laps, limit=None
                    )
                    
                    calculated_at = iso_utc(utc_now())
                    payload_str = json.dumps({"most_improved": full_improved, "most_declined": full_declined}, default=str)
                    with storage.connect() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO org_stats (org_id, session_type, payload, calculated_at) VALUES (?, ?, ?, ?)",
                            (org_id_int, session_types_key, payload_str, calculated_at)
                        )
                        conn.commit()
                    
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
    )


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
    session_types_str = ",".join(session_types_list)
    session_types_key = f"most_improved_{session_types_str}" + (":ignore_outliers" if ignore_outliers else "")

    has_db_stats = storage.org_has_sessions(org_id_int)
    if not has_db_stats:
        redirect_args = {"org_id": org_id_int, "session_types": session_types_list, "error": "No synced session data available to analyze."}
        redirect_args["ignore_outliers"] = "1" if ignore_outliers else "0"
        return redirect(url_for("org_most_improved", **redirect_args))

    try:
        from speedhive.utils.lap_analysis import compute_laps_and_enriched_from_storage
        from speedhive.analyzers.analyze_consistency import load_session_types_from_storage, get_most_improved_rankings

        _, enriched = compute_laps_and_enriched_from_storage(storage, org_id_int, ignore_outliers=ignore_outliers)
        session_map = load_session_types_from_storage(storage, org_id_int)
        min_laps = get_stats_min_laps(org_id_int)
        most_improved, most_declined = get_most_improved_rankings(
            enriched, session_map, session_types=session_types_list, min_laps=min_laps, limit=None
        )

        calculated_at = iso_utc(utc_now())
        payload_str = json.dumps({"most_improved": most_improved, "most_declined": most_declined}, default=str)
        with storage.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO org_stats (org_id, session_type, payload, calculated_at) VALUES (?, ?, ?, ?)",
                (org_id_int, session_types_key, payload_str, calculated_at)
            )
            conn.commit()
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

            # If the database payload was generated with the old limit=15 cap,
            # automatically recalculate inline to cache the full list of drivers.
            if most_wins is not None and len(most_wins) <= 15:
                try:
                    from speedhive.analyzers.analyze_consistency import load_session_types_from_storage
                    from speedhive.analyzers.analyze_results import get_wins_podiums_rankings
                    results_payloads = storage.load_results_payloads(org_id_int)
                    s_map = load_session_types_from_storage(storage, org_id_int)
                    
                    full_wins, full_podiums = get_wins_podiums_rankings(results_payloads, s_map, limit=None)
                    
                    calculated_at = iso_utc(utc_now())
                    payload_str = json.dumps({"most_wins": full_wins, "most_podiums": full_podiums}, default=str)
                    with storage.connect() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO org_stats (org_id, session_type, payload, calculated_at) VALUES (?, ?, ?, ?)",
                            (org_id_int, WINS_PODIUMS_CACHE_KEY, payload_str, calculated_at)
                        )
                        conn.commit()
                        
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
    )


def generate_org_wins_podiums(org_id):
    try:
        org_id_int = int(org_id)
    except (TypeError, ValueError):
        return redirect(url_for("index", error="Invalid organization ID."))

    has_db_stats = storage.org_has_sessions(org_id_int)
    if not has_db_stats:
        return redirect(url_for("org_wins_podiums", org_id=org_id_int, error="No synced session data available to analyze."))

    try:
        from speedhive.analyzers.analyze_consistency import load_session_types_from_storage
        from speedhive.analyzers.analyze_results import get_wins_podiums_rankings

        results_payloads = storage.load_results_payloads(org_id_int)
        session_map = load_session_types_from_storage(storage, org_id_int)
        most_wins, most_podiums = get_wins_podiums_rankings(results_payloads, session_map, limit=None)

        calculated_at = iso_utc(utc_now())
        payload_str = json.dumps({"most_wins": most_wins, "most_podiums": most_podiums}, default=str)
        with storage.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO org_stats (org_id, session_type, payload, calculated_at) VALUES (?, ?, ?, ?)",
                (org_id_int, WINS_PODIUMS_CACHE_KEY, payload_str, calculated_at)
            )
            conn.commit()
    except Exception as exc:
        return redirect(url_for("org_wins_podiums", org_id=org_id_int, error=f"Analysis failed: {exc}"))

    return redirect(url_for("org_wins_podiums", org_id=org_id_int))


def register_routes(app):
    app.add_url_rule("/org/<org_id>/stats", "org_stats", org_stats)
    app.add_url_rule("/org/<org_id>/stats/generate", "generate_org_stats", generate_org_stats, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/driver/<driver_name>", "driver_stats_breakdown", driver_stats_breakdown)
    app.add_url_rule("/org/<org_id>/stats/class-pace", "org_class_pace", org_class_pace)
    app.add_url_rule("/org/<org_id>/stats/class-pace/generate", "generate_org_class_pace", generate_org_class_pace, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/class-pace/settings", "set_class_pace_config", set_class_pace_config, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/participation", "org_participation", org_participation)
    app.add_url_rule("/org/<org_id>/stats/most-improved", "org_most_improved", org_most_improved)
    app.add_url_rule("/org/<org_id>/stats/most-improved/generate", "generate_org_most_improved", generate_org_most_improved, methods=["POST"])
    app.add_url_rule("/org/<org_id>/stats/wins-podiums", "org_wins_podiums", org_wins_podiums)
    app.add_url_rule("/org/<org_id>/stats/wins-podiums/generate", "generate_org_wins_podiums", generate_org_wins_podiums, methods=["POST"])
