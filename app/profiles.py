"""Model profile management — provider switch via per-process env vars.

Profiles are stored as settings_<name>.json in the openclaw application
config directory (<openclaw_root>/config/). The "active" profile is
recorded in <openclaw_root>/config/active_profile (a single line
containing the profile name) and its env vars (ANTHROPIC_BASE_URL,
ANTHROPIC_AUTH_TOKEN, model vars) are injected into each openclaw-spawned
claude subprocess via _build_env. We never mutate ~/.claude/settings.json
or any workspace's .claude/, so concurrent claude processes (other openclaw
users, IDE plugins, manual ``claude`` runs) are not affected by a profile
switch.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

logger = logging.getLogger("openclaw.profiles")

OPENCLAW_ROOT = Path(__file__).resolve().parent.parent  # app/profiles.py → app/ → root
CONFIG_DIR = OPENCLAW_ROOT / "config"
ACTIVE_PROFILE_FILE = CONFIG_DIR / "active_profile"

# Profile display labels for card UI
PROFILE_LABELS: dict[str, str] = {
    "glm": "GLM (智谱)",
    "kimi": "Kimi (月之暗面)",
}


def discover_profiles() -> dict[str, dict]:
    """Discover all settings_*.json profiles in ~/.claude/.

    Returns:
        Dict mapping profile name to profile info:
        {name: {name, label, base_url, model}}
    """
    profiles: dict[str, dict] = {}
    for f in sorted(CONFIG_DIR.glob("settings_*.json")):
        try:
            name = f.stem.replace("settings_", "")
            data = json.loads(f.read_text("utf-8"))
            base_url = data.get("env", {}).get("ANTHROPIC_BASE_URL", "")
            model = data.get("model", "")
            label = PROFILE_LABELS.get(name, name)
            profiles[name] = {
                "name": name,
                "label": label,
                "base_url": base_url,
                "model": model,
            }
        except Exception as e:
            logger.warning("Failed to read profile %s: %s", f, e)
    return profiles


def get_active_profile() -> str:
    """Return the currently active profile name.

    Reads ~/.openclaw_active_profile. Falls back to "unknown" if missing
    or stale. This used to compare ANTHROPIC_AUTH_TOKEN against every
    profile; that broke under concurrent users, so we now track the
    active name explicitly.
    """
    if not ACTIVE_PROFILE_FILE.exists():
        return "unknown"
    try:
        name = ACTIVE_PROFILE_FILE.read_text("utf-8").strip()
        if not name:
            return "unknown"
        return name
    except Exception:
        return "unknown"


def switch_profile(name: str) -> bool:
    """Activate a named profile by recording its name in the marker file.

    No file copying — the actual env injection happens in
    load_active_profile_env() when claude is spawned. This keeps the
    switch side-effect-free for everything outside openclaw.

    Args:
        name: Profile name (e.g. "glm", "kimi"). Must match a
            settings_<name>.json file in ~/.claude/.

    Returns:
        True if the profile exists and was activated, False otherwise.
    """
    source = CONFIG_DIR / f"settings_{name}.json"
    if not source.exists():
        logger.error("Profile not found: %s (looked for %s)", name, source)
        return False
    try:
        ACTIVE_PROFILE_FILE.write_text(name, "utf-8")
        logger.info("Switched profile to %s", name)
        return True
    except Exception as e:
        logger.error("Failed to switch profile to %s: %s", name, e)
        return False


def load_active_profile_env() -> dict[str, str]:
    """Return env vars for the active profile, for claude subprocess injection.

    Reads ~/.claude/settings_<active>.json and returns its "env" dict.
    Empty dict if no active profile is set or the file is unreadable —
    in that case claude falls back to its global settings.json.
    """
    name = get_active_profile()
    if name == "unknown":
        return {}
    source = CONFIG_DIR / f"settings_{name}.json"
    if not source.exists():
        return {}
    try:
        data = json.loads(source.read_text("utf-8"))
        env = data.get("env", {})
        # Only forward string-typed env entries; anything else would
        # break subprocess env construction.
        return {k: str(v) for k, v in env.items() if isinstance(v, (str, int))}
    except Exception as e:
        logger.warning("Failed to load profile env %s: %s", name, e)
        return {}


def test_profile() -> tuple[bool, str]:
    """Test the active profile by sending a minimal API request.

    Uses the env vars that would actually be injected at spawn time,
    so this validates exactly what claude will see — not whatever happens
    to be in ~/.claude/settings.json right now.
    """
    name = get_active_profile()
    if name == "unknown":
        return False, "未设置 active profile（用 /switch 选择一个）"

    env = load_active_profile_env()
    base_url = env.get("ANTHROPIC_BASE_URL", "")
    api_key = env.get("ANTHROPIC_AUTH_TOKEN", "")
    model = env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")

    if not base_url or not api_key:
        return False, f"profile `{name}` 缺少 BASE_URL 或 API_KEY"

    url = f"{base_url}/messages"
    payload = {
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Say OK"}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            return True, f"HTTP 200 ({name})"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.ConnectError:
        return False, f"无法连接 {base_url}"
    except httpx.TimeoutException:
        return False, "请求超时 (30s)"
    except Exception as e:
        return False, str(e)[:200]
