#!/usr/bin/env python3
"""Tests for mermaid diagram support in create_site.py.

The website renders ```mermaid fences with the vendored library; the doc.html
export stays script-free and shows the diagram source instead. These exercise
the importable renderer plus a couple of full CLI builds into temp dirs.

Pure stdlib. Run directly:

    python3 tests/test_mermaid.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ASSETS = ROOT / "assets"
sys.path.insert(0, str(SCRIPTS))

import create_site  # noqa: E402

TESTS: list[tuple[str, "callable"]] = []


def test(name: str):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def eq(got, want, what=""):
    assert got == want, f"{what}\n  got:  {got!r}\n  want: {want!r}"


def build(md: str, extra: list[str] | None = None) -> Path:
    """Full CLI build of `md` into a fresh temp dir; returns the site dir."""
    d = Path(tempfile.mkdtemp(prefix="webdoc-mermaid-"))
    src = d / "doc.md"
    src.write_text(md, encoding="utf-8")
    out = d / "site"
    cmd = [sys.executable, str(SCRIPTS / "create_site.py"), str(src),
           "--out", str(out), "--no-lint"]
    if extra:
        cmd += extra
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"build failed: {proc.stderr}"
    return out


# --------------------------------------------------------------------------- #
# parse_markdown emission
# --------------------------------------------------------------------------- #

@test("site mode: mermaid fence emits escaped source in pre.mermaid, sets the flag")
def _():
    md = "# T\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    html, _, state = create_site.parse_markdown(md, mode="site")
    eq(state.get("mermaid"), True, "mermaid flag set in site mode")
    assert '<figure class="mermaid-figure" data-noedit>' in html, html
    assert '<pre class="mermaid">' in html, html
    # `-->` carries a '>' so html.escape turns it into `--&gt;`; the raw arrow
    # must not survive inside the pre.
    body = html.split('<pre class="mermaid">', 1)[1].split("</pre>", 1)[0]
    assert "--&gt;" in body, body
    assert "-->" not in body, "raw arrow leaked unescaped"


@test("mermaid flag defaults False when no mermaid fence is present")
def _():
    html, _, state = create_site.parse_markdown("# T\n\nJust prose.\n", mode="site")
    eq(state.get("mermaid"), False, "flag present and False")
    assert "mermaid" not in html


@test("doc mode: mermaid fence becomes a code block, records a dropped feature, no script")
def _():
    md = "```mermaid\nflowchart TD\n  A --> B\n```\n"
    html, _, state = create_site.parse_markdown(md, mode="doc")
    assert '<pre><code class="language-mermaid">' in html, html
    assert "<script" not in html, "doc export must stay script-free"
    assert 'class="mermaid-figure"' not in html and '<pre class="mermaid">' not in html
    assert any("mermaid diagram" in d for d in state["dropped"]), state["dropped"]
    eq(state.get("mermaid"), False, "doc mode does not set the site render flag")


@test("multiple mermaid diagrams each get their own pre.mermaid")
def _():
    md = "```mermaid\ngraph LR\n  A --> B\n```\n\n```mermaid\ngraph TD\n  C --> D\n```\n"
    html, _, state = create_site.parse_markdown(md, mode="site")
    eq(html.count('<pre class="mermaid">'), 2, "two diagrams -> two pre blocks")
    eq(state.get("mermaid"), True)


@test("special characters are HTML-escaped exactly once")
def _():
    md = '```mermaid\nsequenceDiagram\n  A->>B: <br/> & "q" tags\n```\n'
    html, _, _ = create_site.parse_markdown(md, mode="site")
    body = html.split('<pre class="mermaid">', 1)[1].split("</pre>", 1)[0]
    assert "&lt;br/&gt;" in body, body
    assert "&amp;" in body, body
    assert "&quot;q&quot;" in body, body
    # escaped exactly once: no double-escaped ampersand
    assert "&amp;amp;" not in body and "&amp;lt;" not in body, "double-escaped"


@test("an empty mermaid block still emits pre.mermaid and sets the flag")
def _():
    html, _, state = create_site.parse_markdown("```mermaid\n```\n", mode="site")
    eq(state.get("mermaid"), True)
    assert '<pre class="mermaid"></pre>' in html, html


# --------------------------------------------------------------------------- #
# mermaid-init.js asset
# --------------------------------------------------------------------------- #

@test("mermaid-init.js guards on window.mermaid and initializes strict/neutral")
def _():
    js = (ASSETS / "mermaid-init.js").read_text(encoding="utf-8")
    assert "window.mermaid" in js, "guards on the global"
    assert 'securityLevel: "strict"' in js, "strict security level"
    assert 'theme: "neutral"' in js, "neutral theme"
    assert "startOnLoad: false" in js, "manual render (no startOnLoad)"
    assert "mermaid.render(" in js, "renders each diagram"
    assert "webdoc-mermaid-" in js, "unique per-diagram ids"


# --------------------------------------------------------------------------- #
# Full CLI build wiring
# --------------------------------------------------------------------------- #

@test("build with mermaid copies both assets, links both scripts, doc stays script-free")
def _():
    out = build("# Title\n\nSome prose.\n\n```mermaid\nflowchart TD\n  A --> B\n```\n")
    assert (out / "mermaid.min.js").is_file(), "mermaid library copied"
    assert (out / "mermaid-init.js").is_file(), "mermaid init copied"
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert '<script src="./mermaid.min.js" defer></script>' in idx, idx[-800:]
    assert '<script src="./mermaid-init.js" defer></script>' in idx, idx[-800:]
    # library must load before init (classic defer scripts run in document order)
    assert idx.index("./mermaid.min.js") < idx.index("./mermaid-init.js"), "order"
    doc = (out / "doc.html").read_text(encoding="utf-8")
    assert "<script" not in doc, "doc.html must stay script-free"
    assert "mermaid.min.js" not in doc, "no mermaid asset reference in doc.html"
    assert '<code class="language-mermaid">' in doc, "doc shows the diagram source"


@test("build without mermaid omits the assets and the script tags")
def _():
    out = build("# Title\n\nJust prose, no diagrams.\n")
    assert not (out / "mermaid.min.js").exists(), "no library copied"
    assert not (out / "mermaid-init.js").exists(), "no init copied"
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "./mermaid.min.js" not in idx and "./mermaid-init.js" not in idx, "no script tags"


@test("a build with both stepper and mermaid copies both asset sets")
def _():
    md = (
        "# Title\n\n"
        '```stepper title="Walk"\nstep one\n---\nstep two\n```\n\n'
        "```mermaid\nflowchart TD\n  A --> B\n```\n"
    )
    out = build(md)
    assert (out / "stepper.js").is_file(), "stepper asset copied"
    assert (out / "mermaid.min.js").is_file(), "mermaid library copied"
    assert (out / "mermaid-init.js").is_file(), "mermaid init copied"
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "./stepper.js" in idx and "./mermaid.min.js" in idx and "./mermaid-init.js" in idx


@test("rebuild_html restores a deleted mermaid asset and keeps the script tags")
def _():
    out = build("# Title\n\nProse.\n\n```mermaid\nflowchart TD\n  A --> B\n```\n")
    # the source path is recorded in the manifest; read it back to drive a rebuild
    import json
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    src = manifest["source_path"]
    (out / "mermaid-init.js").unlink()
    assert not (out / "mermaid-init.js").exists(), "precondition: asset deleted"
    ok = create_site.rebuild_html(src, out)
    eq(ok, True, "rebuild returns True")
    assert (out / "mermaid-init.js").is_file(), "rebuild restored the missing asset"
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert './mermaid.min.js' in idx and './mermaid-init.js' in idx, "script tags survive rebuild"


def main() -> int:
    print(f"mermaid suite  ({len(TESTS)} tests)")
    print(f"  modules: {SCRIPTS}")
    print("-" * 72)
    passed, failed = [], []
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"FAIL  {name}")
        except Exception:
            failed.append((name, traceback.format_exc()))
            print(f"ERROR {name}")
        else:
            passed.append(name)
            print(f"ok    {name}")
    print("-" * 72)
    if failed:
        print("\nFAILURES:\n")
        for name, detail in failed:
            print(f"### {name}")
            for line in detail.rstrip().splitlines():
                print(f"    {line}")
            print()
    print(f"summary: {len(passed)} passed, {len(failed)} failed, {len(TESTS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
