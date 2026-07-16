"""Settings backed by real environment variables, rather than a UI-only
config file -- so a value set here is also what a CLI invocation
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

Two kinds of settings live here:
- Per-org (Gemini key/model): `{NAME}_{org_id}`, e.g. GEMINI_API_KEY_30476,
  via get_org_env_var/set_org_env_var. Falls back to the bare `{NAME}` as a
  shared default when an org hasn't set its own.
- Shared/app-wide (Resend/notification credentials -- one set of values for
  the whole install, not one per org): the bare `{NAME}` only, via
  set_global_env_var/os.environ.get directly.
"""
import json
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
    return get_org_env_var_override(name, org_id) or os.environ.get(name)


def get_org_env_var_override(name: str, org_id: int) -> Optional[str]:
    """This org's own explicit value only -- never the shared fallback. Use
    this (not get_org_env_var) to populate a settings form field: showing
    the shared/global secret's value in a per-org field would look like it
    belongs to this org, and silently pins it as an org-specific override
    the next time the form is saved."""
    from app import web_data_root
    config_path = web_data_root / "orgs" / str(org_id) / "settings.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
            override = config.get("overrides", {}).get(name)
            if override:
                return override
        except Exception:
            pass
    return os.environ.get(f"{name}_{org_id}")


def has_global_default(name: str) -> bool:
    return bool(os.environ.get(name))


def get_org_env_var_with_source(name: str, org_id: int):
    """Returns (effective_value, source), where source is 'org' (this org's
    own override), 'global' (the shared fallback), or None (not configured
    anywhere) -- for building a "how is this actually configured" summary,
    as opposed to get_org_env_var_override()'s "what should the edit form
    show" (which never reveals the shared value)."""
    org_value = get_org_env_var_override(name, org_id)
    if org_value:
        return org_value, "org"
    global_value = os.environ.get(name)
    if global_value:
        return global_value, "global"
    return None, None


def set_org_env_var(name: str, org_id: int, value: Optional[str]) -> None:
    """Persist `value` under the org-scoped overrides block in its settings.json,
    and apply it to the current process environment immediately so the
    already-running app reflects the change without needing a restart."""
    from app import web_data_root
    config_path = web_data_root / "orgs" / str(org_id) / "settings.json"
    
    config = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass

    if "overrides" not in config:
        config["overrides"] = {}

    if value:
        config["overrides"][name] = value
    else:
        config["overrides"].pop(name, None)

    if not config["overrides"]:
        config.pop("overrides", None)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    key = f"{name}_{org_id}"
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)


def set_global_env_var(name: str, value: Optional[str]) -> None:
    """Same as set_org_env_var, but for a setting that's shared app-wide
    rather than per-org (e.g. Resend/notification credentials -- one set of
    values for the whole install, not one per org)."""
    path = org_settings_path()
    if value:
        set_key(path, name, value)
        os.environ[name] = value
    else:
        unset_key(path, name)
        os.environ.pop(name, None)
