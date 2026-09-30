#!/usr/bin/env python3
"""Tests for the review-player video embed wiring in create_site.py.

A bare <video> inside a ```embed fence is the whole authoring surface: no new
markdown syntax, no --flag. create_site.py detects it and bundles
review-player.js/css so it becomes the default, scrubbable video embed. These
exercise the importable parser plus a couple of full CLI builds into temp
dirs. Pure stdlib. Run directly:

    python3 tests/test_review_player.py
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
    d = Path(tempfile.mkdtemp(prefix="webdoc-reviewplayer-"))
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


VIDEO_MD = (
    "# Title\n\nSome prose.\n\n"
    '```embed\n<video controls preload="metadata" src="assets/clip.mp4"></video>\n```\n'
)

# --------------------------------------------------------------------------- #
# parse_markdown emission
# --------------------------------------------------------------------------- #

@test("a <video> embed sets the video flag in both site and doc mode")
def _():
    _, _, site_state = create_site.parse_markdown(VIDEO_MD, mode="site")
    eq(site_state.get("video"), True, "site mode sets the flag")
    _, _, doc_state = create_site.parse_markdown(VIDEO_MD, mode="doc")
    eq(doc_state.get("video"), True, "doc mode sets the flag too (build() only bundles for site)")


@test("video flag defaults False when no <video> embed is present")
def _():
    _, _, state = create_site.parse_markdown("# T\n\nJust prose.\n", mode="site")
    eq(state.get("video"), False)


@test("an <audio> embed does not set the video flag")
def _():
    md = "```embed\n<audio controls src=\"assets/vo.mp3\"></audio>\n```\n"
    _, _, state = create_site.parse_markdown(md, mode="site")
    eq(state.get("video"), False, "audio-only embed is not a video")
    assert any("audio" in d for d in state["dropped"])


@test("a <video> embed is still recorded as a dropped doc-export feature")
def _():
    _, _, state = create_site.parse_markdown(VIDEO_MD, mode="doc")
    assert "video" in state["dropped"], state["dropped"]


# --------------------------------------------------------------------------- #
# review-player.js / .css assets
# --------------------------------------------------------------------------- #

@test("review-player.js is dependency-free and enhances every <video> on the page")
def _():
    js = (ASSETS / "review-player.js").read_text(encoding="utf-8")
    assert "querySelectorAll(\"video\")" in js, "scans for every <video>"
    assert "removeAttribute(\"controls\")" in js, "hands off from native controls only after wrapping"
    assert "rp-timestamp" in js, "clickable timestamp links"
    assert "ArrowRight" in js and "ArrowLeft" in js, "5s/1s seek keys"
    assert '","."' not in js  # sanity: no stray artifact from editing
    assert "case \",\":" in js and "case \".\":" in js, "frame-step keys"
    assert "case \"i\":" in js and "case \"o\":" in js and "case \"l\":" in js, "A/B loop keys"
    assert "case \"m\":" in js, "mute key"
    assert "case \"f\":" in js, "fullscreen key"
    assert "1001 / 30000" in js, "29.97fps frame duration"


@test("review-player.js wires -5s/+5s buttons without disturbing the video element identity")
def _():
    js = (ASSETS / "review-player.js").read_text(encoding="utf-8")
    assert "rp-back5" in js and "rp-fwd5" in js, "skip-back/skip-forward buttons"
    assert 'backBtn.textContent = "-5s"' in js
    assert 'fwdBtn.textContent = "+5s"' in js
    assert "seekBy(-SMALL_SEEK)" in js and "seekBy(SMALL_SEEK)" in js, "buttons reuse the existing clamped seekBy"
    assert "SMALL_SEEK = 5" in js
    # The page's review-feedback.js reads video.currentTime directly off the
    # original element; the player must keep reusing it, never replace it.
    assert "video.parentNode.insertBefore(wrap, video)" in js
    assert "surface.appendChild(video)" in js
    assert "document.createElement(\"video\")" not in js, "must never construct a replacement <video>"


@test("review-player.js captures playback metrics and ships them as their own log")
def _():
    js = (ASSETS / "review-player.js").read_text(encoding="utf-8")
    assert "function PlayerMetrics" in js
    assert 'METRICS_ENDPOINT = "/api/metrics"' in js, "own endpoint, never /api/feedback"
    assert "/api/feedback" not in js, "review-player.js must stay ignorant of the feedback endpoint"
    for evt in (
        "loadstart", "loadedmetadata", "loadeddata", "canplay", "canplaythrough",
        "playing", "waiting", "stalled", "suspend", "seeking", "seeked",
        "pause", "ended", "error", "emptied", "ratechange",
    ):
        assert f'"{evt}"' in js, f"missing capture for {evt}"
    assert "navigator.sendBeacon" in js, "primary delivery path"
    assert "keepalive: true" in js, "fetch fallback survives page unload too"
    assert "rebufferCount" in js and "rebufferMs" in js, "rebuffer count and duration"
    assert "hasStartedPlayback" in js, "pre-roll wait must not double-count as a rebuffer"
    assert "lastSeekLatencyMs" in js, "seek-to-playing latency"
    assert "throughputKbps" in js, "throughput estimate from Resource Timing"
    assert "pagehide" in js and "visibilitychange" in js, "flush triggers on tab hide/close"
    assert "rp-stats-toggle" in js and "rp-stats-on" in js
    assert "statsText" in js, "unobtrusive stats readout behind the toggle"


@test("review-player.css defines the dark chrome and active-player outline")
def _():
    css = (ASSETS / "review-player.css").read_text(encoding="utf-8")
    assert ".review-player" in css
    assert ".rp-scrub" in css, "scrub bar"
    assert ".rp-legend" in css, "shortcut legend"
    assert "rp-active" in css, "active-player highlight for multi-player pages"


@test("review-player.css gives -5s/+5s buttons an Apple-HIG-sized touch target and styles the stats panel")
def _():
    css = (ASSETS / "review-player.css").read_text(encoding="utf-8")
    assert ".rp-skip" in css
    assert "min-height: 44px" in css, "44px is Apple's minimum recommended tap size"
    assert ".rp-stats" in css, "playback stats readout panel"
    assert ".rp-stats-toggle.rp-stats-on" in css, "toggle shows an active state"


# --------------------------------------------------------------------------- #
# Full CLI build wiring
# --------------------------------------------------------------------------- #

@test("build with a <video> embed copies both player assets and links them")
def _():
    out = build(VIDEO_MD)
    assert (out / "review-player.js").is_file(), "player script copied"
    assert (out / "review-player.css").is_file(), "player stylesheet copied"
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert '<script src="./review-player.js" defer></script>' in idx, idx[-800:]
    assert '<link rel="stylesheet" href="./review-player.css">' in idx, idx[:2000]
    # the raw <video> tag itself must survive untouched in the markup (graceful
    # degradation: native controls if the script never runs)
    assert '<video controls preload="metadata" src="assets/clip.mp4"></video>' in idx
    doc = (out / "doc.html").read_text(encoding="utf-8")
    assert "<script" not in doc, "doc.html must stay script-free"
    assert "review-player" not in doc, "no player asset reference in doc.html"


@test("build without a <video> embed omits the player assets and tags")
def _():
    out = build("# Title\n\nJust prose, no video.\n")
    assert not (out / "review-player.js").exists()
    assert not (out / "review-player.css").exists()
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "review-player" not in idx


@test("an <audio>-only embed does not pull in the video player")
def _():
    out = build("```embed\n<audio controls src=\"assets/vo.mp3\"></audio>\n```\n")
    assert not (out / "review-player.js").exists()
    assert not (out / "review-player.css").exists()


@test("rebuild_html restores a deleted player asset and keeps the script/link tags")
def _():
    out = build(VIDEO_MD)
    import json
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    src = manifest["source_path"]
    (out / "review-player.js").unlink()
    assert not (out / "review-player.js").exists(), "precondition: asset deleted"
    ok = create_site.rebuild_html(src, out)
    eq(ok, True, "rebuild returns True")
    assert (out / "review-player.js").is_file(), "rebuild restored the missing asset"
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert './review-player.js' in idx and './review-player.css' in idx, "tags survive rebuild"


@test("a build with both video and mermaid copies both asset sets")
def _():
    md = VIDEO_MD + "\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    out = build(md)
    assert (out / "review-player.js").is_file()
    assert (out / "mermaid.min.js").is_file()
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "./review-player.js" in idx and "./mermaid.min.js" in idx


def main() -> int:
    print(f"review-player suite  ({len(TESTS)} tests)")
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
