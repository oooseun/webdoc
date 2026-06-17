#!/usr/bin/env python3
"""Shared webdoc settings, read from ~/.config/webdoc/settings.json.

The config file is user-owned and lives outside the skill repo, so it is never
committed or published. Missing file / bad JSON falls back to defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULTS: dict[str, object] = {
    # Auto-open the finished site in the browser when a server starts.
    "auto_open": True,
}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path("~/.config").expanduser()
    return root / "webdoc"


def templates_dir() -> Path:
    return config_dir() / "templates"


def config_path() -> Path:
    return config_dir() / "settings.json"


def read_settings() -> dict[str, object]:
    """Return defaults overlaid with any values from the user config file."""
    settings = dict(DEFAULTS)
    path = config_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return settings
    if isinstance(loaded, dict):
        settings.update(loaded)
    return settings


if __name__ == "__main__":
    print(json.dumps({"config_path": str(config_path()), "settings": read_settings()}, indent=2, sort_keys=True))
