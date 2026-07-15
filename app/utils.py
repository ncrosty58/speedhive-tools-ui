import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from speedhive.utils.lap_analysis import first_non_empty, extract_iso_date


def parse_date_to_comparison(dt_str):
    """Parse common date strings for filtering comparisons."""
    if not dt_str:
        return None
    try:
        # standard ISO format: 2026-06-06T12:00:00Z or similar
        # slice first 10 characters for YYYY-MM-DD
        return datetime.strptime(dt_str[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _country_name_from_value(value: Any) -> Optional[str]:
    """Normalize country-like values to a readable name."""
    if value is None:
        return None
    if isinstance(value, dict):
        return first_non_empty(
            value.get("name"),
            value.get("fullName"),
            value.get("alpha2"),
            value.get("alpha3"),
            value.get("code"),
        )
    text = str(value).strip()
    return text or None


def _org_display_name_from_cache(org_id: int) -> str:
    """Return org name from primary cache only, with stable fallback."""
    from app import storage
    db_payload = storage.get_organization(org_id).payload
    if isinstance(db_payload, dict):
        name = first_non_empty(db_payload.get("name"), db_payload.get("organizationName"))
        if name:
            return str(name)
    return f"Organization #{org_id}"


def extract_event_datetime(raw: Dict[str, Any]) -> Optional[str]:
    """Extract event/session datetime using common API keys."""
    if not isinstance(raw, dict):
        return None
    for key in (
        "startTime",
        "startDate",
        "startDateTime",
        "scheduledStart",
        "date",
        "start",
        "eventDate",
        "event_date",
    ):
        value = raw.get(key)
        if value:
            return str(value)
    return extract_iso_date(raw)


def format_datetime_display(value: Any, include_time: bool = True) -> Optional[str]:
    """Format API datetime-like values into readable strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            dt = datetime.utcfromtimestamp(float(value))
            return dt.strftime("%Y-%m-%d %H:%M") if include_time else dt.strftime("%Y-%m-%d")
        except Exception:
            return str(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M") if include_time else dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return text


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def read_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def cache_age_seconds(saved_at: Optional[datetime]) -> Optional[float]:
    if not saved_at:
        return None
    return max((utc_now() - saved_at).total_seconds(), 0.0)


def cache_meta(saved_at: Optional[datetime], source: str, error: Optional[str] = None) -> Dict[str, Any]:
    age = cache_age_seconds(saved_at)
    return {
        "saved_at": iso_utc(saved_at) if saved_at else None,
        "age_seconds": age,
        "age_hours": (age / 3600.0) if age is not None else None,
        "source": source,
        "stale": saved_at is None,
        "error": error,
    }


# Export format_seconds from lap_analysis for backward compatibility
from speedhive.utils.lap_analysis import format_seconds  # noqa: E402, F401

