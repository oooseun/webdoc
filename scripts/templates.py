#!/usr/bin/env python3
"""Manage webdoc theme templates (categories).

A template is a complete stylesheet that swaps webdoc's look. Built-in templates ship
in the skill's templates/ directory (currently: standard). Custom categories are
private — saved under ~/.config/webdoc/templates/<name>/style.css, never committed.

Save a deviation that worked out as a reusable category, then reuse it with
`create_site.py --template <name>`. Share a cohesive look with a team by copying the
category directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

try:
    from settings import templates_dir
except Exception:  # pragma: no cover - settings module should sit beside this file
    def templates_dir() -> Path:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base).expanduser() if base else Path("~/.config").expanduser()
        return root / "webdoc" / "templates"


SKILL_DIR = Path(__file__).resolve().parents[1]
BUILTIN_DIR = SKILL_DIR / "templates"
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def list_templates() -> dict[str, list[str]]:
    builtin = sorted(d.name for d in BUILTIN_DIR.glob("*") if (d / "style.css").is_file())
    user_dir = templates_dir()
    user = sorted(d.name for d in user_dir.glob("*") if (d / "style.css").is_file()) if user_dir.is_dir() else []
    return {"builtin": builtin, "user": user, "user_dir": str(user_dir)}


def resolve_source(src: Path) -> Path:
    src = src.expanduser()
    if src.is_dir() and (src / "style.css").is_file():
        return src / "style.css"
    if src.suffix == ".css" and src.is_file():
        return src
    raise SystemExit(f"source stylesheet not found: {src} (pass a .css file or a site dir containing style.css)")


def save_template(name: str, src: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise SystemExit("invalid template name (use letters, digits, dot, dash, underscore)")
    css = resolve_source(Path(src))
    dest_dir = templates_dir() / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(css, dest_dir / "style.css")
    return dest_dir / "style.css"


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage webdoc theme templates.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List built-in and saved templates")
    p_save = sub.add_parser("save", help="Save a stylesheet as a reusable category")
    p_save.add_argument("name", help="Category name (letters, digits, dot, dash, underscore)")
    p_save.add_argument("--from", dest="src", required=True, help="A .css file or a site dir containing style.css")
    args = parser.parse_args()

    if args.cmd == "list":
        print(json.dumps(list_templates(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "save":
        path = save_template(args.name, args.src)
        print(json.dumps({"saved": str(path), "use": f"--template {args.name}"}, indent=2, sort_keys=True))
        return 0
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
