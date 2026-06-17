#!/usr/bin/env python3
"""Assemble a concepts gallery: one page that switches between concept variations.

Each concept is an already-built webdoc site (a directory with index.html). gallery.py
copies them under one gallery directory and writes a switcher index.html — a tab bar over
an iframe, a pop-out link, and a "Choose this concept" control that records the pick to
feedback.jsonl (via serve_site's API). Serve the gallery directory with serve_site.py to
review the concepts and capture the user's choice.

Usage:
  gallery.py --out ./gallery --title "Clock cycle" \\
      --concept "Discrete steps=/path/site_a" --concept "Timeline scrub=/path/site_b" ...
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path


IGNORE = shutil.ignore_patterns("server.json", "server.log", "feedback.jsonl")


def parse_concept(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise SystemExit(f"--concept must be 'Label=PATH', got: {spec!r}")
    label, path = spec.split("=", 1)
    label = label.strip()
    site = Path(path.strip()).expanduser()
    if not (site / "index.html").is_file():
        raise SystemExit(f"concept site has no index.html: {site}")
    return label or site.name, site


def render_gallery(title: str, gallery_id: str, concepts: list[dict]) -> str:
    title_html = html.escape(title)
    tabs = "\n".join(
        f'<button class="tab" data-i="{i}">{html.escape(c["label"])}</button>'
        for i, c in enumerate(concepts)
    )
    concepts_json = json.dumps([{"id": c["id"], "label": c["label"], "href": f'{c["id"]}/index.html'} for c in concepts])
    gallery_json = json.dumps(gallery_id)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_html} — concepts</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; height: 100vh; display: flex; flex-direction: column;
    font: 15px/1.5 -apple-system, "Segoe UI", sans-serif; background: #14181a; color: #eef5f2; }}
  header {{ padding: 10px 14px; border-bottom: 1px solid #2a3236; }}
  .row {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
  h1 {{ font-size: 1rem; margin: 0 12px 0 0; }}
  .tabs {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .tab {{ appearance: none; border: 1px solid #2a3236; background: #1b2124; color: #cfe; padding: 6px 12px;
    font: inherit; cursor: pointer; border-radius: 6px; }}
  .tab.active {{ background: #0f766e; border-color: #0f766e; color: white; }}
  .spacer {{ flex: 1; }}
  a.popout, .pick {{ color: #9fe; font-size: 0.9rem; }}
  .pick-row {{ margin-top: 8px; }}
  input#note {{ flex: 1; min-width: 180px; padding: 6px 9px; background: #1b2124; border: 1px solid #2a3236;
    color: inherit; font: inherit; border-radius: 6px; }}
  button#pick {{ appearance: none; border: 0; background: #0f766e; color: white; font: inherit; font-weight: 600;
    padding: 7px 14px; border-radius: 6px; cursor: pointer; }}
  #status {{ color: #9fb; font-size: 0.88rem; }}
  iframe {{ flex: 1; width: 100%; border: 0; background: white; }}
</style>
</head>
<body>
  <header>
    <div class="row">
      <h1>{title_html}</h1>
      <div class="tabs">{tabs}</div>
      <span class="spacer"></span>
      <a class="popout" id="popout" href="#" target="_blank" rel="noopener">Pop out ↗</a>
    </div>
    <div class="row pick-row">
      <strong id="current-label"></strong>
      <input id="note" placeholder="Why this one? (optional note)">
      <button id="pick" type="button">Choose this concept</button>
      <span id="status" role="status" aria-live="polite"></span>
    </div>
  </header>
  <iframe id="frame" title="concept preview"></iframe>
  <script>
    const concepts = {concepts_json};
    const galleryId = {gallery_json};
    const frame = document.getElementById('frame');
    const popout = document.getElementById('popout');
    const tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
    const status = document.getElementById('status');
    let current = 0;

    function show(i) {{
      current = i;
      frame.src = concepts[i].href;
      popout.href = concepts[i].href;
      document.getElementById('current-label').textContent = concepts[i].label;
      tabs.forEach((t, j) => t.classList.toggle('active', j === i));
    }}

    tabs.forEach((t, i) => t.addEventListener('click', () => show(i)));
    window.addEventListener('keydown', (e) => {{
      const n = parseInt(e.key, 10);
      if (n >= 1 && n <= concepts.length) show(n - 1);
    }});

    document.getElementById('pick').addEventListener('click', async () => {{
      const note = document.getElementById('note').value.trim();
      const c = concepts[current];
      const feedback = `CHOSEN: ${{c.label}} [${{c.id}}]` + (note ? `\\n${{note}}` : '');
      status.textContent = 'Saving...';
      try {{
        const res = await fetch('/api/feedback', {{
          method: 'POST', headers: {{ 'content-type': 'application/json' }},
          body: JSON.stringify({{ artifact_id: galleryId, page: c.id, feedback }})
        }});
        const body = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(body.error || res.status);
        status.textContent = `Recorded: ${{c.label}}.`;
      }} catch (err) {{
        status.textContent = 'Could not save; open from the localhost preview and retry.';
      }}
    }});

    show(0);
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble a concepts gallery with a switcher page.")
    parser.add_argument("--out", type=Path, required=True, help="Gallery output directory")
    parser.add_argument("--title", default="Concepts", help="Gallery title")
    parser.add_argument("--concept", action="append", default=[], metavar="LABEL=PATH",
                        help="A concept: a label and a built site dir with index.html (repeatable)")
    args = parser.parse_args()

    if len(args.concept) < 2:
        raise SystemExit("provide at least two --concept entries")

    out_dir = args.out.expanduser().resolve()
    if out_dir.exists() and out_dir.is_symlink():
        raise SystemExit(f"refusing to write into symlinked output directory: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    concepts: list[dict] = []
    for i, spec in enumerate(args.concept, start=1):
        label, site = parse_concept(spec)
        cid = f"c{i}"
        dest = out_dir / cid
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(site, dest, ignore=IGNORE)
        concepts.append({"id": cid, "label": label, "source": str(site)})

    gallery_id = re.sub(r"[^a-z0-9]+", "-", args.title.lower()).strip("-") or "concepts"
    (out_dir / "index.html").write_text(render_gallery(args.title, gallery_id, concepts), encoding="utf-8")
    manifest = {"title": args.title, "gallery_id": gallery_id, "gallery_dir": str(out_dir), "concepts": concepts}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result = {
        "gallery_dir": str(out_dir),
        "concepts": [{"id": c["id"], "label": c["label"]} for c in concepts],
        "serve": f"python3 {Path(__file__).resolve().parent}/serve_site.py start {out_dir}",
        "feedback_path": str(out_dir / "feedback.jsonl"),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
