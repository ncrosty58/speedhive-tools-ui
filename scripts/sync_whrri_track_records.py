#!/usr/bin/env python3
"""Pull WHRRI track-record announcements from Speedhive and produce reviewable
candidates. Never writes to curated.json directly -- new/changed rows only ever
land in candidates_pending.json for a human to approve via /track-records/review.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "speedhive-tools", "src"))

from speedhive.wrapper import SpeedhiveClient
from speedhive.exporters.export_org_cache import refresh_org_cache
from speedhive.processing.process_track_records import extract_records_from_storage

APP_ROOT = Path(__file__).resolve().parent.parent
WEB_DATA_ROOT = Path(os.environ.get("SPEEDHIVE_WEB_DATA_DIR", APP_ROOT / "web_data"))
DB_PATH = Path(os.environ.get("SPEEDHIVE_DB_PATH", WEB_DATA_ROOT / "speedhive.db"))
TRACK_RECORDS_DIR = WEB_DATA_ROOT / "track_records"
CURATED_PATH = TRACK_RECORDS_DIR / "curated.json"
CANDIDATES_PATH = TRACK_RECORDS_DIR / "candidates_pending.json"
REJECTED_PATH = TRACK_RECORDS_DIR / "rejected.json"
HISTORY_DIR = TRACK_RECORDS_DIR / "history"
CLASS_ALIAS_PATH = TRACK_RECORDS_DIR / "class_alias_map.json"
CANONICAL_CLASSES_PATH = APP_ROOT / "data" / "whrri_class_abbreviations.json"

MAX_ORG_EVENTS = int(os.environ.get("SPEEDHIVE_MAX_ORG_EVENTS", "150"))
GOTIFY_URL = os.environ.get("GOTIFY_URL")
GOTIFY_APP_TOKEN = os.environ.get("GOTIFY_APP_TOKEN")


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


def normalize_classification(raw_token, alias_map, canonical_classes):
    """Returns (status, resolved_abbreviation). status: 'ok' | 'ambiguous' | 'unknown'."""
    if not raw_token:
        return "unknown", None
    token = raw_token.strip().upper()

    if token in {t.strip().upper() for t in alias_map.get("always_review", [])}:
        return "ambiguous", None

    aliases = {k.strip().upper(): v for k, v in alias_map.get("aliases", {}).items()}
    if token in aliases:
        token = aliases[token].strip().upper()

    canonical_by_upper = {c.strip().upper(): c for c in canonical_classes}
    if token in canonical_by_upper:
        return "ok", canonical_by_upper[token]

    return "unknown", None


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", type=int, default=30476)
    parser.add_argument("--no-sync", action="store_true", help="Skip sync-org, use existing cache as-is")
    parser.add_argument("--full", action="store_true", help="Force a full resync instead of incremental")
    args = parser.parse_args(argv)

    if not args.no_sync:
        client = SpeedhiveClient.create()
        mode = "full" if args.full else "incremental"
        summary = refresh_org_cache(
            client=client,
            org_id=args.org,
            mode=mode,
            max_events=MAX_ORG_EVENTS,
            recent_backfill_events=20 if mode == "incremental" else 0,
            cleanup_on_full=True,
            db_path=DB_PATH,
        )
        print(f"Synced org {args.org} ({mode}): {summary}")

    if not DB_PATH.exists():
        print(f"Error: no cache at {DB_PATH}. Run without --no-sync first.", file=sys.stderr)
        return 1

    raw_records = extract_records_from_storage(args.org, DB_PATH)
    print(f"Extracted {len(raw_records)} raw announcer-flagged record(s)")

    alias_map = load_json(CLASS_ALIAS_PATH, {"aliases": {}, "always_review": []})
    canonical = load_json(CANONICAL_CLASSES_PATH, {"class_abbreviations": []})["class_abbreviations"]
    curated = load_json(CURATED_PATH, {"date": None, "records": []})
    rejected_rows = load_json(REJECTED_PATH, {"rejected": []}).get("rejected", [])
    rejected_keys = {
        rejected_key(r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"), r.get("date"))
        for r in rejected_rows
    }

    curated_fastest = build_curated_fastest_index(curated)

    best_by_class = {}
    flagged = []
    seen_flagged_keys = set()

    for row in raw_records:
        status, resolved = normalize_classification(row.get("classification"), alias_map, canonical)
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
        "org_id": args.org,
        "candidates": candidates,
    }
    save_json(CANDIDATES_PATH, payload)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(HISTORY_DIR / f"candidates_{stamp}.json", payload)

    print(f"Wrote {len(candidates)} candidate(s) to {CANDIDATES_PATH}")

    if candidates:
        new_count = sum(1 for c in candidates if c["action"] == "new_record")
        unmapped_count = sum(1 for c in candidates if c["action"] == "unmapped_classification")
        notify_gotify(
            "WHRRI track records: new candidates",
            f"{len(candidates)} candidate(s) waiting for review at /track-records/review "
            f"({new_count} new record(s), {unmapped_count} unmapped classification(s)).",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
