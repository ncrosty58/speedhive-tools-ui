#!/usr/bin/env python3
"""CLI wrapper for the WHRRI track-records sync/diff pipeline (see
../whrri_track_records.py for the actual logic, shared with app.py's
/api/org/<id>/track-records/sync route). Useful for manual/debug runs on the
box; scheduled runs are driven by GitLab CI hitting the HTTP API instead.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whrri_track_records import run_sync_and_diff


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", type=int, default=30476)
    parser.add_argument("--no-sync", action="store_true", help="Skip sync-org, use existing cache as-is")
    parser.add_argument("--full", action="store_true", help="Force a full resync instead of incremental")
    parser.add_argument("--force", action="store_true", help="Sync even if the cache already looks fresh")
    args = parser.parse_args(argv)

    try:
        summary = run_sync_and_diff(
            args.org,
            do_sync=not args.no_sync,
            full=args.full,
            force=args.force,
            progress_cb=print,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
