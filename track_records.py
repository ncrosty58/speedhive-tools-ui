"""Generic, per-organization track-records sync/diff logic.

Pulls Speedhive's announcer-flagged "New Track/Class Record" data for a given
org, normalizes classification tokens against that org's own alias map (no
hardcoded org-specific data lives here), and diffs against a per-org curated
file -- new/changed rows only ever land in a per-org candidates_pending.json
for a human to review, never written to curated.json directly.

Works for any org_id; nothing here is specific to any one club/organization.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from speedhive.processing.process_track_records import extract_records_from_storage

GOTIFY_URL = os.environ.get("GOTIFY_URL")
GOTIFY_APP_TOKEN = os.environ.get("GOTIFY_APP_TOKEN")
# Speedhive syncs are slow (lots of data per event) -- only re-sync if the
# cache is older than this, unless the caller explicitly forces it.
DEFAULT_STALE_AFTER_HOURS = float(os.environ.get("TRACK_RECORDS_STALE_HOURS", "20"))


def org_track_records_dir(track_records_root: Path, org_id: int) -> Path:
    return Path(track_records_root) / str(org_id)


def paths_for_org(track_records_root: Path, org_id: int) -> dict:
    d = org_track_records_dir(track_records_root, org_id)
    return {
        "dir": d,
        "curated": d / "curated.json",
        "candidates": d / "candidates_pending.json",
        "rejected": d / "rejected.json",
        "alias_map": d / "class_alias_map.json",
        "history": d / "history",
        "tasks": d / "tasks",
    }


def load_json(path, default):
    if not Path(path).exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def lap_time_to_seconds(lap_time):
    """Parse 'm:ss.mmm' or 'ss.mmm' into float seconds; None if unparseable."""
    if not lap_time:
        return None
    parts = str(lap_time).split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None


def normalize_classification(raw_token, alias_map):
    """Returns (status, resolved_abbreviation). status: 'ok' | 'ambiguous'.

    No canonical whitelist is required -- any token is accepted as-is (upper/
    trimmed) unless it's in this org's `always_review` list (for tokens that
    are genuinely ambiguous, e.g. a combined class group that splits into
    multiple record-keeping classes). The human review step is the real
    safety net for typos/unexpected tokens, not a whitelist.
    """
    if not raw_token:
        return "ambiguous", None
    token = raw_token.strip().upper()

    if token in {t.strip().upper() for t in alias_map.get("always_review", [])}:
        return "ambiguous", None

    aliases = {k.strip().upper(): v for k, v in alias_map.get("aliases", {}).items()}
    if token in aliases:
        token = aliases[token].strip().upper()

    return "ok", token


def notify_gotify(title, message):
    if not GOTIFY_URL or not GOTIFY_APP_TOKEN:
        return
    try:
        data = urllib.parse.urlencode({"title": title, "message": message, "priority": 5}).encode()
        url = f"{GOTIFY_URL.rstrip('/')}/message?token={GOTIFY_APP_TOKEN}"
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as exc:
        print(f"Gotify notification failed: {exc}", file=sys.stderr)


def build_curated_fastest_index(curated):
    """Map classAbbreviation -> fastest curated record (dict, plus '_seconds')."""
    fastest = {}
    for r in curated.get("records", []):
        cls = r["classAbbreviation"]
        secs = lap_time_to_seconds(r.get("lapTime"))
        if secs is None:
            continue
        if cls not in fastest or secs < fastest[cls]["_seconds"]:
            entry = dict(r)
            entry["_seconds"] = secs
            fastest[cls] = entry
    return fastest


def rejected_key(classAbbreviation, lapTime, driverName, date):
    return (classAbbreviation, lapTime, driverName, date)


def get_cache_status(org_id, db_path, track_records_root):
    """Freshness info for the Speedhive cache -- no network calls."""
    from speedhive.storage import SpeedhiveStorage

    p = paths_for_org(track_records_root, org_id)
    db_path = Path(db_path)

    candidates_payload = load_json(p["candidates"], {"candidates": []})
    pending_candidates = len(candidates_payload.get("candidates", []))

    if not db_path.exists():
        return {
            "org_id": org_id,
            "last_synced_at": None,
            "age_hours": None,
            "needs_sync": True,
            "stale_after_hours": DEFAULT_STALE_AFTER_HOURS,
            "pending_candidates": pending_candidates,
        }

    storage = SpeedhiveStorage(db_path)
    state = storage.get_org_status(org_id) or {}
    last_refresh_at = state.get("last_refresh_at")
    age_hours = None
    needs_sync = True
    if last_refresh_at:
        try:
            last_dt = datetime.fromisoformat(str(last_refresh_at).replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
            needs_sync = age_hours >= DEFAULT_STALE_AFTER_HOURS
        except Exception:
            pass

    return {
        "org_id": org_id,
        "last_synced_at": last_refresh_at,
        "age_hours": age_hours,
        "needs_sync": needs_sync,
        "stale_after_hours": DEFAULT_STALE_AFTER_HOURS,
        "pending_candidates": pending_candidates,
    }


def run_sync_and_diff(org_id, db_path, track_records_root, progress_cb=None):
    """Extract + normalize + diff for one org, against the already-synced cache
    at db_path. Does NOT perform the Speedhive sync itself -- callers (the
    Flask route) are responsible for refreshing db_path first if needed, using
    the existing generic refresh_org_cache machinery this app already has.
    Returns a summary dict.
    """
    def report(phase):
        if progress_cb:
            progress_cb(phase)

    p = paths_for_org(track_records_root, org_id)
    db_path = Path(db_path)
    if not db_path.exists():
        raise RuntimeError(f"No cache at {db_path}; sync the org first.")

    report("Extracting announcer-flagged records")
    raw_records = extract_records_from_storage(org_id, db_path)

    report("Normalizing and diffing against curated records")
    alias_map = load_json(p["alias_map"], {"aliases": {}, "always_review": []})
    curated = load_json(p["curated"], {"date": None, "records": []})
    rejected_rows = load_json(p["rejected"], {"rejected": []}).get("rejected", [])
    rejected_keys = {
        rejected_key(r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"), r.get("date"))
        for r in rejected_rows
    }

    curated_fastest = build_curated_fastest_index(curated)

    best_by_class = {}
    flagged = []
    seen_flagged_keys = set()

    for row in raw_records:
        status, resolved = normalize_classification(row.get("classification"), alias_map)
        secs = row.get("lap_time_seconds")
        if secs is None:
            secs = lap_time_to_seconds(row.get("lap_time"))
        ts = row.get("timestamp")
        date_str = str(ts)[:10] if ts else None

        if status != "ok":
            key = rejected_key(row.get("classification"), row.get("lap_time"), row.get("driver"), date_str)
            if key in rejected_keys or key in seen_flagged_keys:
                continue
            seen_flagged_keys.add(key)
            flagged.append({
                "action": "unmapped_classification",
                "reason": status,
                "current": None,
                "proposed": {
                    "classAbbreviation": row.get("classification"),
                    "lapTime": row.get("lap_time"),
                    "driverName": row.get("driver"),
                    "marque": row.get("marque"),
                    "date": date_str,
                },
                "raw": {
                    "event_name": row.get("event_name"),
                    "session_name": row.get("session_name"),
                    "text": row.get("text"),
                },
            })
            continue

        if secs is None:
            continue
        if resolved not in best_by_class or secs < best_by_class[resolved]["_seconds"]:
            entry = dict(row)
            entry["_seconds"] = secs
            entry["_resolved"] = resolved
            entry["_date"] = date_str
            best_by_class[resolved] = entry

    candidates = []
    for cls, entry in best_by_class.items():
        proposed = {
            "classAbbreviation": cls,
            "lapTime": entry.get("lap_time"),
            "driverName": entry.get("driver"),
            "marque": entry.get("marque"),
            "date": entry.get("_date"),
        }
        key = rejected_key(cls, proposed["lapTime"], proposed["driverName"], proposed["date"])
        if key in rejected_keys:
            continue

        current = curated_fastest.get(cls)
        if current is not None and entry["_seconds"] >= current["_seconds"]:
            continue  # Speedhive doesn't know a time faster than what's already curated

        current_public = None
        if current is not None:
            current_public = {k: current[k] for k in ("classAbbreviation", "lapTime", "driverName", "marque", "date")}

        candidates.append({
            "action": "new_record",
            "classAbbreviation": cls,
            "current": current_public,
            "proposed": proposed,
            "raw": {
                "event_name": entry.get("event_name"),
                "session_name": entry.get("session_name"),
                "text": entry.get("text"),
            },
        })

    candidates.extend(flagged)

    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "org_id": org_id,
        "candidates": candidates,
    }
    save_json(p["candidates"], payload)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(p["history"] / f"candidates_{stamp}.json", payload)

    new_count = sum(1 for c in candidates if c["action"] == "new_record")
    unmapped_count = sum(1 for c in candidates if c["action"] == "unmapped_classification")

    if candidates:
        notify_gotify(
            f"Track records: new candidates for org {org_id}",
            f"{len(candidates)} candidate(s) waiting for review at /org/{org_id}/track-records/review "
            f"({new_count} new record(s), {unmapped_count} unmapped classification(s)).",
        )

    report("Done")
    return {
        "raw_records_scanned": len(raw_records),
        "candidates_found": len(candidates),
        "new_record_candidates": new_count,
        "unmapped_candidates": unmapped_count,
        "generated_at": payload["generated_at"],
    }
