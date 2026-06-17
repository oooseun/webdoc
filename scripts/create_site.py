#!/usr/bin/env python3
"""Create a static localhost-friendly website from a Markdown report."""

from __future__ import annotations

import argparse
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


def inline_md(text: str) -> str:
    escaped = html.escape(text)

    # Protect generated tags via placeholders so later passes (e.g. bare-URL
    # autolinking) never re-process URLs already inside an <a>/<code> element.
    placeholders: list[str] = []

    def stash(htmlfrag: str) -> str:
        placeholders.append(htmlfrag)
        return f"\x00{len(placeholders) - 1}\x00"

    escaped = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{m.group(1)}</code>"), escaped)
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


def unique_id(text: str, used: set[str]) -> str:
    base = slugify(text, "section")
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def parse_markdown(markdown: str) -> tuple[str, list[dict[str, str]]]:
    lines = markdown.splitlines()
    out: list[str] = []
    toc: list[dict[str, str]] = []
    used_ids: set[str] = set()
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        fence = re.match(r"^```(\w[\w.+-]*)?\s*$", stripped)
        if fence:
            language = fence.group(1) or ""
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            class_attr = f' class="language-{html.escape(language, quote=True)}"' if language else ""
            code = html.escape("\n".join(code_lines))
            out.append(f"<pre><code{class_attr}>{code}</code></pre>")
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
            table.extend(f"<th>{inline_md(cell)}</th>" for cell in headers)
            table.append("</tr></thead><tbody>")
            for row in rows:
                table.append("<tr>")
                for idx in range(len(headers)):
                    cell = row[idx] if idx < len(row) else ""
                    table.append(f"<td>{inline_md(cell)}</td>")
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

    return "\n".join(out), toc


def render_html(title: str, source: Path, body_html: str, toc: list[dict[str, str]], manifest: dict[str, object]) -> str:
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

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_html}</title>
  <link rel="stylesheet" href="./style.css">
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
  </div>
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


def read_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


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
    }

    body_html, toc = parse_markdown(markdown)
    page = render_html(title, source.resolve(), body_html, toc, manifest)

    out_dir.mkdir(parents=True, exist_ok=True)
    css_src = SKILL_DIR / "assets" / "report.css"
    shutil.copyfile(css_src, out_dir / "style.css")
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_registry(out_dir, manifest)

    result = {
        "artifact_id": artifact_id,
        "site_dir": str(out_dir),
        "index_path": str(out_dir / "index.html"),
        "manifest_path": str(out_dir / "manifest.json"),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
