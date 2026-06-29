#!/usr/bin/env python3
"""Server-side round-trip for webdoc editing mode.

One edit posted from the in-page editor becomes a surgical rewrite of the
canonical Markdown source. This module owns that round-trip so serve_site.py
stays a thin HTTP shell and the logic is unit-testable without a live server:

  * apply_edit(source_path, payload) -> (http_status, json_body)
      validate -> hash-check (409 on drift) -> html->md -> re-wrap by block
      type -> atomic write -> ledger append -> lint (advisory) -> re-render.
  * the override ledger (`<source_stem>.edits.json`) + check_overrides(), the
    anti-clobber contract a later agent pass must honour before rewriting a
    human-edited block.

Rendering primitives (block_hash, split_table_row, cell_marker, inline_md) are
reused from create_site so the client's emitted data-md-* attributes and the
server's checks/re-render never diverge. HTML sanitisation is delegated to the
html2md whitelist. Prose lint is advisory only and never blocks a save.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from create_site import block_hash, cell_marker, inline_md, split_table_row
from html2md import html_to_markdown

try:
    import lint_prose
except Exception:  # pragma: no cover - linter is advisory; absence must not break edits
    lint_prose = None  # type: ignore[assignment]


RANGE_TYPES = {"paragraph", "heading", "listitem"}
ALL_TYPES = RANGE_TYPES | {"tablecell"}
_STALE = {"error": "stale_block", "message": "this block changed on disk — reload"}

_HEADING_PREFIX = re.compile(r"^(\s*#{1,6}\s+)")
_LIST_PREFIX = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)")
# A paragraph whose new text would begin with one of these markers must be
# escaped on write (leading backslash) so a rebuild keeps it a paragraph rather
# than promoting it to a list/heading/quote. '>' needs no following space.
_LEADING_BLOCK = re.compile(r"^(?:#{1,6}\s|[-*+]\s|>|\d+[.)]\s)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Atomic writes
# --------------------------------------------------------------------------- #

def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (temp file in the same dir + rename).

    newline="" disables newline translation so the caller's exact line endings
    (LF or CRLF) are written verbatim - the source's style is preserved."""
    directory = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".webdoc-edit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_lines(path: Path, lines: list[str], trailing_newline: bool, newline: str = "\n") -> None:
    text = newline.join(lines)
    if trailing_newline:
        text += newline
    _atomic_write_text(path, text)


# --------------------------------------------------------------------------- #
# Block flattening + re-wrap
# --------------------------------------------------------------------------- #

def flatten(markdown: str) -> str:
    """Collapse converter output to a single source line.

    Paragraphs, headings, list items and table cells are each one logical unit
    rendered from one (paragraph: possibly several) source line(s) joined by
    spaces. v1 writes each edited block back as a single line, so a stray hard
    break can't split a list item or seed a blank line mid-paragraph."""
    return " ".join(seg.strip() for seg in markdown.split("\n") if seg.strip())


def rewrap_range(btype: str, original_slice: list[str], flat: str) -> list[str]:
    """Re-attach the structural prefix a block type carries, around new text."""
    first = original_slice[0] if original_slice else ""
    if btype == "heading":
        match = _HEADING_PREFIX.match(first)
        prefix = match.group(1) if match else "# "
        return [prefix + flat]
    if btype == "listitem":
        match = _LIST_PREFIX.match(first)
        prefix = match.group(1) if match else "- "
        return [prefix + flat]
    # paragraph: escape a leading block marker so a rebuild does not promote the
    # paragraph to a list/heading/quote. inline_md renders the bare marker.
    if _LEADING_BLOCK.match(flat):
        return ["\\" + flat]
    return [flat]


def _rejoin_row(original: str, fields: list[str]) -> str:
    """Rebuild a table row from cell fields, preserving outer-pipe style.

    Each cell's literal pipes are escaped (\\|) so the rebuilt row still splits
    into the same cells - both the cell being edited and untouched siblings
    (which split_table_row already unescaped on the way in)."""
    stripped = original.strip()
    lead = stripped.startswith("|")
    trail = stripped.endswith("|")
    inner = " | ".join(field.strip().replace("|", "\\|") for field in fields)
    return ("| " if lead else "") + inner + (" |" if trail else "")


# --------------------------------------------------------------------------- #
# Lint (advisory)
# --------------------------------------------------------------------------- #

def lint_block(text: str) -> list[dict[str, str]]:
    """Run the prose linter over one block's text; return findings, never raise.

    Findings are advisory in edit mode (a human override always wins), so every
    severity is surfaced inline and none blocks the save."""
    if lint_prose is None or not text.strip():
        return []
    try:
        rules = lint_prose.load_rules()
        alerts = lint_prose.lint(text, rules)
    except Exception:
        return []
    return [{"severity": sev, "rule": rule, "message": msg} for _line, sev, rule, msg in alerts]


# --------------------------------------------------------------------------- #
# Override ledger + anti-clobber contract
# --------------------------------------------------------------------------- #

def ledger_path(source_path: str | Path) -> Path:
    path = Path(source_path)
    return path.with_name(path.stem + ".edits.json")


def read_ledger(source_path: str | Path) -> list[dict]:
    try:
        data = json.loads(ledger_path(source_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _same_block(existing: dict, entry: dict) -> bool:
    if existing.get("type") != entry.get("type"):
        return False
    if entry.get("type") == "tablecell":
        return existing.get("line") == entry.get("line") and existing.get("cell") == entry.get("cell")
    return existing.get("start") == entry.get("start") and existing.get("end") == entry.get("end")


def append_ledger(source_path: str | Path, entry: dict) -> list[dict]:
    """Upsert an edit record (latest hash per block identity wins)."""
    ledger = [item for item in read_ledger(source_path) if not _same_block(item, entry)]
    ledger.append(entry)
    _atomic_write_text(ledger_path(source_path), json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    return ledger


def check_overrides(source_path: str | Path, block_markdown: str) -> bool:
    """True if `block_markdown` matches a human-edited block in the ledger.

    The anti-clobber guarantee: before an agent rewrites a block (e.g. a
    humanizer pass), it computes the block's current source-slice text, calls
    this, and must not silently overwrite when it returns True - flag or ask.
    Keyed on content hash, so it survives line drift in the rest of the doc."""
    target = block_hash(block_markdown)
    return any(entry.get("content_hash") == target for entry in read_ledger(source_path))


# --------------------------------------------------------------------------- #
# The edit round-trip
# --------------------------------------------------------------------------- #

def apply_edit(source_path: str | Path, payload: dict) -> tuple[int, dict]:
    """Apply one editing-mode edit to the canonical Markdown source.

    Returns (http_status, json_body). Never raises on bad input - validation
    failures map to 400/409. A 409 means the source drifted under the open tab
    (hash mismatch); nothing is written."""
    btype = str(payload.get("type", ""))
    if btype not in ALL_TYPES:
        return 400, {"error": "bad_type"}

    source_path = Path(source_path)
    try:
        # newline="" keeps CRLF/LF verbatim so we can detect and preserve the
        # source's line-ending style instead of silently rewriting it to LF.
        with source_path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        return 500, {"error": "source_unreadable", "detail": str(exc)}
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith("\n")
    lines = text.splitlines()

    flat = flatten(html_to_markdown(str(payload.get("html", ""))))

    if btype == "tablecell":
        return _apply_cell(source_path, lines, trailing_newline, payload, flat, newline)
    return _apply_range(source_path, lines, trailing_newline, payload, btype, flat, newline)


def _apply_range(
    source_path: Path,
    lines: list[str],
    trailing_newline: bool,
    payload: dict,
    btype: str,
    flat: str,
    newline: str = "\n",
) -> tuple[int, dict]:
    try:
        start = int(payload["start"])
        end = int(payload["end"])
    except (KeyError, TypeError, ValueError):
        return 400, {"error": "bad_range"}
    if start < 1 or end < start or end > len(lines):
        return 409, dict(_STALE)

    original_slice = lines[start - 1:end]
    if block_hash("\n".join(original_slice)) != str(payload.get("hash", "")):
        return 409, dict(_STALE)
    if not flat:
        return 400, {"error": "empty_block", "message": "a block cannot be emptied in edit mode"}

    new_block = rewrap_range(btype, original_slice, flat)
    new_lines = lines[:start - 1] + new_block + lines[end:]
    _write_lines(source_path, new_lines, trailing_newline, newline)

    new_start = start
    new_end = start + len(new_block) - 1
    new_hash = block_hash("\n".join(new_block))
    line_delta = len(new_block) - len(original_slice)
    new_html = inline_md(flat)

    append_ledger(source_path, {
        "type": btype,
        "start": new_start,
        "end": new_end,
        "content_hash": new_hash,
        "edited_at": now_iso(),
        "excerpt": flat[:120],
    })
    return 200, {
        "ok": True,
        "type": btype,
        "new_html": new_html,
        "new_hash": new_hash,
        "new_start": new_start,
        "new_end": new_end,
        "line_delta": line_delta,
        "lint": lint_block(flat),
    }


def _apply_cell(
    source_path: Path,
    lines: list[str],
    trailing_newline: bool,
    payload: dict,
    flat: str,
    newline: str = "\n",
) -> tuple[int, dict]:
    try:
        line = int(payload["line"])
        cell = int(payload["cell"])
    except (KeyError, TypeError, ValueError):
        return 400, {"error": "bad_cell"}
    if line < 1 or line > len(lines) or cell < 0:
        return 409, dict(_STALE)

    row = lines[line - 1]
    fields = split_table_row(row)
    current = fields[cell] if cell < len(fields) else ""
    if block_hash(current) != str(payload.get("hash", "")):
        return 409, dict(_STALE)

    # A cell carries a [+]/[-]/[~] marker as a structural prefix (it styles the
    # cell, not its text); preserve it around the new text.
    marker, _ = cell_marker(current)
    new_field = (marker + " " + flat).strip() if marker else flat
    while len(fields) <= cell:
        fields.append("")
    fields[cell] = new_field
    new_row = _rejoin_row(row, fields)
    new_lines = lines[:line - 1] + [new_row] + lines[line:]
    _write_lines(source_path, new_lines, trailing_newline, newline)

    # Recompute from the written row so the client's stored hash matches exactly
    # what a fresh create_site build would emit for this cell.
    written = split_table_row(new_row)
    canon = written[cell] if cell < len(written) else ""
    new_hash = block_hash(canon)
    _, cell_text = cell_marker(canon)
    new_html = inline_md(cell_text)

    append_ledger(source_path, {
        "type": "tablecell",
        "line": line,
        "cell": cell,
        "content_hash": new_hash,
        "edited_at": now_iso(),
        "excerpt": cell_text[:120],
    })
    return 200, {
        "ok": True,
        "type": "tablecell",
        "new_html": new_html,
        "new_hash": new_hash,
        "line": line,
        "cell": cell,
        "line_delta": 0,
        "lint": lint_block(cell_text),
    }
