#!/usr/bin/env python3
"""Create a unified local website from a Markdown report.

Prose and interactive components live in one source. Fenced blocks extend Markdown:
  ```stepper title="..."   -> a click-to-advance state machine (no looping)
  ```embed / ```component   -> raw HTML/JS injected verbatim (toggles, audio, galleries)
Table cells may lead with [+]/[-]/[~] for green/red/amber conditional formatting.

Always emits two files: index.html (the interactive site) and doc.html (a single
self-contained file for the Google Drive -> Open with Google Docs import path).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(os.environ.get("AGENT_ARTIFACT_SITES", "~/agent-artifacts/sites")).expanduser()

try:
    from settings import templates_dir
except Exception:  # pragma: no cover - settings module should sit beside this file
    def templates_dir() -> Path:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base).expanduser() if base else Path("~/.config").expanduser()
        return root / "webdoc" / "templates"


def resolve_template(value: str) -> Path:
    """Resolve a theme template to a complete stylesheet.

    Accepts a path to a .css file, a directory containing style.css, a saved user
    category under ~/.config/webdoc/templates/<name>/, or a built-in template name.
    """
    candidate = Path(value).expanduser()
    if candidate.suffix == ".css" and candidate.is_file():
        return candidate
    if candidate.is_dir() and (candidate / "style.css").is_file():
        return candidate / "style.css"
    user = templates_dir() / value / "style.css"
    if user.is_file():
        return user
    builtin = SKILL_DIR / "templates" / value / "style.css"
    if builtin.is_file():
        return builtin
    raise SystemExit(f"template not found: {value!r} (looked for a .css file, {user}, and {builtin})")


# Conditional-format markers usable as the first token of a table cell.
CELL_CLASS = {"[+]": "cell-good", "[-]": "cell-bad", "[~]": "cell-warn"}
CELL_DOC_STYLE = {
    "[+]": "background:#e6f4ea;color:#14532d",
    "[-]": "background:#fde8e8;color:#7f1d1d",
    "[~]": "background:#fef3e2;color:#7c4a03",
}
IMG_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}
IMG_TAG_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"[^>]*?>', re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, fallback: str = "webdoc") -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or fallback


def source_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def block_hash(text: str) -> str:
    """Short stable hash of an editable block's exact source text.

    Emitted into the page as data-md-hash and re-checked server-side on save so
    a block edited under the open tab is caught (409) instead of clobbered. The
    client and server must compute it over the same string: for a line-range
    block that is "\\n".join(lines[start-1:end]); for a table cell it is the raw
    (split_table_row-stripped) cell field."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def stable_artifact_id(path: Path, snapshot: bool = False) -> str:
    resolved = path.resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    base = f"{slugify(path.stem)}-{digest}"
    if snapshot:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{base}-{stamp}"
    return base


_CTRL_RUN = re.compile(r"[\x00-\x20]+")


def safe_href(href: str) -> str:
    href = href.strip()
    # Browsers ignore interior/leading control + whitespace chars in a scheme,
    # so "java\tscript:" or "\x01javascript:" would execute despite a naive
    # prefix check. Strip all of [\x00-\x20] before testing the scheme.
    probe = _CTRL_RUN.sub("", href).lower()
    if probe.startswith(("javascript:", "data:", "vbscript:")):
        return "#"
    return href


def _anchor(href: str, label: str) -> str:
    safe = safe_href(html.unescape(href))
    href_attr = html.escape(safe, quote=True)
    external = safe.lower().startswith(("http://", "https://"))
    extra = ' target="_blank" rel="noopener noreferrer"' if external else ""
    return f'<a href="{href_attr}"{extra}>{label}</a>'


def _image(src: str, alt: str) -> str:
    safe = safe_href(html.unescape(src))
    href_attr = html.escape(safe, quote=True)
    return f'<img src="{href_attr}" alt="{alt}" loading="lazy">'


def _code_span(stash, match: "re.Match") -> str:
    fence, content = match.group(1), match.group(2)
    # CommonMark strips one leading + trailing space when a multi-backtick fence
    # was padded to hold backtick-bearing content; single-backtick spans keep
    # their content verbatim (webdoc's long-standing behaviour).
    if len(fence) > 1 and len(content) >= 2 and content[0] == " " and content[-1] == " " and content.strip(" "):
        content = content[1:-1]
    return stash(f"<code>{content}</code>")


# A leading block marker (-, *, +, #, >, or "1." / "1)") that the edit
# round-trip backslash-escaped on write, so an edited paragraph that now begins
# with one stays a paragraph. We strip the backslash and render the bare marker;
# '>' needs no following space (parse_markdown treats ">x" as a quote too).
_LEADING_MARKER_ESCAPE = re.compile(r"^\\(>|#{1,6}(?=\s)|[-*+](?=\s)|\d+[.)](?=\s))")


def inline_md(text: str) -> str:
    # Pull off a leading escaped block marker before anything else, and render
    # it literally at the very end (only at the leading position - a mid-text
    # "\-" is left to the general backslash rule). This keeps a paragraph that
    # starts with "- ", "# ", "> ", or "1. " a paragraph, with no backslash shown.
    leading_literal = ""
    lead = _LEADING_MARKER_ESCAPE.match(text)
    if lead:
        leading_literal = html.escape(lead.group(1))
        text = text[lead.end():]

    escaped = html.escape(text)

    # Protect generated tags via placeholders so later passes (e.g. bare-URL
    # autolinking) never re-process URLs already inside an <a>/<code>/<img> element.
    placeholders: list[str] = []

    def stash(htmlfrag: str) -> str:
        placeholders.append(htmlfrag)
        return f"\x00{len(placeholders) - 1}\x00"

    def emph(s: str) -> str:
        # Bold / italic / strikethrough. Applied to the whole line and, separately,
        # to a link's label so marks inside link text render. Any already-stashed
        # code span / escaped char in `s` is an inert \x00N\x00 marker here.
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"~~([^~\n]+)~~", r"<del>\1</del>", s)
        return s

    # Code spans: an opening backtick run not escaped by a preceding backslash,
    # closed by a run of the same length. Widening the fence lets backtick-
    # bearing content round-trip; the (?<!\\) guard means a typed-then-escaped
    # backtick (\`) is left for the backslash rule below, not eaten as a span.
    escaped = re.sub(
        r"(?<!\\)(`+)(.+?)\1(?!`)",
        lambda m: _code_span(stash, m),
        escaped,
        flags=re.DOTALL,
    )
    # Backslash escapes for the characters this renderer treats as syntax. Run
    # AFTER code spans are stashed (so a backslash inside `code` stays literal)
    # and BEFORE the link/image/emphasis rules, so the edit round-trip can write
    # a literal `*`/`` ` ``/`[`/`]`/`\` as \X and have it render as the bare char.
    escaped = re.sub(r"\\([\\`*\[\]~])", lambda m: stash(m.group(1)), escaped)
    # ![alt](src) images — must run before the link rule (it contains [alt](src))
    escaped = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: stash(_image(m.group(2), m.group(1))),
        escaped,
    )
    # [label](url) — the label is emphasis-processed so bold/italic/strike inside
    # link text render (code spans + escaped chars in it are already stashed).
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: stash(_anchor(m.group(2), emph(m.group(1)))),
        escaped,
    )
    # <url> autolinks (angle brackets were escaped to &lt;...&gt;)
    escaped = re.sub(
        r"&lt;(https?://[^\s&<>]+?)&gt;",
        lambda m: stash(_anchor(m.group(1), m.group(1))),
        escaped,
    )
    # bare URLs in remaining text (trailing punctuation left outside the link)
    escaped = re.sub(
        r"(?<![\"'>=])\b(https?://[^\s<>()]+[^\s<>().,;:!?])",
        lambda m: stash(_anchor(m.group(1), m.group(1))),
        escaped,
    )
    escaped = emph(escaped)

    # Restore placeholders, repeatedly. A stashed anchor's label can itself hold
    # placeholders (escaped chars / code spans), so a single pass would leave the
    # inner \x00N\x00 markers (NUL bytes) in the output. Loop until stable. An
    # out-of-range index (only reachable via a crafted NUL in the source, which
    # html2md now strips) is dropped rather than raising or leaking the NUL.
    n = len(placeholders)
    while "\x00" in escaped:
        expanded = re.sub(
            r"\x00(\d+)\x00",
            lambda m: placeholders[int(m.group(1))] if int(m.group(1)) < n else "",
            escaped,
        )
        if expanded == escaped:
            break  # a stray \x00 that is not a valid marker; stop rather than spin
        escaped = expanded
    return leading_literal + escaped


def split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    # A trailing pipe is the row's closing delimiter only when it is not an
    # escaped pipe (\|) belonging to the last cell's text.
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    # Split on UNescaped pipes only, then unescape \| -> | per cell (standard
    # Markdown). A row with no backslash splits exactly as before, so existing
    # tables render unchanged.
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def is_table_separator(line: str) -> bool:
    cells = split_table_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def cell_marker(cell: str) -> tuple[str | None, str]:
    """Return (marker, remaining_text) where marker is one of [+]/[-]/[~] or None."""
    stripped = cell.strip()
    for token in ("[+]", "[-]", "[~]"):
        if stripped.startswith(token):
            return token, stripped[len(token):].strip()
    return None, cell


def unique_id(text: str, used: set[str]) -> str:
    base = slugify(text, "section")
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def step_chunks(block: str) -> list[str]:
    return [chunk.strip() for chunk in re.split(r"^\s*---\s*$", block, flags=re.MULTILINE) if chunk.strip()]


def render_step_body(chunk: str) -> str:
    paragraphs = re.split(r"\n\s*\n", chunk.strip())
    out = []
    for para in paragraphs:
        joined = " ".join(line.strip() for line in para.splitlines() if line.strip())
        if joined:
            out.append(f"<p>{inline_md(joined)}</p>")
    return "".join(out)


def render_stepper(block: str, meta: str, mode: str, state: dict) -> str:
    state["stepper"] = True
    title_match = re.search(r'title="([^"]*)"', meta)
    title = title_match.group(1) if title_match else ""
    chunks = step_chunks(block)

    if mode == "doc":
        head = f"<p><strong>{html.escape(title)}</strong></p>" if title else ""
        items = "".join(f"<li>{inline_md(' '.join(c.split()))}</li>" for c in chunks)
        return f'{head}<ol class="stepper-static">{items}</ol>'

    caption = f"<figcaption>{html.escape(title)}</figcaption>" if title else ""
    steps = []
    for idx, chunk in enumerate(chunks):
        hidden = "" if idx == 0 else " hidden"
        steps.append(f'<div class="step" data-step="{idx + 1}"{hidden}>{render_step_body(chunk)}</div>')
    total = len(chunks)
    return (
        '<figure class="stepper" data-stepper data-noedit aria-roledescription="step-through visualization">'
        f"{caption}"
        f'<div class="stepper-steps">{"".join(steps)}</div>'
        '<div class="stepper-controls">'
        '<button type="button" data-stepper-prev>Prev</button>'
        f'<span data-stepper-status>1 / {total}</span>'
        '<button type="button" data-stepper-next>Next</button>'
        '<button type="button" data-stepper-reset>Reset</button>'
        "</div>"
        "</figure>"
    )


def render_embed(block: str, mode: str, state: dict) -> str:
    state["embed"] = True
    low = block.lower()
    if "<audio" in low:
        state["dropped"].add("audio")
    if "<video" in low:
        state["dropped"].add("video")
    if "<script" in low:
        state["dropped"].add("custom JavaScript")
    if "<iframe" in low:
        state["dropped"].add("embedded frame")
    state["dropped"].add("interactive HTML embeds")
    if mode == "doc":
        return "<p><em>[Interactive element omitted in the document export — view the website version.]</em></p>"
    # Wrap with data-noedit so edit mode gives embeds the same view-only
    # affordance as the stepper/code/blockquote (and never makes them editable).
    return f'<div class="webdoc-embed" data-noedit>{block}</div>'


def _md_attrs(lines: list[str], start0: int, end0_excl: int, btype: str) -> str:
    """Edit-mode identity for a line-range block (paragraph/heading/listitem).

    start0/end0_excl are 0-based [start, end) into `lines`. Emits 1-based
    inclusive data-md-start/end, a hash of the exact source slice, and the type
    so the in-page editor can locate and round-trip the block."""
    digest = block_hash("\n".join(lines[start0:end0_excl]))
    return (
        f' data-md-start="{start0 + 1}" data-md-end="{end0_excl}"'
        f' data-md-hash="{digest}" data-md-type="{btype}"'
    )


def _cell_attrs(line0: int, col: int, field: str) -> str:
    """Edit-mode identity for a table cell (sub-line unit).

    line0 is the row's 0-based source line; col is the 0-based column. `field`
    is the raw (split_table_row-stripped) cell source, hashed for drift checks."""
    digest = block_hash(field)
    return (
        f' data-md-line="{line0 + 1}" data-md-cell="{col}"'
        f' data-md-hash="{digest}" data-md-type="tablecell"'
    )


def parse_markdown(markdown: str, mode: str = "site") -> tuple[str, list[dict[str, str]], dict]:
    lines = markdown.splitlines()
    out: list[str] = []
    toc: list[dict[str, str]] = []
    used_ids: set[str] = set()
    state: dict = {"dropped": set(), "stepper": False, "embed": False}
    # Edit-mode block identity is emitted only for the interactive site, never
    # the doc.html export (which must stay clean for the Google Docs path).
    editable = mode == "site"
    noedit = " data-noedit" if editable else ""
    i = 0
    pending_blanks = 0  # consecutive blank source lines seen before the next block

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            pending_blanks += 1
            i += 1
            continue

        # Blank lines beyond the block separator render as visible vertical gaps:
        # this is how extra spacing is authored (the editor's Enter-at-block-start
        # writes one). One blank is the normal separator between two blocks; the
        # first block has no separator, so every leading blank counts as a gap.
        # Site mode only - the doc export stays clean. Capped so a pathological run
        # of blanks can't blow out the page.
        if editable and pending_blanks:
            extra = pending_blanks - (1 if out else 0)
            if extra > 0:
                gap = '<div class="webdoc-gap" data-noedit aria-hidden="true" style="height:0.7em"></div>'
                out.append(gap * min(extra, 12))
        pending_blanks = 0

        block_start = i  # 0-based source line where this block begins

        fence = re.match(r"^```\s*([A-Za-z][\w.+-]*)?\s*(.*?)\s*$", stripped)
        if fence:
            language = (fence.group(1) or "").lower()
            meta = fence.group(2) or ""
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            block = "\n".join(code_lines)
            if language == "stepper":
                out.append(render_stepper(block, meta, mode, state))
                continue
            if language in ("embed", "component"):
                out.append(render_embed(block, mode, state))
                continue
            class_attr = f' class="language-{html.escape(language, quote=True)}"' if language else ""
            out.append(f"<pre{noedit}><code{class_attr}>{html.escape(block)}</code></pre>")
            continue

        heading = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            section_id = unique_id(text, used_ids)
            if level <= 3:
                toc.append({"level": str(level), "id": section_id, "text": text})
            attrs = _md_attrs(lines, block_start, block_start + 1, "heading") if editable else ""
            out.append(f'<h{level} id="{section_id}"{attrs}>{inline_md(text)}</h{level}>')
            i += 1
            continue

        if (
            "|" in line
            and i + 1 < len(lines)
            and "|" in lines[i + 1]
            and is_table_separator(lines[i + 1])
        ):
            header_idx = block_start  # 0-based source line of the header row
            headers = split_table_row(line)
            i += 2
            rows: list[list[str]] = []
            row_idxs: list[int] = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(split_table_row(lines[i]))
                row_idxs.append(i)
                i += 1
            table = ['<div class="table-wrap"><table><thead><tr>']
            for col, cell in enumerate(headers):
                _, text = cell_marker(cell)
                cattrs = _cell_attrs(header_idx, col, cell) if editable else ""
                table.append(f"<th{cattrs}>{inline_md(text)}</th>")
            table.append("</tr></thead><tbody>")
            for r, row in enumerate(rows):
                table.append("<tr>")
                for idx in range(len(headers)):
                    cell = row[idx] if idx < len(row) else ""
                    token, text = cell_marker(cell)
                    if token and mode == "doc":
                        attr = f' style="{CELL_DOC_STYLE[token]}"'
                    elif token:
                        attr = f' class="{CELL_CLASS[token]}"'
                    else:
                        attr = ""
                    cattrs = _cell_attrs(row_idxs[r], idx, cell) if editable else ""
                    table.append(f"<td{attr}{cattrs}>{inline_md(text)}</td>")
                table.append("</tr>")
            table.append("</tbody></table></div>")
            out.append("".join(table))
            continue

        if re.match(r"^\s*[-*]\s+", line):
            items: list[tuple[str, int]] = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append((re.sub(r"^\s*[-*]\s+", "", lines[i]).strip(), i))
                i += 1
            lis = []
            for item, idx0 in items:
                attrs = _md_attrs(lines, idx0, idx0 + 1, "listitem") if editable else ""
                lis.append(f"<li{attrs}>{inline_md(item)}</li>")
            out.append("<ul>" + "".join(lis) + "</ul>")
            continue

        if re.match(r"^\s*\d+[.)]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append((re.sub(r"^\s*\d+[.)]\s+", "", lines[i]).strip(), i))
                i += 1
            lis = []
            for item, idx0 in items:
                attrs = _md_attrs(lines, idx0, idx0 + 1, "listitem") if editable else ""
                lis.append(f"<li{attrs}>{inline_md(item)}</li>")
            out.append("<ol>" + "".join(lis) + "</ol>")
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote{noedit}><p>{inline_md(' '.join(quote_lines))}</p></blockquote>")
            continue

        para: list[str] = [stripped]
        i += 1
        while i < len(lines):
            look = lines[i]
            look_stripped = look.strip()
            if not look_stripped:
                break
            if (
                look_stripped.startswith("```")
                or re.match(r"^(#{1,4})\s+", look)
                or re.match(r"^\s*[-*]\s+", look)
                or re.match(r"^\s*\d+[.)]\s+", look)
                or look_stripped.startswith(">")
                or (
                    "|" in look
                    and i + 1 < len(lines)
                    and "|" in lines[i + 1]
                    and is_table_separator(lines[i + 1])
                )
            ):
                break
            para.append(look_stripped)
            i += 1
        attrs = _md_attrs(lines, block_start, i, "paragraph") if editable else ""
        out.append(f"<p{attrs}>{inline_md(' '.join(para))}</p>")

    return "\n".join(out), toc, state


def inline_doc_images(html_str: str, base_dir: Path, state: dict) -> str:
    """Inline local images as data: URIs so doc.html is a single self-contained file."""
    base_resolved = base_dir.resolve()

    def repl(match: re.Match) -> str:
        tag = match.group(0)
        src = match.group(1)
        alt_match = re.search(r'\balt="([^"]*)"', tag)
        alt = alt_match.group(1) if alt_match else ""
        if src.startswith("data:"):
            return tag
        if re.match(r"^[a-z][a-z0-9+.-]*://", src, re.IGNORECASE):
            state["dropped"].add("images")
            return f"<em>[image: {alt}]</em>"
        candidate = (base_dir / src).resolve()
        try:
            candidate.relative_to(base_resolved)
        except ValueError:
            state["dropped"].add("images")
            return f"<em>[image: {alt}]</em>"
        ext = candidate.suffix.lower()
        if candidate.is_file() and ext in IMG_MIME:
            data = base64.b64encode(candidate.read_bytes()).decode("ascii")
            return f'<img src="data:{IMG_MIME[ext]};base64,{data}" alt="{alt}">'
        state["dropped"].add("images")
        return f"<em>[image: {alt}]</em>"

    return IMG_TAG_RE.sub(repl, html_str)


def render_html(
    title: str,
    source: Path,
    body_html: str,
    toc: list[dict[str, str]],
    manifest: dict[str, object],
    custom_css: list[str] | None = None,
    custom_js: list[str] | None = None,
    include_stepper: bool = False,
) -> str:
    generated = html.escape(str(manifest["generated_at"]))
    source_display = html.escape(str(source))
    source_name = html.escape(Path(source).name, quote=True)
    title_html = html.escape(title)
    artifact_json = json.dumps(str(manifest["artifact_id"]))

    if toc:
        toc_items = "\n".join(
            f'<li class="toc-level-{item["level"]}"><a href="#{html.escape(item["id"], quote=True)}">{html.escape(item["text"])}</a></li>'
            for item in toc
        )
        toc_html = f"<nav class=\"toc\" aria-label=\"Table of contents\"><h2>Contents</h2><ol>{toc_items}</ol></nav>"
    else:
        toc_html = '<nav class="toc" aria-label="Table of contents"><h2>Contents</h2><p>No section headings found.</p></nav>'

    manifest_json = html.escape(json.dumps(manifest, indent=2, sort_keys=True))

    css_links = "".join(
        f'\n  <link rel="stylesheet" href="./{html.escape(name, quote=True)}">'
        for name in (custom_css or [])
    )
    scripts = ""
    if include_stepper:
        scripts += '\n    <script src="./stepper.js" defer></script>'
    for name in (custom_js or []):
        scripts += f'\n    <script src="./{html.escape(name, quote=True)}" defer></script>'
    # Editing mode: bundled into the interactive site only. Inert until the
    # reader clicks Edit; the doc.html export never loads this.
    scripts += '\n    <script src="./edit.js" defer></script>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_html}</title>
  <link rel="stylesheet" href="./style.css">{css_links}
  <link rel="stylesheet" href="./edit.css">
</head>
<body data-webdoc-source="{source_name}">
  <div class="shell">
    <header class="masthead">
      <p class="eyebrow">Local document site</p>
      <h1>{title_html}</h1>
      <div class="meta">
        <span>Generated {generated}</span>
        <span>Source: <code>{source_display}</code></span>
      </div>
    </header>
    <div class="layout">
      {toc_html}
      <main>
{body_html}
        <section class="feedback" id="feedback">
          <h2>Feedback</h2>
          <p>Use this box for review notes. When this page is opened from its localhost preview, your note is saved to <code>feedback.jsonl</code> in this website folder so the agent can read it directly.</p>
          <form id="feedback-form">
            <label for="feedback-text">Notes for the agent</label>
            <textarea id="feedback-text" name="feedback" rows="7" placeholder="Type feedback, corrections, decisions, or follow-up instructions here."></textarea>
            <div class="feedback-actions">
              <button type="submit">Save feedback</button>
              <span id="feedback-status" role="status" aria-live="polite"></span>
            </div>
          </form>
        </section>
        <details class="footer">
          <summary>Site manifest</summary>
          <pre><code>{manifest_json}</code></pre>
        </details>
      </main>
    </div>
  </div>{scripts}
  <script>
    const feedbackForm = document.getElementById('feedback-form');
    const feedbackText = document.getElementById('feedback-text');
    const feedbackStatus = document.getElementById('feedback-status');
    feedbackForm.addEventListener('submit', async (event) => {{
      event.preventDefault();
      const feedback = feedbackText.value.trim();
      if (!feedback) {{
        feedbackStatus.textContent = 'Enter feedback first.';
        return;
      }}
      feedbackStatus.textContent = 'Saving...';
      try {{
        const response = await fetch('/api/feedback', {{
          method: 'POST',
          headers: {{ 'content-type': 'application/json' }},
          body: JSON.stringify({{
            artifact_id: {artifact_json},
            page: window.location.pathname,
            feedback
          }})
        }});
        const body = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(body.error || `HTTP ${{response.status}}`);
        feedbackText.value = '';
        feedbackStatus.textContent = `Saved to ${{body.feedback_path || 'feedback.jsonl'}}.`;
      }} catch (error) {{
        feedbackStatus.textContent = 'Could not save here; open the localhost preview and try again.';
      }}
    }});
  </script>
</body>
</html>
"""


DOC_STYLE = """
body { max-width: 7.5in; margin: 0 auto; padding: 0.5in 0.6in;
  font: 12pt/1.5 -apple-system, "Segoe UI", Arial, sans-serif; color: #111; }
h1, h2, h3, h4 { line-height: 1.2; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #999; padding: 6px 9px; text-align: left; vertical-align: top; }
th { background: #eee; }
code { font-family: ui-monospace, Menlo, Consolas, monospace; background: #f2f2f2; padding: 0 3px; }
pre { background: #f2f2f2; padding: 10px; overflow-x: auto; }
pre code { background: transparent; padding: 0; }
ol.stepper-static { padding-left: 1.4em; }
blockquote { margin: 0 0 1em; padding-left: 1em; border-left: 3px solid #ccc; color: #444; }
img { max-width: 100%; height: auto; }
""".strip()


def render_doc_html(title: str, body_html: str) -> str:
    title_html = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_html}</title>
<style>{DOC_STYLE}</style>
</head>
<body>
<h1>{title_html}</h1>
{body_html}
</body>
</html>
"""


def read_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def copy_into(src: Path, dest_dir: Path, subdir: str | None = None) -> str:
    src = src.expanduser()
    if not src.is_file():
        raise SystemExit(f"file not found: {src}")
    target_dir = dest_dir / subdir if subdir else dest_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target_dir / src.name)
    return src.name


def write_registry(out_dir: Path, manifest: dict[str, object]) -> None:
    try:
        out_resolved = out_dir.resolve()
        root_resolved = DEFAULT_ROOT.resolve()
        out_resolved.relative_to(root_resolved)
    except Exception:
        return

    registry_path = root_resolved / "index.json"
    registry: dict[str, object]
    if registry_path.exists():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            registry = {"artifacts": {}}
    else:
        registry = {"artifacts": {}}
    artifacts = registry.setdefault("artifacts", {})
    if isinstance(artifacts, dict):
        artifacts[str(manifest["artifact_id"])] = {
            "title": manifest["title"],
            "source_path": manifest["source_path"],
            "site_dir": manifest["site_dir"],
            "generated_at": manifest["generated_at"],
        }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically (temp file in the same dir + rename), so a
    concurrent HTTP GET never reads a half-written page during a rebuild."""
    directory = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".webdoc-rebuild-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rebuild_html(source_path: str | Path, out_dir: str | Path) -> bool:
    """Regenerate index.html + doc.html from the current source, reusing the
    manifest's build settings (title, bundled custom css/js, stepper).

    This is what makes an edit made via /api/edit survive a browser reload: the
    write path updates the canonical .md, and the server calls this so the served
    static page is re-rendered from it. No lint gate and no asset re-copy - only
    the HTML is regenerated (style.css / edit.js / edit.css / stepper.js and any
    bundled media are already in place and unchanged by an edit). Returns True on
    success; never raises, because a rebuild failure must not undo the edit that
    already persisted to the source."""
    try:
        out_dir = Path(out_dir)
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return False
        source = Path(source_path)
        markdown = source.read_text(encoding="utf-8", errors="replace")

        body_html, toc, state = parse_markdown(markdown, mode="site")
        doc_body, _, doc_state = parse_markdown(markdown, mode="doc")
        doc_body = inline_doc_images(doc_body, out_dir, doc_state)

        title = str(manifest.get("title") or read_title(markdown, source.stem))
        # Coerce to a list before filtering: a hand-corrupted manifest whose
        # custom_css is a bare string would otherwise iterate its characters and
        # emit one <link> per character.
        raw_css = manifest.get("custom_css")
        raw_js = manifest.get("custom_js")
        custom_css = [c for c in raw_css if isinstance(c, str)] if isinstance(raw_css, list) else []
        custom_js = [j for j in raw_js if isinstance(j, str)] if isinstance(raw_js, list) else []

        # Keep the manifest's source fingerprint honest after the edit.
        manifest["source_sha256"] = source_hash(source)
        try:
            manifest["source_mtime"] = datetime.fromtimestamp(
                source.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        except OSError:
            pass
        manifest["generated_at"] = now_iso()

        page = render_html(
            title, source.resolve(), body_html, toc, manifest,
            custom_css=custom_css, custom_js=custom_js,
            include_stepper=bool(state.get("stepper")),
        )
        doc_page = render_doc_html(title, doc_body)

        # index.html first: reload-persistence depends on it. Each write is
        # individually atomic; the set is not transactional, so if a later write
        # fails the manifest fingerprint may lag - harmless, since edit conflict
        # checks recompute block hashes from the live source, not the manifest.
        _atomic_write(out_dir / "index.html", page)
        _atomic_write(out_dir / "doc.html", doc_page)
        _atomic_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a static HTML website from a Markdown file.")
    parser.add_argument("source", type=Path, help="Canonical Markdown/report source file")
    parser.add_argument("--out", type=Path, help="Output directory. Defaults to ~/agent-artifacts/sites/<artifact-id>")
    parser.add_argument("--title", help="Override page title")
    parser.add_argument("--artifact-id", help="Override artifact id")
    parser.add_argument("--snapshot", action="store_true", help="Append a timestamp to the artifact id")
    parser.add_argument("--template", default="standard", help="Theme template: a name (standard or a saved category) or a path to a style.css")
    parser.add_argument("--css", action="append", default=[], metavar="PATH", help="Extra stylesheet to bundle and link (repeatable)")
    parser.add_argument("--js", action="append", default=[], metavar="PATH", help="Extra script to bundle and load deferred (repeatable)")
    parser.add_argument("--asset", action="append", default=[], metavar="PATH", help="Media file to copy into the site's assets/ (repeatable)")
    parser.add_argument("--no-lint", action="store_true", help="skip the AI-writing prose gate (escape hatch; not recommended)")
    args = parser.parse_args()

    source = args.source.expanduser()
    if not source.exists() or not source.is_file():
        print(f"source file not found: {source}", file=sys.stderr)
        return 2

    artifact_id = args.artifact_id or stable_artifact_id(source, snapshot=args.snapshot)
    out_dir = (args.out.expanduser() if args.out else DEFAULT_ROOT / artifact_id).resolve()
    if out_dir.exists() and out_dir.is_symlink():
        print(f"refusing to write into symlinked output directory: {out_dir}", file=sys.stderr)
        return 2

    markdown = source.read_text(encoding="utf-8", errors="replace")

    # Prose gate: block on structural AI-writing tells before building. Native
    # (scripts/lint_prose.py), no external dependency. See references/structural-tells.md.
    if not args.no_lint:
        linter = Path(__file__).resolve().parent / "lint_prose.py"
        if linter.exists():
            gate = subprocess.run([sys.executable, str(linter), str(source)])
            if gate.returncode == 1:
                print(
                    "build blocked: structural AI-writing tells found above. "
                    "Fix them, or rebuild with --no-lint to bypass.",
                    file=sys.stderr,
                )
                return 1

    title = args.title or read_title(markdown, source.stem.replace("_", " ").replace("-", " ").title())
    digest = source_hash(source)
    generated_at = now_iso()

    body_html, toc, state = parse_markdown(markdown, mode="site")
    doc_body, _, doc_state = parse_markdown(markdown, mode="doc")

    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resolve_template(args.template), out_dir / "style.css")
    if state.get("stepper"):
        shutil.copyfile(SKILL_DIR / "assets" / "stepper.js", out_dir / "stepper.js")
    # Editing-mode assets, bundled into every site (index.html links them; the
    # doc.html export does not). Inert until the reader clicks Edit.
    shutil.copyfile(SKILL_DIR / "assets" / "edit.js", out_dir / "edit.js")
    shutil.copyfile(SKILL_DIR / "assets" / "edit.css", out_dir / "edit.css")

    for asset in args.asset:
        copy_into(Path(asset), out_dir, subdir="assets")
    custom_css = [copy_into(Path(path), out_dir) for path in args.css]
    custom_js = [copy_into(Path(path), out_dir) for path in args.js]

    doc_body = inline_doc_images(doc_body, out_dir, doc_state)

    dropped = set(state["dropped"]) | set(doc_state["dropped"])
    if custom_css:
        dropped.add("custom CSS")
    if custom_js:
        dropped.add("custom JavaScript")
    if state.get("stepper"):
        dropped.add("interactive stepper (flattened to a numbered list in the doc)")
    dropped_features = sorted(dropped)

    doc_export = {
        "path": str(out_dir / "doc.html"),
        "import_steps": "Upload doc.html to Google Drive, then right-click it -> Open with -> Google Docs.",
        "dropped_features": dropped_features,
    }

    manifest: dict[str, object] = {
        "artifact_id": artifact_id,
        "title": title,
        "source_path": str(source.resolve()),
        "source_sha256": digest,
        "source_mtime": datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "site_dir": str(out_dir),
        "index_path": str(out_dir / "index.html"),
        "feedback_path": str(out_dir / "feedback.jsonl"),
        "generated_at": generated_at,
        "generator": "webdoc/create_site.py",
        "template": args.template,
        "custom_css": custom_css,
        "custom_js": custom_js,
        "doc_export": doc_export,
    }

    page = render_html(
        title, source.resolve(), body_html, toc, manifest,
        custom_css=custom_css, custom_js=custom_js, include_stepper=bool(state.get("stepper")),
    )
    doc_page = render_doc_html(title, doc_body)

    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / "doc.html").write_text(doc_page, encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_registry(out_dir, manifest)

    result = {
        "artifact_id": artifact_id,
        "site_dir": str(out_dir),
        "index_path": str(out_dir / "index.html"),
        "doc_export": doc_export,
        "manifest_path": str(out_dir / "manifest.json"),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
