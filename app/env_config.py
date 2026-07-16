"""Per-org settings backed by real environment variables, rather than a
UI-only config file -- so a value set here is also what a CLI invocation
(speedhive ... --org ID) sees, as long as it loads the same file.

Values live in web_data/org_settings.env (not the top-level .env): that
directory is already bind-mounted read-write into the container, whereas
bind-mounting a single file (like .env directly) breaks the atomic
rewrite dotenv's set_key() does internally (os.replace onto a
single-file bind mount fails with "Device or resource busy"). Both this
app and speedhive.cli.main load web_data/org_settings.env in addition to
the top-level .env, using the same SPEEDHIVE_WEB_DATA_DIR convention, so a
value saved through the UI reaches CLI invocations against the same
web_data directory too.

Naming convention: `{NAME}_{org_id}`, e.g. GEMINI_API_KEY_30476. Readers
(speedhive.llm.get_gemini_api_key, etc.) fall back to the bare `{NAME}` as a
shared default when an org hasn't set its own.
"""
import os
from typing import Optional

from dotenv import set_key, unset_key


def org_settings_path() -> str:
    from app import web_data_root
    path = web_data_root / "org_settings.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return str(path)


def get_org_env_var(name: str, org_id: int) -> Optional[str]:
    """The effective value for actually USING a setting: this org's own
    override if set, otherwise the shared bare-name fallback."""
    return os.environ.get(f"{name}_{org_id}") or os.environ.get(name)


def get_org_env_var_override(name: str, org_id: int) -> Optional[str]:
    """This org's own explicit value only -- never the shared fallback. Use
    this (not get_org_env_var) to populate a settings form field: showing
    the shared/global secret's value in a per-org field would look like it
    belongs to this org, and silently pins it as an org-specific override
    the next time the form is saved."""
    return os.environ.get(f"{name}_{org_id}")


def has_global_default(name: str) -> bool:
    return bool(os.environ.get(name))


def get_org_env_var_with_source(name: str, org_id: int):
    """Returns (effective_value, source), where source is 'org' (this org's
    own override), 'global' (the shared fallback), or None (not configured
    anywhere) -- for building a "how is this actually configured" summary,
    as opposed to get_org_env_var_override()'s "what should the edit form
    show" (which never reveals the shared value)."""
    org_value = os.environ.get(f"{name}_{org_id}")
    if org_value:
        return org_value, "org"
    global_value = os.environ.get(name)
    if global_value:
        return global_value, "global"
    return None, None


def set_org_env_var(name: str, org_id: int, value: Optional[str]) -> None:
    """Persist `value` under the org-scoped key, and apply it to the current
    process immediately so the already-running app reflects the change
    without needing a restart."""
    key = f"{name}_{org_id}"
    path = org_settings_path()
    if value:
        set_key(path, key, value)
        os.environ[key] = value
    else:
        unset_key(path, key)
        os.environ.pop(key, None)
