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
    return os.environ.get(f"{name}_{org_id}") or os.environ.get(name)


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
