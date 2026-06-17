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
import sys
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


def stable_artifact_id(path: Path, snapshot: bool = False) -> str:
    resolved = path.resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    base = f"{slugify(path.stem)}-{digest}"
    if snapshot:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{base}-{stamp}"
    return base


def safe_href(href: str) -> str:
    href = href.strip()
    lowered = href.lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:")):
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


def inline_md(text: str) -> str:
    escaped = html.escape(text)

    # Protect generated tags via placeholders so later passes (e.g. bare-URL
    # autolinking) never re-process URLs already inside an <a>/<code>/<img> element.
    placeholders: list[str] = []

    def stash(htmlfrag: str) -> str:
        placeholders.append(htmlfrag)
        return f"\x00{len(placeholders) - 1}\x00"

    escaped = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{m.group(1)}</code>"), escaped)
    # ![alt](src) images — must run before the link rule (it contains [alt](src))
    escaped = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: stash(_image(m.group(2), m.group(1))),
        escaped,
    )
    # [label](url)
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: stash(_anchor(m.group(2), m.group(1))),
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
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)

    # Restore placeholders
    escaped = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], escaped)
    return escaped


def split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


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
        '<figure class="stepper" data-stepper aria-roledescription="step-through visualization">'
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
    return block


def parse_markdown(markdown: str, mode: str = "site") -> tuple[str, list[dict[str, str]], dict]:
    lines = markdown.splitlines()
    out: list[str] = []
    toc: list[dict[str, str]] = []
    used_ids: set[str] = set()
    state: dict = {"dropped": set(), "stepper": False, "embed": False}
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

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
            out.append(f"<pre><code{class_attr}>{html.escape(block)}</code></pre>")
            continue

        heading = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            section_id = unique_id(text, used_ids)
            if level <= 3:
                toc.append({"level": str(level), "id": section_id, "text": text})
            out.append(f'<h{level} id="{section_id}">{inline_md(text)}</h{level}>')
            i += 1
            continue

        if (
            "|" in line
            and i + 1 < len(lines)
            and "|" in lines[i + 1]
            and is_table_separator(lines[i + 1])
        ):
            headers = split_table_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(split_table_row(lines[i]))
                i += 1
            table = ['<div class="table-wrap"><table><thead><tr>']
            for cell in headers:
                _, text = cell_marker(cell)
                table.append(f"<th>{inline_md(text)}</th>")
            table.append("</tr></thead><tbody>")
            for row in rows:
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
                    table.append(f"<td{attr}>{inline_md(text)}</td>")
                table.append("</tr>")
            table.append("</tbody></table></div>")
            out.append("".join(table))
            continue

        if re.match(r"^\s*[-*]\s+", line):
            items: list[str] = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]).strip())
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline_md(item)}</li>" for item in items) + "</ul>")
            continue

        if re.match(r"^\s*\d+[.)]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]).strip())
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline_md(item)}</li>" for item in items) + "</ol>")
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote><p>{inline_md(' '.join(quote_lines))}</p></blockquote>")
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
        out.append(f"<p>{inline_md(' '.join(para))}</p>")

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

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_html}</title>
  <link rel="stylesheet" href="./style.css">{css_links}
</head>
<body>
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
    title = args.title or read_title(markdown, source.stem.replace("_", " ").replace("-", " ").title())
    digest = source_hash(source)
    generated_at = now_iso()

    body_html, toc, state = parse_markdown(markdown, mode="site")
    doc_body, _, doc_state = parse_markdown(markdown, mode="doc")

    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resolve_template(args.template), out_dir / "style.css")
    if state.get("stepper"):
        shutil.copyfile(SKILL_DIR / "assets" / "stepper.js", out_dir / "stepper.js")

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
