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

from create_site import (
    block_hash, cell_marker, inline_md, is_table_separator, rewrite_svg_text,
    split_table_row, svg_text_at,
)
from html2md import html_to_markdown

try:
    import lint_prose
except Exception:  # pragma: no cover - linter is advisory; absence must not break edits
    lint_prose = None  # type: ignore[assignment]


RANGE_TYPES = {"paragraph", "heading", "listitem"}
ALL_TYPES = RANGE_TYPES | {"tablecell"}
# Spacing is paragraph/heading only: list items have no blank line between them,
# so inserting one would split the list (and renumber an ordered one) instead of
# adding a gap. Delete still works on list items (it removes the whole line).
_SPACE_TYPES = {"paragraph", "heading"}
# Retype (the list toolbar button) toggles a block between these two types.
_RETYPE_TYPES = {"paragraph", "listitem"}
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
    # A lines list ending in "" (a trailing blank line) is only representable with
    # a final newline: ["A", ""] must serialize as "A\n\n", because "A\n" re-reads
    # as ["A"] - silently losing the blank line and tripping undo's bounds check.
    # This is the no-trailing-newline sibling of the empty-doc case below: deleting
    # the last block of a file with no final newline leaves a trailing blank, and
    # without this coercion the block would be dropped and its undo would 409.
    if lines and lines[-1] == "":
        trailing_newline = True
    text = newline.join(lines)
    # An empty block list is an empty document (zero bytes), never a lone blank
    # line: writing "\n" here would make deleting the only block irreversible
    # (undo would then read [""] and restore one line too many).
    if trailing_newline and lines:
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


def _drop_ledger_in_range(source_path: str | Path, start: int, end: int) -> None:
    """Drop ledger entries for range blocks fully inside [start, end].

    Called on delete: a removed block's override record is now meaningless. Entry
    line numbers are advisory (check_overrides keys on content_hash), so we only
    prune what the delete clearly removed and leave the rest untouched."""
    ledger = read_ledger(source_path)
    kept = [
        e for e in ledger
        if not (
            e.get("type") in RANGE_TYPES
            and isinstance(e.get("start"), int)
            and isinstance(e.get("end"), int)
            and start <= e["start"] and e["end"] <= end
        )
    ]
    if len(kept) != len(ledger):
        _atomic_write_text(ledger_path(source_path), json.dumps(kept, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# Session undo stack
# --------------------------------------------------------------------------- #
#
# Editing mode supports undo (Cmd/Ctrl+Z and the banner button). Every mutating
# op (edit, delete, cell edit) pushes an inverse *source splice* onto a per-file
# LIFO stack; apply_undo pops the newest and replays it against the file. Because
# undo is strictly LIFO, the splice indices recorded at push time are still valid
# when popped: reversing op N restores the file to its state just before N, which
# is exactly when op N-1 was recorded. The stack lives in this process only - a
# within-session affordance; a server restart simply empties it, and the client
# reconciles (a 400 nothing_to_undo / 409 desync clears its parallel stack).

_UNDO: dict[str, list[dict]] = {}
_UNDO_LIMIT = 200  # bound memory; the oldest entries fall off the bottom


def _undo_key(source_path: str | Path) -> str:
    return str(Path(source_path))


def _push_undo(source_path: str | Path, entry: dict) -> None:
    stack = _UNDO.setdefault(_undo_key(source_path), [])
    stack.append(entry)
    if len(stack) > _UNDO_LIMIT:
        del stack[0]


def undo_depth(source_path: str | Path) -> int:
    return len(_UNDO.get(_undo_key(source_path), []))


def clear_undo(source_path: str | Path) -> None:
    _UNDO.pop(_undo_key(source_path), None)


# --------------------------------------------------------------------------- #
# The edit round-trip
# --------------------------------------------------------------------------- #

def apply_edit(source_path: str | Path, payload: dict) -> tuple[int, dict]:
    """Apply one editing-mode edit to the canonical Markdown source.

    Returns (http_status, json_body). Never raises on bad input - validation
    failures map to 400/409. A 409 means the source drifted under the open tab
    (hash mismatch); nothing is written.

    `op` selects the operation: the default "edit" replaces a block's text;
    "delete" removes a whole range block (paragraph/heading/listitem)."""
    op = str(payload.get("op", "edit"))
    btype = str(payload.get("type", ""))
    if op in ("svgtext", "rowdelete", "coldelete"):
        pass  # sub-element ops (SVG label / table row / table column); no block type
    elif op == "delete":
        # Delete is range-only: a table cell cannot be removed (breaks the row).
        if btype not in RANGE_TYPES:
            return 400, {"error": "bad_type"}
    elif op == "space":
        # Spacing is paragraph/heading only (a blank would split a list).
        if btype not in _SPACE_TYPES:
            return 400, {"error": "bad_type"}
    elif op == "retype":
        # The list button toggles paragraph <-> list item; both types required,
        # and the target must differ from the current type.
        target_type = str(payload.get("target", ""))
        if btype not in _RETYPE_TYPES or target_type not in _RETYPE_TYPES or target_type == btype:
            return 400, {"error": "bad_retype"}
    elif btype not in ALL_TYPES:
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

    if op == "delete":
        return _apply_delete(source_path, lines, trailing_newline, payload, btype, newline)
    if op == "space":
        return _apply_space(source_path, lines, trailing_newline, payload, btype, newline)
    if op == "svgtext":
        return _apply_svgtext(source_path, lines, trailing_newline, payload, newline)
    if op == "rowdelete":
        return _apply_rowdelete(source_path, lines, trailing_newline, payload, newline)
    if op == "coldelete":
        return _apply_coldelete(source_path, lines, trailing_newline, payload, newline)

    flat = flatten(html_to_markdown(str(payload.get("html", ""))))

    if op == "retype":
        # paragraph -> list item is a clean prefix add (one line). list item ->
        # paragraph blank-separates the pulled-out paragraph from adjacent list
        # markers so the source is CommonMark-portable, not just webdoc-parseable.
        # Undo is labelled "retype" so the client reverses the element swap.
        target_type = str(payload.get("target", ""))
        if target_type == "paragraph":
            return _apply_delist(source_path, lines, trailing_newline, payload, flat, newline)
        return _apply_range(source_path, lines, trailing_newline, payload,
                            target_type, flat, newline, undo_label="retype")

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
    undo_label: str = "edit",
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
        return 400, {"error": "empty_block",
                     "message": "To remove a block, use the Delete button (a block can't be emptied)."}

    new_block = rewrap_range(btype, original_slice, flat)
    new_lines = lines[:start - 1] + new_block + lines[end:]
    _write_lines(source_path, new_lines, trailing_newline, newline)

    # Inverse for undo: at the block's start, drop the lines we just wrote and
    # put the original slice back. Valid because undo is LIFO (see _UNDO).
    # `expect` is the content undo will remove (the lines we wrote): undo verifies
    # the file still holds exactly this before splicing, so an out-of-band edit
    # cannot make a stale-but-in-bounds splice clobber the wrong lines. `trailing`
    # restores the file's exact trailing-newline state. `undo_label` is "retype"
    # for a list<->paragraph conversion so the client reverses the DOM element swap.
    _push_undo(source_path, {
        "label": undo_label,
        "at": start - 1,
        "remove": len(new_block),
        "insert": original_slice,
        "expect": new_block,
        "trailing": trailing_newline,
        "newline": newline,
    })

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

    # Inverse for undo: restore the original row (carries line + cell so undo can
    # recompute this cell's hash/html, not the whole row's).
    _push_undo(source_path, {
        "label": "cell",
        "at": line - 1,
        "remove": 1,
        "insert": [row],
        "expect": [new_row],
        "trailing": trailing_newline,
        "newline": newline,
        "line": line,
        "cell": cell,
    })

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


def _apply_rowdelete(
    source_path: Path,
    lines: list[str],
    trailing_newline: bool,
    payload: dict,
    newline: str = "\n",
) -> tuple[int, dict]:
    """Delete one data row from a table. The client sends the active cell's
    line+cell+hash as a drift check (it works from the DOM, not the raw row line).
    The header and separator rows are never deletable."""
    try:
        line = int(payload["line"])
        cell = int(payload["cell"])
    except (KeyError, TypeError, ValueError):
        return 400, {"error": "bad_cell"}
    if line < 1 or line > len(lines) or cell < 0:
        return 409, dict(_STALE)
    row = lines[line - 1]
    if "|" not in row or is_table_separator(row):
        return 400, {"error": "bad_row", "message": "only a table data row can be deleted"}
    # A header row is a table's FIRST row (the line above is not a table row)
    # immediately followed by the `| --- |` separator. The "first row" test avoids
    # mistaking a data row that happens to sit above an all-dashes divider row for
    # the header. Deleting a real header would leave a table with no header.
    prev_is_table = line >= 2 and "|" in lines[line - 2]
    if not prev_is_table and line < len(lines) and is_table_separator(lines[line]):
        return 400, {"error": "bad_row", "message": "the header row can't be deleted"}
    fields = split_table_row(row)
    current = fields[cell] if cell < len(fields) else ""
    if block_hash(current) != str(payload.get("hash", "")):
        return 409, dict(_STALE)

    new_lines = lines[:line - 1] + lines[line:]
    _write_lines(source_path, new_lines, trailing_newline, newline)
    _push_undo(source_path, {
        "label": "rowdelete", "at": line - 1, "remove": 0, "insert": [row],
        "expect": [], "anchor": lines[line] if line < len(lines) else None,
        "trailing": trailing_newline, "newline": newline,
    })
    return 200, {"ok": True, "op": "rowdelete", "line": line, "line_delta": -1, "shift_threshold": line}


def _apply_coldelete(
    source_path: Path,
    lines: list[str],
    trailing_newline: bool,
    payload: dict,
    newline: str = "\n",
) -> tuple[int, dict]:
    """Delete one column from every row of a table (header, separator, data). The
    client sends the table's line range [start,end], the column index, and the
    active cell's line+cell+hash as a drift check. Line count is unchanged."""
    try:
        start = int(payload["start"])
        end = int(payload["end"])
        col = int(payload["col"])
        chkline = int(payload["line"])
        chkcell = int(payload["cell"])
    except (KeyError, TypeError, ValueError):
        return 400, {"error": "bad_range"}
    if start < 1 or end < start or end > len(lines) or col < 0 or chkline < 1 or chkline > len(lines):
        return 409, dict(_STALE)
    chk = split_table_row(lines[chkline - 1])
    chkcur = chk[chkcell] if 0 <= chkcell < len(chk) else ""
    if block_hash(chkcur) != str(payload.get("hash", "")):
        return 409, dict(_STALE)

    new_slice: list[str] = []
    removed = False
    for row in lines[start - 1:end]:
        if "|" in row:
            fields = split_table_row(row)
            # Every table row must carry the column (else the source drifted / is
            # ragged) and must keep at least one column after the delete.
            if col >= len(fields):
                return 409, dict(_STALE)
            if len(fields) <= 1:
                return 400, {"error": "last_column", "message": "a table needs at least one column"}
            fields.pop(col)
            removed = True
            new_slice.append(_rejoin_row(row, fields))
        else:
            new_slice.append(row)
    if not removed:
        return 409, dict(_STALE)

    new_lines = lines[:start - 1] + new_slice + lines[end:]
    _write_lines(source_path, new_lines, trailing_newline, newline)
    _push_undo(source_path, {
        "label": "coldelete", "at": start - 1, "remove": len(new_slice),
        "insert": lines[start - 1:end], "expect": new_slice,
        "trailing": trailing_newline, "newline": newline,
    })
    return 200, {"ok": True, "op": "coldelete", "start": start, "end": end, "col": col, "line_delta": 0}


def _apply_delete(
    source_path: Path,
    lines: list[str],
    trailing_newline: bool,
    payload: dict,
    btype: str,
    newline: str = "\n",
) -> tuple[int, dict]:
    """Remove a whole range block (paragraph/heading/listitem) from the source.

    Hash-checked like an edit (409 on drift); the removed slice is pushed onto
    the undo stack so the block can be restored verbatim. Returns the negative
    line_delta so the client can shift the ranges of every following block."""
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

    new_lines = lines[:start - 1] + lines[end:]
    _write_lines(source_path, new_lines, trailing_newline, newline)

    # Inverse for undo: re-insert the removed slice at the same position. remove=0
    # so there is no removed content to guard; instead `anchor` records the line
    # that will sit at the splice point after deletion (None if the block was at
    # end-of-file), so undo can confirm the position hasn't shifted out from under
    # it before re-inserting. `trailing`/`newline` restore the file's exact line-
    # ending state (matters when the only block is deleted -> empty file).
    #
    # Ledger note: _drop_ledger_in_range removes this block's override record and
    # undo does not restore it. The ledger is content-hash keyed and advisory
    # (check_overrides), so a delete-then-undo'd block that had a prior human edit
    # loses its anti-clobber record (the client still restores the visible badge).
    # Accepted residual, same class as the stale entry an edit-undo leaves behind.
    _push_undo(source_path, {
        "label": "delete",
        "at": start - 1,
        "remove": 0,
        "insert": original_slice,
        "expect": [],
        "anchor": lines[end] if end < len(lines) else None,
        "trailing": trailing_newline,
        "newline": newline,
    })
    _drop_ledger_in_range(source_path, start, end)

    return 200, {
        "ok": True,
        "op": "delete",
        "type": btype,
        "start": start,
        "end": end,
        "line_delta": -(end - start + 1),
    }


def _apply_space(
    source_path: Path,
    lines: list[str],
    trailing_newline: bool,
    payload: dict,
    btype: str,
    newline: str = "\n",
) -> tuple[int, dict]:
    """Add or remove one blank line immediately before a block, for vertical spacing.

    The block's content is untouched - only the gap above it changes (extra blank
    lines render as gaps; see create_site). Hash-checked on the block so we space
    the right one and catch drift. Undoable. `shift_threshold` is start-1 so the
    spaced block itself (and everything after) re-shifts by line_delta on the
    client, which also manages the gap element."""
    try:
        start = int(payload["start"])
        end = int(payload["end"])
    except (KeyError, TypeError, ValueError):
        return 400, {"error": "bad_range"}
    if start < 1 or end < start or end > len(lines):
        return 409, dict(_STALE)
    original_slice = lines[start - 1:end]
    digest = block_hash("\n".join(original_slice))
    if digest != str(payload.get("hash", "")):
        return 409, dict(_STALE)

    direction = str(payload.get("dir", "add"))

    if direction == "add":
        new_lines = lines[:start - 1] + [""] + lines[start - 1:]
        _write_lines(source_path, new_lines, trailing_newline, newline)
        _push_undo(source_path, {
            "label": "space", "at": start - 1, "remove": 1, "insert": [],
            "expect": [""], "trailing": trailing_newline, "newline": newline,
        })
        return 200, {
            "ok": True, "op": "space", "type": btype, "dir": "add",
            "new_start": start + 1, "new_end": end + 1, "new_hash": digest,
            "line_delta": 1, "shift_threshold": start - 1,
        }

    if direction == "remove":
        # Count the consecutive blank lines immediately before the block. Keep one
        # as the block separator (none required if only blanks precede the block,
        # i.e. it is the first block); refuse if there is no extra gap to remove.
        gap = 0
        j = start - 2
        while j >= 0 and lines[j].strip() == "":
            gap += 1
            j -= 1
        is_first = (start - 1 - gap) <= 0
        if gap - (0 if is_first else 1) < 1:
            return 400, {"error": "no_space", "message": "no extra spacing to remove"}
        new_lines = lines[:start - 2] + lines[start - 1:]
        _write_lines(source_path, new_lines, trailing_newline, newline)
        _push_undo(source_path, {
            "label": "space", "at": start - 2, "remove": 0, "insert": [""],
            "expect": [], "anchor": original_slice[0] if original_slice else None,
            "trailing": trailing_newline, "newline": newline,
        })
        return 200, {
            "ok": True, "op": "space", "type": btype, "dir": "remove",
            "new_start": start - 1, "new_end": end - 1, "new_hash": digest,
            "line_delta": -1, "shift_threshold": start - 1,
        }

    return 400, {"error": "bad_dir"}


def _apply_delist(
    source_path: Path,
    lines: list[str],
    trailing_newline: bool,
    payload: dict,
    flat: str,
    newline: str = "\n",
) -> tuple[int, dict]:
    """Convert a list item to a paragraph, blank-separating it from any adjacent
    list markers.

    Writing just the bare text ("- a\\ntext\\n- c") renders correctly in webdoc's
    own line-based parser but is a *lazy continuation* in CommonMark (GitHub, VS
    Code, pandoc would merge it into the item above). Inserting a blank line on
    each side that touches a list marker keeps the split portable, and a single
    blank renders no gap, so webdoc's own output is unchanged."""
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
        return 400, {"error": "empty_block",
                     "message": "To remove a block, use the Delete button (a block can't be emptied)."}

    para = ("\\" + flat) if _LEADING_BLOCK.match(flat) else flat
    need_before = start > 1 and bool(_LIST_PREFIX.match(lines[start - 2]))
    need_after = end < len(lines) and bool(_LIST_PREFIX.match(lines[end]))
    new_block = ([""] if need_before else []) + [para] + ([""] if need_after else [])
    new_lines = lines[:start - 1] + new_block + lines[end:]
    _write_lines(source_path, new_lines, trailing_newline, newline)

    _push_undo(source_path, {
        "label": "retype", "at": start - 1, "remove": len(new_block),
        "insert": original_slice, "expect": new_block,
        "trailing": trailing_newline, "newline": newline,
    })

    new_start = start + (1 if need_before else 0)
    new_hash = block_hash(para)
    append_ledger(source_path, {
        "type": "paragraph", "start": new_start, "end": new_start,
        "content_hash": new_hash, "edited_at": now_iso(), "excerpt": flat[:120],
    })
    return 200, {
        "ok": True, "type": "paragraph", "new_html": inline_md(flat),
        "new_hash": new_hash, "new_start": new_start, "new_end": new_start,
        "line_delta": len(new_block) - len(original_slice), "lint": lint_block(flat),
    }


def _apply_svgtext(
    source_path: Path,
    lines: list[str],
    trailing_newline: bool,
    payload: dict,
    newline: str = "\n",
) -> tuple[int, dict]:
    """Rewrite one simple <text> label inside an embedded SVG.

    `loc` is "embed_start:embed_end:idx" - the embed content's 1-based inclusive
    source range plus the label's index among simple <text> elements (the same
    order create_site stamps them in). Hash-checked against the label's current
    source content (409 on drift). Undoable; the label's text is collapsed to one
    line and XML-escaped on write, so the SVG stays valid."""
    parts = str(payload.get("loc", "")).split(":")
    try:
        embed_start, embed_end, idx = int(parts[0]), int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return 400, {"error": "bad_loc"}
    if embed_start < 1 or embed_end < embed_start or embed_end > len(lines):
        return 409, dict(_STALE)

    embed_lines = lines[embed_start - 1:embed_end]
    block = "\n".join(embed_lines)
    current = svg_text_at(block, idx)
    if current is None:
        return 409, dict(_STALE)
    if block_hash(current) != str(payload.get("hash", "")):
        return 409, dict(_STALE)

    # Collapse to one line and drop C0/DEL control chars (NUL et al. are invalid
    # in XML and would otherwise reach the SVG source).
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(payload.get("text", "")))
    new_text = " ".join(raw.split())
    if not new_text:
        return 400, {"error": "empty_label", "message": "a diagram label can't be empty"}

    new_block = rewrite_svg_text(block, idx, new_text)
    if new_block is None:
        return 409, dict(_STALE)
    new_embed_lines = new_block.split("\n")
    new_lines = lines[:embed_start - 1] + new_embed_lines + lines[embed_end:]
    _write_lines(source_path, new_lines, trailing_newline, newline)

    _push_undo(source_path, {
        "label": "svgtext",
        "at": embed_start - 1,
        "remove": len(new_embed_lines),
        "insert": embed_lines,
        "expect": new_embed_lines,
        "trailing": trailing_newline, "newline": newline,
    })

    new_embed_end = embed_start - 1 + len(new_embed_lines)
    rendered = svg_text_at(new_block, idx)
    return 200, {
        "ok": True, "op": "svgtext",
        "new_loc": f"{embed_start}:{new_embed_end}:{idx}",
        "new_hash": block_hash(rendered if rendered is not None else new_text),
        "new_text": new_text,
        "line_delta": len(new_embed_lines) - len(embed_lines),
    }


def apply_undo(source_path: str | Path) -> tuple[int, dict]:
    """Reverse the most recent mutating edit on this source (LIFO).

    Pops the newest inverse splice and replays it against the file. Returns the
    affected block's fresh identity (range + hash, or cell hash) and line_delta
    so the client can patch the DOM and re-shift following blocks. A 400 means
    nothing is left to undo; a 409 means the source drifted out from under the
    stack (the stack is then dropped, never replayed against a changed file)."""
    key = _undo_key(source_path)
    stack = _UNDO.get(key)
    if not stack:
        return 400, {"error": "nothing_to_undo", "message": "nothing to undo"}

    entry = stack[-1]  # peek; only pop after the write succeeds
    source_path = Path(source_path)
    try:
        with source_path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        return 500, {"error": "source_unreadable", "detail": str(exc)}
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith("\n")
    lines = text.splitlines()

    at = int(entry["at"])
    remove = int(entry["remove"])
    insert = list(entry["insert"])
    expect = entry.get("expect")
    # Reject a stale splice. The bounds check catches a file that shrank; the
    # content check (the lines undo is about to remove must still be exactly what
    # this op wrote) catches a same-length out-of-band edit that the bounds check
    # would miss - parity with the 409 hash-check that guards edit/delete. For an
    # insert-only (delete) undo there is no removed content, so `anchor` guards the
    # position instead: the line that should sit at the splice point must still be
    # there (or the file must still end there), else an out-of-band line shift
    # would re-insert the block in the wrong place. Either way the whole stack is
    # dropped, never replayed against a changed file.
    desynced = at < 0 or remove < 0 or at + remove > len(lines)
    if not desynced and expect is not None and lines[at:at + remove] != list(expect):
        desynced = True
    if not desynced and "anchor" in entry:
        anchor = entry["anchor"]
        if anchor is None:
            desynced = at != len(lines)  # block was at EOF; undo must append there
        else:
            desynced = at >= len(lines) or lines[at] != anchor
    if desynced:
        clear_undo(source_path)
        return 409, {"error": "undo_desynced", "message": "source changed — reload to edit"}

    # Restore the file's exact line-ending state. `newline`/`trailing` are recorded
    # per op because apply_undo re-reads the (possibly emptied) file, where the
    # style can no longer be re-detected - e.g. undoing the delete of the only
    # block: the file is "" so a fresh detect would default to LF and drop CRLF.
    target_trailing = bool(entry.get("trailing", trailing_newline))
    target_newline = entry.get("newline", newline)
    new_lines = lines[:at] + insert + lines[at + remove:]
    _write_lines(source_path, new_lines, target_trailing, target_newline)
    stack.pop()
    if not stack:
        clear_undo(source_path)

    label = entry.get("label", "edit")
    body = {
        "ok": True,
        "label": label,
        "line_delta": len(insert) - remove,
        # Following blocks (start past here) shifted by line_delta; the affected
        # block itself is patched by the client from new_start/new_end below.
        "shift_threshold": at + remove,
        "remaining": undo_depth(source_path),
    }
    if label == "cell":
        line = int(entry.get("line", at + 1))
        cell = int(entry.get("cell", 0))
        written = split_table_row(insert[0]) if insert else []
        canon = written[cell] if cell < len(written) else ""
        _, cell_text = cell_marker(canon)
        body.update({
            "line": line,
            "cell": cell,
            "new_hash": block_hash(canon),
            "new_html": inline_md(cell_text),
        })
    else:
        body.update({
            "new_start": at + 1,
            "new_end": at + len(insert),
            "new_hash": block_hash("\n".join(insert)),
        })
    return 200, body
