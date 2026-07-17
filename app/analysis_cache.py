"""Process-wide cache for the org-wide lap-analysis bundle.

Every driver report, stats recalculation, and pace/improvement analysis
starts from the same org-wide computation: parse every synced session's
laps, drop duplicated sessions, and compute per-driver per-session lap
statistics. For a large org that takes ~15-20s and it doesn't depend on
which driver was clicked -- only on the synced data -- so it's computed
once per (org, ignore_outliers) and reused until a sync writes new
session rows (detected via a cheap row-count/max-saved_at stamp).

Bundles are large (~175MB for a 4,400-session org), so the LRU is tiny:
big enough for one org's two ignore_outliers variants. Treat bundle
contents as read-only -- they're shared across requests.
"""
import threading
from collections import OrderedDict

from speedhive.utils.lap_analysis import (
    _compute_laps_and_enriched_from_payloads,
    dedupe_session_ids,
)

_MAX_BUNDLES = 2
_lock = threading.Lock()
_cache: "OrderedDict[tuple, tuple]" = OrderedDict()


def _data_stamp(storage, org_id):
    with storage.connect() as conn:
        laps_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(saved_at), '') FROM session_laps WHERE org_id = ?",
            (org_id,),
        ).fetchone()
        results_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(saved_at), '') FROM session_results WHERE org_id = ?",
            (org_id,),
        ).fetchone()
    return (tuple(laps_row), tuple(results_row))


def get_org_analysis(storage, org_id, ignore_outliers):
    """Return the org's analysis bundle, computed once per data version:

    {"laps_by_driver": ..., "enriched": ..., "results_payloads": ...}

    laps_by_driver/enriched are what compute_laps_and_enriched_from_storage
    returns; results_payloads has duplicated sessions already removed, ready
    for start/win/podium counting. Contents are shared -- do not mutate.
    """
    key = (int(org_id), bool(ignore_outliers))
    stamp = _data_stamp(storage, org_id)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            _cache.move_to_end(key)
            return hit[1]

        # Build inside the lock: concurrent cold requests wait for one build
        # instead of each spending ~20s and several hundred MB repeating it.
        sessions = storage.load_session_payloads(org_id)
        results_payloads = storage.load_results_payloads(org_id)
        laps_payloads = storage.load_laps_payloads(org_id)
        keep_sids = dedupe_session_ids(results_payloads, laps_payloads)
        results_payloads = {sid: rows for sid, rows in results_payloads.items() if sid in keep_sids}
        laps_by_driver, enriched = _compute_laps_and_enriched_from_payloads(
            sessions,
            results_payloads,
            laps_payloads,
            ignore_outliers=ignore_outliers,
            keep_sids=keep_sids,
        )
        bundle = {
            "laps_by_driver": laps_by_driver,
            "enriched": enriched,
            "results_payloads": results_payloads,
        }
        _cache[key] = (stamp, bundle)
        while len(_cache) > _MAX_BUNDLES:
            _cache.popitem(last=False)
        return bundle
