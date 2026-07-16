# Next Session Plan

_Saved 2026-07-16, end of a long session covering nav reorg, curated track-record
editing, a historical-ledger diff-logic fix, duplicate cleanup, and a
data-matching investigation. All changes described as "done" below are deployed
live and passing both test suites (103 app tests + 84 speedhive-tools tests)._

## 1. Re-audit the "missing from Speedhive" analysis with normalized matching

While investigating why some curated records (e.g. Steve Ives, and ~114 others)
don't show up in Live Lookup, we found the "no raw match" check was too strict:
it compares `lapTime` and `driverName` as exact strings, which misses real
matches due to:

- **Lap-time format**: curated stores sub-minute times as `"0:59.439"`; raw
  announcer data stores them as `"59.439"` (no leading `0:`). Confirmed via
  Jonathan Finstrom's P1 record.
- **Driver-name format**: curated sometimes omits a middle name/initial that
  the raw announcement includes (e.g. "Andrew Abbott" vs "Andrew T Abbott").
- **Small rounding/transcription differences**: e.g. curated `1:06.897` vs raw
  `1:06.896` for Saylor Frase (ASR); curated `1:11.501` vs raw `1:11.499` for
  Paul Young (GT3). Likely artifacts from whenever the original curated list
  was hand-transcribed.
- **Structurally unmatchable by design**: `F5-2-stroke`/`F5-4-stroke` curated
  records can never auto-match, because "F5" is in this org's
  `always_review` alias-map list -- the raw announcement only ever says the
  bare ambiguous token "F5"; a human manually resolved it to the specific
  stroke variant when approving it.

**To do:**
- Rebuild the "no raw match" check using normalized comparisons instead of
  exact string equality:
  - Parse both sides' lap times to seconds-as-float (reuse
    `lap_time_to_seconds` from `curation.py`) and compare numerically with a
    small tolerance (~0.01s), not string equality.
  - Compare driver names fuzzily (strip middle names/initials, or reuse
    `normalize_name()` from `analyze_consistency.py` if it's a good fit)
    instead of exact string match.
- Re-run the "114 no-match" analysis with the improved matching to get an
  accurate count of what's genuinely missing from the synced cache vs. just
  format-mismatched.
- **Re-check the dedupe cleanup already run** (130 rows removed from org
  30476's curated list) -- it used the same exact-string `(class, lapTime,
  driverName)` matching, so it may have missed duplicates that differ only in
  lap-time format or name formatting. Worth a second pass with normalized
  matching once that's built.
- Consider whether `run_sync_and_diff`'s own candidate-identity matching
  (`curated_ldc` / `rejected_ldc` in `curation.py`) should move to the same
  normalized comparison, so future scans don't propose false "new record"
  candidates for records that already exist under a slightly different
  string format.

## 2. Event detail page styling (Overview tab > Open event)

Already fixed once this session:
- Replaced undefined/orphaned CSS classes (`.breadcrumb-custom`, `.page-header`,
  `.glass-card-header`, `.text-gradient`) across `event.html`, `championship.html`,
  `results.html`, `lap_times.html` with the site's real, already-established
  classes (matching `index.html`/`org_operations.html` conventions).
- Added missing `.bg-surface` / `.bg-surface-2` / `.badge-alert` / `.badge-ok`
  utility classes to `base.html` (were referenced ~34 places across the site
  but never actually defined).
- Fixed `results.html`'s session tabs, which were unstyled default Bootstrap
  (light theme) -- now use the shared `.nav-tabs-custom` dark styling.

**Still outstanding per user:** more styling work remains on this page. User's
own hypothesis: part of what looks broken may actually be **incomplete synced
data** (missing venue/date/session fields rendering blank/N/A) rather than a
CSS bug. **User is running a full Speedhive sync tonight** -- once that
completes, re-examine the Event detail page with fuller data before doing any
more CSS work, to separate genuine remaining layout bugs from data-completeness
artifacts.

## 3. Stats page: average lap time per class, by year (chart)

New Stats sub-page: a chart showing how each class's average lap time has
progressed year over year. Research already done this session (via an Explore
subagent) on what's reusable:

- `compute_laps_and_enriched_from_storage(storage, org_id, ignore_outliers)`
  (`speedhive-tools/src/speedhive/utils/lap_analysis.py`) -- gives per
  driver-session `filtered_laps` (outlier-filtered lap times), keyed by
  `session{sid}_pos{pos}`.
- `load_session_types_from_storage(storage, org_id)`
  (`speedhive/analyzers/analyze_consistency.py`) -- session_id -> raw session
  dict; use with `matches_session_type(session_raw, type)` to filter by
  race/qualifying/practice, same as `aggregate_by_name` already does.
- Per-session car-class extraction pattern already used in
  `driver_stats_breakdown` (`app/routes/stats.py`):
  `first_non_empty(session_raw.get("classification"), session_raw.get("class"),
  session_raw.get("classificationName"), session_raw.get("className"))`.
- **No existing year-extraction helper** -- derive year from the session's date
  fields (same `first_non_empty(startTime, scheduledStart, start_date, date)`
  pattern `driver_stats_breakdown` uses), then take the first 4 chars /
  `.year` of the parsed date.
- **No existing per-(class, year) grouping function** -- write one fresh,
  structurally similar to `aggregate_by_name`/`cluster_names` but keyed on
  (car class, event year) instead of driver name, pooling all filtered laps
  for each group and averaging.

**Plan:**
- Add a new analyzer module in speedhive-tools, e.g.
  `speedhive/analyzers/analyze_class_pace.py`, with a function like
  `compute_avg_lap_by_class_year(enriched, session_map, session_types=["race"])`
  returning `{"years": [...], "classes": [...], "series": {class_name: [avg_seconds_or_None, ...]}, "counts": {...}}`.
- New route + template (e.g. `/org/<id>/stats/class-pace`), with its own
  "Generate/Recalculate" trigger mirroring the existing Stats page's
  cache-in-`org_stats`-table pattern (use a distinct `session_type` cache key
  like `"classpace_<session_types>"` so it can't collide with the existing
  driver-consistency cache entries).
- Chart.js multi-line chart: one line per class, x-axis = year, y-axis =
  average lap time. Reference `templates/lap_times.html`'s existing Chart.js
  integration for the dark-theme wiring (reads `--brand` etc. from CSS custom
  properties at runtime) -- but that one's a single-series chart; this needs
  multiple distinctly-colored series.
- **Before writing any chart color code**: re-invoke the `dataviz` skill (was
  loaded once this session but got interrupted before use) for the categorical
  palette -- fixed hue order, run the skill's validator script for
  colorblind-safety, don't hand-roll colors. Also add a legend (>=2 series
  always needs one) and a small sub-nav entry under the Stats tab, mirroring
  the Track Records tab's sub-nav pattern already built this session
  (`_track_records_subnav.html`).

## Also not yet actioned (lower priority, mentioned but not agreed to)

- **37-41 pre-existing duplicate groups** in org 30476's curated list --
  same "same record, different date convention" pattern as the 130 rows
  cleaned up tonight, but these predate all of today's work (both entries in
  each group are pre-existing, not something I introduced). Separate,
  longer-standing data question. Offered to clean these up too; user hadn't
  responded when the session ended.
