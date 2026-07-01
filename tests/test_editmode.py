#!/usr/bin/env python3
"""Test suite for webdoc editing mode (the source round-trip + HTML->md).

Unlike test_lint_prose.py (a CLI-contract suite), these exercise the importable
modules directly, since that is the stable surface the server and any future
agent pass call:

  * html2md.html_to_markdown      - the strict whitelist converter
  * edit_support.apply_edit       - validate -> hash-check -> write -> ledger
  * edit_support.check_overrides  - the anti-clobber contract
  * create_site.inline_md         - that escaped specials round-trip cleanly

Pure stdlib. Run directly:

    python3 tests/test_editmode.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import create_site  # noqa: E402
import edit_support  # noqa: E402
from html2md import html_to_markdown  # noqa: E402

block_hash = create_site.block_hash

TESTS: list[tuple[str, "callable"]] = []


def test(name: str):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def eq(got, want, what=""):
    assert got == want, f"{what}\n  got:  {got!r}\n  want: {want!r}"


def write_md(text: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="webdoc-edit-test-"))
    src = d / "doc.md"
    src.write_text(text, encoding="utf-8")
    return src


def slice_hash(text: str, start: int, end: int) -> str:
    """Hash of lines[start-1:end], the way create_site emits data-md-hash."""
    lines = text.splitlines()
    return block_hash("\n".join(lines[start - 1:end]))


# --------------------------------------------------------------------------- #
# html2md whitelist
# --------------------------------------------------------------------------- #

@test("html2md: strong/b -> **, em/i -> *")
def _():
    eq(html_to_markdown("<strong>a</strong>"), "**a**", "strong")
    eq(html_to_markdown("<b>a</b>"), "**a**", "b")
    eq(html_to_markdown("<em>a</em>"), "*a*", "em")
    eq(html_to_markdown("<i>a</i>"), "*a*", "i")


@test("html2md: s/del/strike -> ~~ (strikethrough), nests + collapses")
def _():
    eq(html_to_markdown("<s>a</s>"), "~~a~~", "s")
    eq(html_to_markdown("<del>a</del>"), "~~a~~", "del (re-rendered form)")
    eq(html_to_markdown("<strike>a</strike>"), "~~a~~", "strike")
    eq(html_to_markdown("a <s>b</s> c"), "a ~~b~~ c", "inline")
    eq(html_to_markdown("<strong>x <s>y</s></strong>"), "**x ~~y~~**", "nested in bold")
    eq(html_to_markdown("<s><del>x</del></s>"), "~~x~~", "identical marks collapse")


@test("strikethrough: inline_md renders <del>, full md<->html cycle is stable")
def _():
    eq(create_site.inline_md("~~x~~"), "<del>x</del>", "render")
    eq(create_site.inline_md("a ~~b~~ c"), "a <del>b</del> c", "inline render")
    for md in ["~~struck~~", "**bold ~~x~~**", "before ~~mid~~ after", "~~a~~ ~~b~~"]:
        eq(html_to_markdown(create_site.inline_md(md)), md, "round-trip stable: " + md)
    # A literal "~~" typed as text is escaped on save and renders literally,
    # never re-parsing as strikethrough.
    typed = html_to_markdown("~~not struck~~")
    eq(typed, "\\~\\~not struck\\~\\~", "literal ~~ escaped on save")
    eq(create_site.inline_md(typed), "~~not struck~~", "and renders literally, not struck")


@test("inline_md: marks render inside link text; no NUL leak; strike-in-link stable")
def _():
    inl = create_site.inline_md
    # P2: emphasis/strike/code inside a link label render (not shown literally)
    eq("<strong>b</strong>" in inl("[**b**](http://x)"), True, "bold in link text")
    eq("<del>s</del>" in inl("[~~s~~](http://x)"), True, "strike in link text")
    eq("<code>c</code>" in inl("[`c`](http://x)"), True, "code in link text")
    # P1: a Markdown-special char in link text must not leak a NUL placeholder
    for md in ["[a\\*b](http://x)", "[u \\`q\\`](http://x)", "[a\\~\\~b](http://x)"]:
        eq("\x00" in inl(md), False, "NUL leaked for " + md)
    # strike inside a link round-trips stably over two save/render cycles
    md1 = html_to_markdown('<a href="http://x"><s>t</s></a>')
    eq(md1, "[~~t~~](http://x)", "strike-in-link save1")
    md2 = html_to_markdown(create_site.inline_md(md1))
    eq(md2, md1, "strike-in-link stable on re-save")
    eq("\x00" in create_site.inline_md(md2), False, "no NUL on re-render")


@test("NUL sentinel: html2md strips it; inline_md drops a stray marker, no crash")
def _():
    # html2md must never emit the placeholder sentinel into the .md.
    eq(html_to_markdown("a\x00b"), "ab", "NUL stripped from text")
    eq(html_to_markdown("<code>a\x00b</code>"), "`ab`", "NUL stripped inside code")
    # A crafted/stray \x00N\x00 marker in source must not raise or leak a NUL.
    eq(create_site.inline_md("\x0099\x00"), "", "out-of-range marker dropped")
    eq("\x00" in create_site.inline_md("ok \x007\x00 done"), False, "no NUL leak")


@test("html2md: code is literal (no escaping, no nested marks)")
def _():
    eq(html_to_markdown("<code>x</code>"), "`x`")
    eq(html_to_markdown("<code>**not bold**</code>"), "`**not bold**`")
    eq(html_to_markdown("<code>a\\*b</code>"), "`a\\*b`")  # backslash kept literal


@test("html2md: link with href -> [text](href); no href -> text only")
def _():
    eq(html_to_markdown('<a href="http://x">t</a>'), "[t](http://x)")
    eq(html_to_markdown("<a>t</a>"), "t")


@test("html2md: br -> newline")
def _():
    eq(html_to_markdown("a<br>b"), "a\nb")


@test("html2md: disallowed tags contribute text content only")
def _():
    eq(html_to_markdown('<span style="color:red">hi</span>'), "hi")
    # Block-level elements (div/p) emit a word boundary so neighbouring words
    # stay separated (see the Enter/<div> boundary test below); inline spans do not.
    eq(html_to_markdown("<div>one<p>two</p></div>"), "one two")
    # the tag itself must never survive into the markdown
    out = html_to_markdown("<script>alert(1)</script>ok")
    assert "<" not in out and "script" not in out.replace("alert", ""), out


@test("html2md: nested identical marks collapse")
def _():
    eq(html_to_markdown("<strong><strong>x</strong></strong>"), "**x**")
    eq(html_to_markdown("<em><i>x</i></em>"), "*x*")


@test("html2md: mixed nested marks")
def _():
    eq(html_to_markdown("<strong>a<em>b</em>c</strong>"), "**a*b*c**")


@test("html2md: plain-text specials are backslash-escaped")
def _():
    eq(html_to_markdown("a*b"), "a\\*b")
    eq(html_to_markdown("see [note]"), "see \\[note\\]")
    eq(html_to_markdown("back`tick"), "back\\`tick")


@test("html2md: total - never throws on garbage")
def _():
    for junk in ["<<>></b>< a", "<a href=", "</strong>", "<b><i></b></i>", "", "&amp;<&>"]:
        out = html_to_markdown(junk)
        assert isinstance(out, str), junk


@test("html2md/inline_md round-trip: escaped specials render as bare chars")
def _():
    # The whole point of escaping: a literal '*' the user typed must not become
    # emphasis on the next render, and must not show a backslash.
    md = html_to_markdown("a*b and [x]")
    eq(create_site.inline_md(md), "a*b and [x]")


# --------------------------------------------------------------------------- #
# Line-range replace + hash + 409
# --------------------------------------------------------------------------- #

@test("apply_edit: paragraph replace collapses multi-line block, shifts range")
def _():
    text = "First line.\nstill same paragraph.\n\nSecond para.\n"
    src = write_md(text)
    payload = {
        "type": "paragraph", "start": 1, "end": 2,
        "hash": slice_hash(text, 1, 2),
        "html": "<strong>New</strong> body.",
    }
    status, body = edit_support.apply_edit(src, payload)
    eq(status, 200, "status")
    eq(body["new_html"], "<strong>New</strong> body.", "new_html")
    eq(body["new_start"], 1, "new_start")
    eq(body["new_end"], 1, "new_end")
    eq(body["line_delta"], -1, "line_delta")  # 2 source lines -> 1
    eq(src.read_text(encoding="utf-8"), "**New** body.\n\nSecond para.\n", "file on disk")
    # returned hash matches a fresh hash of the written block
    eq(body["new_hash"], slice_hash(src.read_text(encoding="utf-8"), 1, 1), "new_hash")


@test("apply_edit: 409 on a stale hash, and nothing is written")
def _():
    text = "Original paragraph.\n"
    src = write_md(text)
    payload = {"type": "paragraph", "start": 1, "end": 1, "hash": "deadbeefdead", "html": "Hacked."}
    status, body = edit_support.apply_edit(src, payload)
    eq(status, 409, "status")
    eq(body["error"], "stale_block", "error")
    eq(src.read_text(encoding="utf-8"), text, "file unchanged")


@test("apply_edit: out-of-range start/end -> 409")
def _():
    src = write_md("One line.\n")
    status, _ = edit_support.apply_edit(
        src, {"type": "paragraph", "start": 5, "end": 9, "hash": "x", "html": "y"})
    eq(status, 409, "out of range")


@test("apply_edit: empty result is rejected (400), file unchanged")
def _():
    text = "Keep me.\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(
        src, {"type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1), "html": "   "})
    eq(status, 400, "status")
    eq(body["error"], "empty_block", "error")
    eq(src.read_text(encoding="utf-8"), text, "unchanged")


# --------------------------------------------------------------------------- #
# Structure-prefix preservation
# --------------------------------------------------------------------------- #

@test("apply_edit: heading keeps its level prefix")
def _():
    text = "## Old title\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(
        src, {"type": "heading", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1),
              "html": "<em>New</em> title"})
    eq(status, 200, "status")
    eq(src.read_text(encoding="utf-8"), "## *New* title\n", "heading prefix kept")


@test("apply_edit: list item keeps its marker + indent")
def _():
    text = "  - old item\n  - second item\n"
    src = write_md(text)
    status, _ = edit_support.apply_edit(
        src, {"type": "listitem", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1),
              "html": "new <strong>x</strong>"})
    eq(status, 200, "status")
    eq(src.read_text(encoding="utf-8"), "  - new **x**\n  - second item\n", "marker+indent kept")


@test("apply_edit: ordered list item keeps its numeric marker")
def _():
    text = "1. first\n2. second\n"
    src = write_md(text)
    status, _ = edit_support.apply_edit(
        src, {"type": "listitem", "start": 2, "end": 2, "hash": slice_hash(text, 2, 2),
              "html": "edited"})
    eq(status, 200, "status")
    eq(src.read_text(encoding="utf-8"), "1. first\n2. edited\n", "ordered marker kept")


# --------------------------------------------------------------------------- #
# Table cell field replace
# --------------------------------------------------------------------------- #

@test("apply_edit: table cell field replace preserves its [+] marker")
def _():
    text = "| A | B |\n| --- | --- |\n| [+] **12 ms** | 380 ms |\n"
    src = write_md(text)
    field = create_site.split_table_row(text.splitlines()[2])[0]  # "[+] **12 ms**"
    payload = {"type": "tablecell", "line": 3, "cell": 0, "hash": block_hash(field),
               "html": "<strong>9 ms</strong>"}
    status, body = edit_support.apply_edit(src, payload)
    eq(status, 200, "status")
    eq(body["new_html"], "<strong>9 ms</strong>", "new_html (text only, marker is the class)")
    eq(body["line_delta"], 0, "cell edit never shifts lines")
    eq(src.read_text(encoding="utf-8"),
       "| A | B |\n| --- | --- |\n| [+] **9 ms** | 380 ms |\n", "marker preserved, other cell intact")


@test("apply_edit: table cell 409 on stale hash")
def _():
    text = "| A | B |\n| --- | --- |\n| x | y |\n"
    src = write_md(text)
    status, _ = edit_support.apply_edit(
        src, {"type": "tablecell", "line": 3, "cell": 1, "hash": "nope", "html": "z"})
    eq(status, 409, "stale cell")
    eq(src.read_text(encoding="utf-8"), text, "unchanged")


@test("apply_edit: table cell can be emptied (cells may be blank)")
def _():
    text = "| A | B |\n| --- | --- |\n| x | y |\n"
    src = write_md(text)
    field = create_site.split_table_row(text.splitlines()[2])[1]  # "y"
    status, _ = edit_support.apply_edit(
        src, {"type": "tablecell", "line": 3, "cell": 1, "hash": block_hash(field), "html": ""})
    eq(status, 200, "empty cell allowed")
    eq(src.read_text(encoding="utf-8"), "| A | B |\n| --- | --- |\n| x |  |\n", "cell emptied")


# --------------------------------------------------------------------------- #
# Ledger + check_overrides (anti-clobber contract)
# --------------------------------------------------------------------------- #

@test("ledger: an edit writes <stem>.edits.json with content_hash + excerpt")
def _():
    text = "A line to edit.\n"
    src = write_md(text)
    edit_support.apply_edit(
        src, {"type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1),
              "html": "Edited line."})
    ledger = edit_support.read_ledger(src)
    eq(len(ledger), 1, "one entry")
    entry = ledger[0]
    eq(entry["type"], "paragraph", "type")
    assert entry["content_hash"] and entry["excerpt"] and entry["edited_at"], entry


@test("check_overrides: True for the edited block's text, False otherwise")
def _():
    text = "Human will fix this.\n"
    src = write_md(text)
    edit_support.apply_edit(
        src, {"type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1),
              "html": "Human fixed this."})
    new_block = src.read_text(encoding="utf-8").splitlines()[0]
    assert edit_support.check_overrides(src, new_block) is True, "edited block recognised"
    assert edit_support.check_overrides(src, "Some unrelated text.") is False, "unknown block"


@test("ledger: re-editing the same range upserts (latest hash wins, no dup)")
def _():
    text = "v1 text here.\n"
    src = write_md(text)
    edit_support.apply_edit(
        src, {"type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1), "html": "v2 text."})
    t2 = src.read_text(encoding="utf-8")
    edit_support.apply_edit(
        src, {"type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(t2, 1, 1), "html": "v3 text."})
    ledger = edit_support.read_ledger(src)
    eq(len(ledger), 1, "single entry after two edits to same range")
    eq(ledger[0]["content_hash"], slice_hash(src.read_text(encoding="utf-8"), 1, 1), "latest hash")


# --------------------------------------------------------------------------- #
# create_site attribute emission (site vs doc)
# --------------------------------------------------------------------------- #

@test("create_site: site mode emits data-md-* on editable blocks; doc mode does not")
def _():
    md = "# Title\n\nA paragraph.\n\n- item one\n"
    site_html, _, _ = create_site.parse_markdown(md, mode="site")
    doc_html, _, _ = create_site.parse_markdown(md, mode="doc")
    assert 'data-md-type="paragraph"' in site_html, "paragraph attr in site"
    assert 'data-md-type="heading"' in site_html, "heading attr in site"
    assert 'data-md-type="listitem"' in site_html, "listitem attr in site"
    assert "data-md-" not in doc_html, "no edit attrs leak into doc export"
    assert "data-noedit" not in doc_html, "no noedit attrs in doc export"


@test("create_site: emitted hash matches what apply_edit checks (no drift)")
def _():
    md = "# Title\n\nFirst para line.\nSecond para line.\n"
    site_html, _, _ = create_site.parse_markdown(md, mode="site")
    # pull the paragraph's emitted attributes back out
    import re
    m = re.search(r'<p data-md-start="(\d+)" data-md-end="(\d+)" data-md-hash="([0-9a-f]+)"', site_html)
    assert m, site_html
    start, end, emitted = int(m.group(1)), int(m.group(2)), m.group(3)
    eq(emitted, slice_hash(md, start, end), "emitted hash == server-side slice hash")


# --------------------------------------------------------------------------- #
# Round-1 review blockers (regression tests)
# --------------------------------------------------------------------------- #

@test("blocker #1: a pipe typed into a table cell round-trips as one cell")
def _():
    text = "| A | B |\n| --- | --- |\n| fast | 380 ms |\n"
    src = write_md(text)
    field = create_site.split_table_row(text.splitlines()[2])[0]  # "fast"
    status, body = edit_support.apply_edit(
        src, {"type": "tablecell", "line": 3, "cell": 0,
              "hash": block_hash(field), "html": "a | b"})
    eq(status, 200, "status")
    row = src.read_text(encoding="utf-8").splitlines()[2]
    cells = create_site.split_table_row(row)
    eq(len(cells), 2, f"still two cells, got {cells!r}")
    eq(cells[0], "a | b", "edited cell holds the literal pipe")
    eq(cells[1], "380 ms", "sibling cell intact")
    assert "\\|" in row, f"pipe is escaped in the source row: {row!r}"
    eq(body["new_html"], "a | b", "re-render shows the pipe literally")
    eq(body["new_hash"], block_hash(cells[0]), "hash is over the unescaped field")


@test("blocker #1: editing a sibling cell keeps an existing escaped pipe intact")
def _():
    # Row already carries an escaped pipe in cell 0; editing cell 1 must not
    # split or corrupt cell 0.
    text = "| A | B |\n| --- | --- |\n| a \\| b | y |\n"
    src = write_md(text)
    field1 = create_site.split_table_row(text.splitlines()[2])[1]  # "y"
    eq(create_site.split_table_row(text.splitlines()[2]), ["a | b", "y"], "pre: cell0 has a literal pipe")
    status, _ = edit_support.apply_edit(
        src, {"type": "tablecell", "line": 3, "cell": 1,
              "hash": block_hash(field1), "html": "z"})
    eq(status, 200, "status")
    cells = create_site.split_table_row(src.read_text(encoding="utf-8").splitlines()[2])
    eq(cells, ["a | b", "z"], "sibling pipe survives the edit")


@test("blocker #2: safe_href neutralises control-char scheme obfuscation to #")
def _():
    eq(create_site.safe_href("javascript:alert(1)"), "#", "plain")
    eq(create_site.safe_href("java\tscript:alert(1)"), "#", "interior tab")
    eq(create_site.safe_href("\x01javascript:alert(1)"), "#", "leading control char")
    eq(create_site.safe_href("  JaVaScRiPt:alert(1)"), "#", "case + space")
    eq(create_site.safe_href("data:text/html;base64,AAAA"), "#", "data:")
    eq(create_site.safe_href("vbscript:msgbox"), "#", "vbscript:")
    eq(create_site.safe_href("https://example.test/x"), "https://example.test/x", "https passes")
    eq(create_site.safe_href("/relative/path"), "/relative/path", "relative passes")


@test("blocker #2: a control-char javascript href is neutralised through the renderer")
def _():
    # The shared safe_href protects the normal renderer too: a stored
    # "java\tscript:" link must not render as a live href.
    out = create_site.inline_md('[x](java\tscript:alert(1))')
    assert 'href="#"' in out, out
    assert "javascript" not in out.lower(), out


@test("blocker #2b: a disallowed-scheme link is dropped to plain text in html2md")
def _():
    eq(html_to_markdown('<a href="javascript:alert(1)">click</a>'), "click")
    eq(html_to_markdown('<a href="java\tscript:alert(1)">x</a>'), "x", "control-char obfuscation")
    eq(html_to_markdown('<a href="data:text/html;base64,AAAA">y</a>'), "y")
    eq(html_to_markdown('<a href="vbscript:msgbox">z</a>'), "z")
    eq(html_to_markdown('<a href="https://ok.test">k</a>'), "[k](https://ok.test)", "normal link survives")


@test("blocker #4: typed backticks in prose round-trip and render literally")
def _():
    for typed, want in [
        ("press `Cmd`", "press `Cmd`"),                              # single
        ("press `Cmd` then `Enter`", "press `Cmd` then `Enter`"),    # multiple
    ]:
        text = "Old body.\n"
        src = write_md(text)
        status, body = edit_support.apply_edit(
            src, {"type": "paragraph", "start": 1, "end": 1,
                  "hash": slice_hash(text, 1, 1), "html": typed})
        eq(status, 200, f"status for {typed!r}")
        eq(body["new_html"], want, f"renders literally: {typed!r}")
        assert "<code>" not in body["new_html"], f"no code span for {typed!r}: {body['new_html']!r}"


@test("blocker #4: a real code span still renders as <code>")
def _():
    text = "Old body.\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(
        src, {"type": "paragraph", "start": 1, "end": 1,
              "hash": slice_hash(text, 1, 1), "html": "use <code>git</code> now"})
    eq(status, 200, "status")
    eq(body["new_html"], "use <code>git</code> now", "code span survives")
    eq(src.read_text(encoding="utf-8"), "use `git` now\n", "stored as a backtick span")


@test("blocker #4: backtick-bearing code content widens the fence and round-trips")
def _():
    eq(html_to_markdown("<code>a`b</code>"), "``a`b``", "fence widened")
    eq(create_site.inline_md("``a`b``"), "<code>a`b</code>", "renders the inner backtick")
    # leading backtick in content gets a CommonMark pad that the renderer strips
    eq(html_to_markdown("<code>`x</code>"), "`` `x ``", "padded fence")
    eq(create_site.inline_md("`` `x ``"), "<code>`x</code>", "pad stripped on render")
    # a plain single-backtick span is unchanged (no space stripping)
    eq(create_site.inline_md("` x `"), "<code> x </code>", "single-backtick content verbatim")


@test("blocker #5: a leading block marker stays a paragraph (all four kinds)")
def _():
    import re as _re
    for kind, typed in [
        ("list", "- not a list"),
        ("heading", "## not a heading"),
        ("quote", "> not a quote"),
        ("ordered", "1. not ordered"),
    ]:
        text = "Plain paragraph body.\n"
        src = write_md(text)
        status, body = edit_support.apply_edit(
            src, {"type": "paragraph", "start": 1, "end": 1,
                  "hash": slice_hash(text, 1, 1), "html": typed})
        eq(status, 200, f"{kind}: status")
        written = src.read_text(encoding="utf-8")
        assert written.splitlines()[0].startswith("\\"), f"{kind}: marker escaped on write: {written!r}"
        # A fresh full rebuild must classify it as a paragraph, never promote it.
        site_html, _, _ = create_site.parse_markdown(written, mode="site")
        assert "<p" in site_html, f"{kind}: rebuilt as a paragraph"
        for promoted in ("<ul>", "<ol>", "<h1", "<h2", "<blockquote"):
            assert promoted not in site_html, f"{kind}: must not promote to {promoted}\n{site_html}"
        # per-block re-render == the paragraph's inner HTML on a full rebuild
        m = _re.search(r"<p\b[^>]*>(.*?)</p>", site_html, _re.DOTALL)
        assert m, f"{kind}: paragraph in rebuild:\n{site_html}"
        eq(m.group(1), body["new_html"], f"{kind}: per-block render == full rebuild")
        assert "\\" not in body["new_html"], f"{kind}: no backslash shown: {body['new_html']!r}"


@test("blocker #6: Enter-inserted <div> and &nbsp; keep words separated")
def _():
    eq(html_to_markdown("A.<div>B.</div>"), "A. B.", "block boundary")
    eq(html_to_markdown("one&nbsp;two"), "one two", "&nbsp; entity -> space")
    eq(html_to_markdown("a b"), "a b", "U+00A0 -> space")
    # a single edited paragraph with an Enter stays one logical line via flatten
    eq(edit_support.flatten(html_to_markdown("A.<div>B.</div>")), "A. B.", "one logical line")


@test("blocker (nit): render_embed is wrapped data-noedit (view-only) in the site")
def _():
    state = {"dropped": set(), "stepper": False, "embed": False}
    out = create_site.render_embed("<div>live</div>", "site", state)
    assert "data-noedit" in out, out
    assert "<div>live</div>" in out, "embed body preserved"
    doc = create_site.render_embed("<div>live</div>", "doc", {"dropped": set()})
    assert "data-noedit" not in doc and "omitted" in doc, doc


@test("doc export stays clean: no edit chrome with code/blockquote/stepper/embed")
def _():
    import subprocess
    md = (
        "# Title\n\n"
        "A paragraph.\n\n"
        "> a blockquote\n\n"
        "```python\nprint('hi')\n```\n\n"
        '```stepper title="Walk"\nstep one\n---\nstep two\n```\n\n'
        '```embed\n<div class="x">live</div>\n```\n'
    )
    d = Path(tempfile.mkdtemp(prefix="webdoc-doc-clean-"))
    src = d / "doc.md"
    src.write_text(md, encoding="utf-8")
    out = d / "site"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "create_site.py"), str(src), "--out", str(out), "--no-lint"],
        capture_output=True, text=True)
    eq(proc.returncode, 0, f"build ok (stderr: {proc.stderr})")
    doc = (out / "doc.html").read_text(encoding="utf-8")
    for needle in ("data-md-", "data-noedit", "edit.js", "edit.css"):
        assert needle not in doc, f"{needle!r} leaked into doc.html"
    # the interactive site DOES carry edit chrome (identity + view-only marks)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "data-md-type" in index, "site emits block identity"
    assert "data-noedit" in index, "site marks embed/stepper/code view-only"


@test("blocker (nit): line endings are preserved (CRLF stays CRLF)")
def _():
    text = "First line.\r\nSecond line.\r\n"
    src = write_md(text)
    status, _ = edit_support.apply_edit(
        src, {"type": "paragraph", "start": 1, "end": 1,
              "hash": slice_hash(text, 1, 1), "html": "Edited first."})
    eq(status, 200, "status")
    raw = src.read_bytes()
    assert b"\r\n" in raw, f"CRLF preserved: {raw!r}"
    assert b"\nEdited" not in raw and b"Edited first.\r\n" in raw, f"edited line uses CRLF: {raw!r}"


# --------------------------------------------------------------------------- #
# Block delete (op="delete") + session undo
# --------------------------------------------------------------------------- #

@test("delete: removes a range block, shifts the rest, returns negative delta")
def _():
    text = "Para one.\n\nPara two.\n\nPara three.\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 3, "end": 3,
        "hash": slice_hash(text, 3, 3)})
    eq(status, 200, "status")
    eq(body["op"], "delete", "op echoed")
    eq(body["line_delta"], -1, "one line removed")
    eq(src.read_text(encoding="utf-8"), "Para one.\n\n\nPara three.\n", "middle block gone")


@test("delete: 409 on a stale hash, nothing removed")
def _():
    text = "Keep me.\n\nDelete target.\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 3, "end": 3, "hash": "deadbeef"})
    eq(status, 409, "status")
    eq(body["error"], "stale_block", "error")
    eq(src.read_text(encoding="utf-8"), text, "unchanged")


@test("delete: a table cell cannot be deleted (bad_type), file unchanged")
def _():
    text = "| A | B |\n| --- | --- |\n| x | y |\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "op": "delete", "type": "tablecell", "line": 3, "cell": 0, "hash": "x"})
    eq(status, 400, "status")
    eq(body["error"], "bad_type", "error")
    eq(src.read_text(encoding="utf-8"), text, "unchanged")


@test("delete: drops the deleted block's ledger entry")
def _():
    text = "Edit me.\n\nKeep me.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1), "html": "Edited."})
    eq(len(edit_support.read_ledger(src)), 1, "one ledger entry after the edit")
    t1 = src.read_text(encoding="utf-8")
    edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(t1, 1, 1)})
    eq(edit_support.read_ledger(src), [], "ledger entry for the deleted block is dropped")


@test("undo: restores a deleted block verbatim and re-shifts ranges")
def _():
    text = "Para one.\n\nPara two.\n\nPara three.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 3, "end": 3, "hash": slice_hash(text, 3, 3)})
    status, body = edit_support.apply_undo(src)
    eq(status, 200, "status")
    eq(body["label"], "delete", "label")
    eq(body["line_delta"], 1, "one line restored")
    eq(body["new_start"], 3, "restored at its original start")
    eq(body["new_end"], 3, "single line")
    eq(src.read_text(encoding="utf-8"), text, "file restored exactly")
    eq(body["new_hash"], slice_hash(text, 3, 3), "hash matches the restored block")


@test("undo: reverses a text edit (content, range, and added line restored)")
def _():
    text = "First line.\nstill same paragraph.\n\nSecond para.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "type": "paragraph", "start": 1, "end": 2, "hash": slice_hash(text, 1, 2),
        "html": "<strong>New</strong> body."})
    status, body = edit_support.apply_undo(src)
    eq(status, 200, "status")
    eq(body["label"], "edit", "label")
    eq(src.read_text(encoding="utf-8"), text, "the two-line paragraph is restored")
    eq(body["new_start"], 1, "new_start")
    eq(body["new_end"], 2, "new_end (two lines back)")
    eq(body["line_delta"], 1, "the collapsed line is restored (1 -> 2)")


@test("undo: nothing to undo -> 400")
def _():
    src = write_md("Just one paragraph.\n")
    status, body = edit_support.apply_undo(src)
    eq(status, 400, "status")
    eq(body["error"], "nothing_to_undo", "error")


@test("undo: LIFO - reverses the delete first, then the earlier edit")
def _():
    text = "Alpha.\n\nBravo.\n\nCharlie.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1), "html": "Alpha edited."})
    t1 = src.read_text(encoding="utf-8")
    edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 5, "end": 5, "hash": slice_hash(t1, 5, 5)})
    s1, b1 = edit_support.apply_undo(src)
    eq(s1, 200, "undo1 status")
    eq(b1["label"], "delete", "undo1 reverses the delete (newest)")
    eq(src.read_text(encoding="utf-8"), t1, "back to the post-edit state")
    s2, b2 = edit_support.apply_undo(src)
    eq(s2, 200, "undo2 status")
    eq(b2["label"], "edit", "undo2 reverses the earlier edit")
    eq(src.read_text(encoding="utf-8"), text, "back to the original")
    s3, _ = edit_support.apply_undo(src)
    eq(s3, 400, "undo3: nothing left")


@test("undo: reverses a table cell edit (cell hash + html recomputed)")
def _():
    text = "| A | B |\n| --- | --- |\n| x | y |\n"
    src = write_md(text)
    field = create_site.split_table_row(text.splitlines()[2])[0]  # "x"
    edit_support.apply_edit(src, {
        "type": "tablecell", "line": 3, "cell": 0, "hash": block_hash(field), "html": "z"})
    eq(src.read_text(encoding="utf-8"), "| A | B |\n| --- | --- |\n| z | y |\n", "cell edited")
    status, body = edit_support.apply_undo(src)
    eq(status, 200, "status")
    eq(body["label"], "cell", "label")
    eq(body["line"], 3, "line")
    eq(body["cell"], 0, "cell")
    eq(body["line_delta"], 0, "a cell undo never shifts lines")
    eq(src.read_text(encoding="utf-8"), text, "the row is restored")
    restored = create_site.split_table_row(src.read_text(encoding="utf-8").splitlines()[2])[0]
    eq(body["new_hash"], block_hash(restored), "hash matches the restored cell")
    eq(body["new_html"], "x", "restored cell html")


@test("undo: refuses and drops the stack when the source drifted (409)")
def _():
    text = "Alpha.\n\nBravo.\n\nCharlie.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 5, "end": 5, "hash": slice_hash(text, 5, 5)})
    # The source shrinks under the stack (fewer lines than the stale splice needs).
    src.write_text("x\n", encoding="utf-8")
    status, body = edit_support.apply_undo(src)
    eq(status, 409, "status")
    eq(body["error"], "undo_desynced", "error")
    s2, b2 = edit_support.apply_undo(src)
    eq(s2, 400, "the stack was dropped, nothing left to undo")
    eq(b2["error"], "nothing_to_undo", "error")


@test("undo: deleting the only block round-trips to exact bytes (empty doc)")
def _():
    text = "Solo paragraph.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1)})
    eq(src.read_text(encoding="utf-8"), "", "deleting the only block empties the file, not a blank line")
    status, _ = edit_support.apply_undo(src)
    eq(status, 200, "undo status")
    eq(src.read_text(encoding="utf-8"), text, "restored to the exact original bytes")


@test("undo: a same-length out-of-band edit is caught (409 desync, content kept)")
def _():
    text = "AAA\nBBB\nCCC\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "type": "paragraph", "start": 2, "end": 2, "hash": slice_hash(text, 2, 2), "html": "XXX"})
    eq(src.read_text(encoding="utf-8"), "AAA\nXXX\nCCC\n", "edited")
    # An external writer changes the SAME line, SAME length, under the stack: the
    # bounds check would miss this; the content guard must catch it.
    src.write_text("AAA\nZZZ\nCCC\n", encoding="utf-8")
    status, body = edit_support.apply_undo(src)
    eq(status, 409, "content guard catches the drift")
    eq(body["error"], "undo_desynced", "error")
    eq(src.read_text(encoding="utf-8"), "AAA\nZZZ\nCCC\n", "the external content is NOT clobbered")
    s2, _ = edit_support.apply_undo(src)
    eq(s2, 400, "the stack was dropped after the desync")


@test("undo: deleting the only block preserves CRLF on restore (empty-file edge)")
def _():
    text = "Solo CRLF para.\r\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1)})
    eq(src.read_bytes(), b"", "deleting the only block empties the file")
    status, _ = edit_support.apply_undo(src)
    eq(status, 200, "undo status")
    # The file is empty when undo runs, so the CRLF style cannot be re-detected;
    # it must come from the recorded undo entry, not default to LF.
    eq(src.read_bytes(), b"Solo CRLF para.\r\n", "CRLF restored exactly, not flipped to LF")


@test("undo: deleting the last block of a no-trailing-newline file round-trips")
def _():
    text = "A\n\nB"  # two paragraphs, NO final newline (editor/agent-written files)
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 3, "end": 3, "hash": slice_hash(text, 3, 3)})
    # The blank separator must be preserved (not silently dropped to "A\n"), and
    # the file must stay representable so undo can find its splice point.
    eq(src.read_text(encoding="utf-8"), "A\n\n", "blank separator kept after deleting the last block")
    status, _ = edit_support.apply_undo(src)
    eq(status, 200, "undo succeeds (no false 'source changed' 409)")
    eq(src.read_text(encoding="utf-8"), "A\n\nB", "restored to exact bytes, no trailing newline added")


@test("undo: delete-undo rejects an out-of-band line shift (position guard)")
def _():
    text = "L1\nL2\nL3\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "delete", "type": "paragraph", "start": 2, "end": 2, "hash": slice_hash(text, 2, 2)})
    eq(src.read_text(encoding="utf-8"), "L1\nL3\n", "L2 deleted")
    # An external writer prepends a line: the splice point shifts under the stack.
    # remove=0 means no content to compare, so the anchor (the line that should be
    # at the splice point) must catch it.
    src.write_text("NEW\nL1\nL3\n", encoding="utf-8")
    status, body = edit_support.apply_undo(src)
    eq(status, 409, "anchor catches the shift")
    eq(body["error"], "undo_desynced", "error")
    eq(src.read_text(encoding="utf-8"), "NEW\nL1\nL3\n", "block NOT misplaced or clobbered")


@test("undo: an edit then undo preserves CRLF line endings")
def _():
    text = "First line.\r\nSecond line.\r\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1), "html": "Edited first."})
    status, _ = edit_support.apply_undo(src)
    eq(status, 200, "status")
    raw = src.read_bytes()
    assert b"\r\n" in raw and b"\nFirst" not in raw, f"CRLF preserved through undo: {raw!r}"


# --------------------------------------------------------------------------- #
# Spacing (op="space") + undo
# --------------------------------------------------------------------------- #

@test("space: add inserts a blank before the block and shifts it down")
def _():
    text = "Title line.\n\nBody paragraph.\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "op": "space", "type": "paragraph", "start": 3, "end": 3,
        "hash": slice_hash(text, 3, 3), "dir": "add"})
    eq(status, 200, "status")
    eq(body["dir"], "add", "dir")
    eq(body["new_start"], 4, "block shifted down a line")
    eq(body["line_delta"], 1, "one blank added")
    eq(src.read_text(encoding="utf-8"), "Title line.\n\n\nBody paragraph.\n", "extra blank before the block")


@test("space: add then undo restores exact bytes")
def _():
    text = "Title line.\n\nBody paragraph.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "space", "type": "paragraph", "start": 3, "end": 3,
        "hash": slice_hash(text, 3, 3), "dir": "add"})
    status, body = edit_support.apply_undo(src)
    eq(status, 200, "undo status")
    eq(body["label"], "space", "label")
    eq(src.read_text(encoding="utf-8"), text, "restored to exact bytes")


@test("space: remove deletes one extra blank, refuses to remove the separator")
def _():
    text = "Title line.\n\n\nBody paragraph.\n"  # two blanks before the block
    src = write_md(text)
    status, _ = edit_support.apply_edit(src, {
        "op": "space", "type": "paragraph", "start": 4, "end": 4,
        "hash": slice_hash(text, 4, 4), "dir": "remove"})
    eq(status, 200, "remove status")
    eq(src.read_text(encoding="utf-8"), "Title line.\n\nBody paragraph.\n", "one extra blank removed")
    t2 = src.read_text(encoding="utf-8")  # only the separator remains
    status2, body2 = edit_support.apply_edit(src, {
        "op": "space", "type": "paragraph", "start": 3, "end": 3,
        "hash": slice_hash(t2, 3, 3), "dir": "remove"})
    eq(status2, 400, "refuses to remove the separator")
    eq(body2["error"], "no_space", "error")
    eq(src.read_text(encoding="utf-8"), t2, "unchanged when refused")


@test("space: a list item cannot be spaced (bad_type), file unchanged")
def _():
    text = "- one\n- two\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "op": "space", "type": "listitem", "start": 2, "end": 2,
        "hash": slice_hash(text, 2, 2), "dir": "add"})
    eq(status, 400, "status")
    eq(body["error"], "bad_type", "error")
    eq(src.read_text(encoding="utf-8"), text, "unchanged")


@test("create_site: extra blanks render as gaps; the first block's leading blanks count")
def _():
    # one separator blank -> no gap; a second -> one gap.
    md = "First.\n\nSecond.\n\n\nThird.\n"
    site_html, _, _ = create_site.parse_markdown(md, mode="site")
    eq(site_html.count("webdoc-gap"), 1, "one gap for the double blank, none for the single separator")
    # before the first block there is no separator, so every leading blank is a gap.
    lead_html, _, _ = create_site.parse_markdown("\n\n# Title\n\nbody\n", mode="site")
    eq(lead_html.count("webdoc-gap"), 2, "two leading blanks -> two gaps (no separator to discount)")
    # the doc export stays clean.
    doc_html, _, _ = create_site.parse_markdown(md, mode="doc")
    assert "webdoc-gap" not in doc_html, "no gaps leak into the doc export"


@test("space: remove then undo restores the extra blank")
def _():
    text = "Title line.\n\n\nBody paragraph.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "space", "type": "paragraph", "start": 4, "end": 4,
        "hash": slice_hash(text, 4, 4), "dir": "remove"})
    eq(src.read_text(encoding="utf-8"), "Title line.\n\nBody paragraph.\n", "removed")
    status, _ = edit_support.apply_undo(src)
    eq(status, 200, "undo status")
    eq(src.read_text(encoding="utf-8"), text, "extra blank restored")


# --------------------------------------------------------------------------- #
# Reload persistence (create_site.rebuild_html)
# --------------------------------------------------------------------------- #

@test("rebuild_html: an edit is reflected in index.html and its hash matches source")
def _():
    import subprocess
    import re as _re
    d = Path(tempfile.mkdtemp(prefix="webdoc-rebuild-"))
    src = d / "doc.md"
    src.write_text("# Title\n\nOriginal body.\n", encoding="utf-8")
    out = d / "site"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "create_site.py"), str(src), "--out", str(out), "--no-lint"],
        capture_output=True, text=True)
    eq(proc.returncode, 0, f"build ok (stderr: {proc.stderr})")
    assert "Original body." in (out / "index.html").read_text(encoding="utf-8"), "original renders"

    # Edit the paragraph through the server round-trip (writes the source only).
    t = src.read_text(encoding="utf-8")
    status, _ = edit_support.apply_edit(
        src, {"type": "paragraph", "start": 3, "end": 3, "hash": slice_hash(t, 3, 3), "html": "Edited body."})
    eq(status, 200, "edit ok")
    assert "Original body." in (out / "index.html").read_text(encoding="utf-8"), "index.html is stale before rebuild"

    ok = create_site.rebuild_html(src, out)
    eq(ok, True, "rebuild ok")
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "Edited body." in idx and "Original body." not in idx, "index.html now reflects the edit"
    # The rebuilt page's block hash matches the current source, so a reloaded page
    # will not 409 on the next edit.
    m = _re.search(r'<p data-md-start="3" data-md-end="3" data-md-hash="([0-9a-f]+)"', idx)
    assert m, f"paragraph attrs present in rebuilt index.html:\n{idx[:400]}"
    eq(m.group(1), slice_hash(src.read_text(encoding="utf-8"), 3, 3), "rebuilt hash matches the source")


@test("rebuild_html: preserves title, gaps, and custom css/js from the manifest")
def _():
    import subprocess
    import json as _json
    d = Path(tempfile.mkdtemp(prefix="webdoc-rebuild2-"))
    src = d / "doc.md"
    # a double blank -> one gap; a custom css bundled
    src.write_text("# Kept Title\n\nA.\n\n\nB.\n", encoding="utf-8")
    css = d / "extra.css"
    css.write_text("/* x */\n", encoding="utf-8")
    out = d / "site"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "create_site.py"), str(src), "--out", str(out),
         "--title", "Kept Title", "--css", str(css), "--no-lint"],
        capture_output=True, text=True)
    eq(proc.returncode, 0, f"build ok (stderr: {proc.stderr})")
    manifest = _json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    eq(manifest.get("custom_css"), ["extra.css"], "custom css recorded in manifest")

    ok = create_site.rebuild_html(src, out)
    eq(ok, True, "rebuild ok")
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "<title>Kept Title</title>" in idx, "title preserved"
    assert 'href="./extra.css"' in idx, "custom css link preserved on rebuild"
    eq(idx.count("webdoc-gap"), 1, "the authored gap still renders after rebuild")


# --------------------------------------------------------------------------- #
# Retype (op="retype") - the list toolbar button (paragraph <-> list item)
# --------------------------------------------------------------------------- #

@test("retype: paragraph -> list item adds the bullet marker")
def _():
    text = "Plain text.\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "op": "retype", "type": "paragraph", "target": "listitem", "start": 1, "end": 1,
        "hash": slice_hash(text, 1, 1), "html": "Plain text."})
    eq(status, 200, "status")
    eq(body["type"], "listitem", "new type")
    eq(src.read_text(encoding="utf-8"), "- Plain text.\n", "bullet marker added")


@test("retype: list item -> paragraph strips the marker")
def _():
    text = "- Item text.\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "op": "retype", "type": "listitem", "target": "paragraph", "start": 1, "end": 1,
        "hash": slice_hash(text, 1, 1), "html": "Item text."})
    eq(status, 200, "status")
    eq(body["type"], "paragraph", "new type")
    eq(src.read_text(encoding="utf-8"), "Item text.\n", "marker stripped")


@test("retype: list->paragraph blank-separates from adjacent markers (portable)")
def _():
    # first item: a blank AFTER (before the next marker); none needed before.
    text = "1. First.\n2. Second.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "retype", "type": "listitem", "target": "paragraph", "start": 1, "end": 1,
        "hash": slice_hash(text, 1, 1), "html": "First."})
    eq(src.read_text(encoding="utf-8"), "First.\n\n2. Second.\n", "blank after, before the next marker")

    # middle item: a blank on BOTH sides, so a CommonMark tool splits it too
    # (not a lazy continuation of the item above).
    text2 = "- a\n- b\n- c\n"
    src2 = write_md(text2)
    status, body = edit_support.apply_edit(src2, {
        "op": "retype", "type": "listitem", "target": "paragraph", "start": 2, "end": 2,
        "hash": slice_hash(text2, 2, 2), "html": "b"})
    eq(status, 200, "status")
    eq(src2.read_text(encoding="utf-8"), "- a\n\nb\n\n- c\n", "middle split has blanks both sides")
    eq(body["new_start"], 3, "the paragraph shifted below the inserted blank")
    eq(body["line_delta"], 2, "two blank lines added")
    edit_support.apply_undo(src2)
    eq(src2.read_text(encoding="utf-8"), text2, "undo restores the list byte-identical")


@test("retype: single-item list -> paragraph needs no separators")
def _():
    text = "- Item text.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "retype", "type": "listitem", "target": "paragraph", "start": 1, "end": 1,
        "hash": slice_hash(text, 1, 1), "html": "Item text."})
    eq(src.read_text(encoding="utf-8"), "Item text.\n", "no adjacent markers, no blanks added")


@test("retype: same-type or unsupported type is rejected (bad_retype)")
def _():
    text = "Para.\n"
    src = write_md(text)
    for pl in [
        {"type": "paragraph", "target": "paragraph"},
        {"type": "heading", "target": "listitem"},
        {"type": "paragraph", "target": "tablecell"},
    ]:
        status, body = edit_support.apply_edit(src, dict(
            op="retype", start=1, end=1, hash=slice_hash(text, 1, 1), html="Para.", **pl))
        eq(status, 400, f"rejected {pl['type']}->{pl['target']}")
        eq(body["error"], "bad_retype", "error")
    eq(src.read_text(encoding="utf-8"), text, "unchanged")


@test("retype: undo restores the original type and bytes")
def _():
    text = "Convert me.\n"
    src = write_md(text)
    edit_support.apply_edit(src, {
        "op": "retype", "type": "paragraph", "target": "listitem", "start": 1, "end": 1,
        "hash": slice_hash(text, 1, 1), "html": "Convert me."})
    eq(src.read_text(encoding="utf-8"), "- Convert me.\n", "converted to a list item")
    status, body = edit_support.apply_undo(src)
    eq(status, 200, "undo status")
    eq(body["label"], "retype", "undo carries the retype label")
    eq(src.read_text(encoding="utf-8"), text, "restored to the paragraph")


@test("edit: emptying a block is rejected with a message pointing to Delete")
def _():
    text = "Keep me.\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "type": "paragraph", "start": 1, "end": 1, "hash": slice_hash(text, 1, 1), "html": "  "})
    eq(status, 400, "status")
    eq(body["error"], "empty_block", "error")
    assert "Delete" in body.get("message", ""), f"message guides to Delete: {body.get('message')!r}"


# --------------------------------------------------------------------------- #
# Diagram-label editing (op="svgtext" + SVG <text> stamping)
# --------------------------------------------------------------------------- #

@test("svgtext: create_site stamps simple <text> labels in site mode, not doc")
def _():
    import re as _re
    md = "# T\n\n```embed\n<svg><text x=\"1\">Alpha</text><text x=\"2\">Beta</text></svg>\n```\n"
    site, _, _ = create_site.parse_markdown(md, mode="site")
    doc, _, _ = create_site.parse_markdown(md, mode="doc")
    eq(site.count("data-md-svgtext="), 2, "both simple labels stamped in site mode")
    assert "data-md-svgtext" not in doc, "no stamp leaks into the doc export"
    idxs = _re.findall(r'data-md-svgtext="[^"]*:(\d+)"', site)
    eq(idxs, ["0", "1"], "label indices follow source order")
    # a <text> with child elements (a tspan) is NOT stamped (not editable)
    md2 = "# T\n\n```embed\n<svg><text><tspan>x</tspan></text></svg>\n```\n"
    site2, _, _ = create_site.parse_markdown(md2, mode="site")
    assert "data-md-svgtext" not in site2, "a <text> with children is left view-only"


@test("svgtext: edits the Nth label, XML-escapes, hash-checks, and undoes")
def _():
    text = "# T\n\n```embed\n<svg><text x=\"1\">Alpha</text><text x=\"2\">Beta</text></svg>\n```\n"
    src = write_md(text)
    status, body = edit_support.apply_edit(src, {
        "op": "svgtext", "loc": "4:4:0", "hash": create_site.block_hash("Alpha"), "text": "Gamma & <b>"})
    eq(status, 200, "status")
    eq(body["new_text"], "Gamma & <b>", "returns the plain text for the client")
    written = src.read_text(encoding="utf-8")
    assert "Gamma &amp; &lt;b&gt;" in written, f"escaped into the SVG source: {written!r}"
    assert "Alpha" not in written and "Beta" in written, "only the first label changed"
    status2, b2 = edit_support.apply_undo(src)
    eq(status2, 200, "undo status")
    eq(b2["label"], "svgtext", "undo label")
    eq(src.read_text(encoding="utf-8"), text, "restored to exact bytes")


@test("svgtext: bad loc, stale hash, out-of-range index, empty label are rejected")
def _():
    text = "# T\n\n```embed\n<svg><text>One</text></svg>\n```\n"
    src = write_md(text)
    eq(edit_support.apply_edit(src, {"op": "svgtext", "loc": "nope", "hash": "x", "text": "y"})[0], 400, "bad_loc")
    eq(edit_support.apply_edit(src, {"op": "svgtext", "loc": "4:4:0", "hash": "wrong", "text": "y"})[0], 409, "stale hash")
    eq(edit_support.apply_edit(src, {"op": "svgtext", "loc": "4:4:9", "hash": "x", "text": "y"})[0], 409, "index out of range")
    st, bd = edit_support.apply_edit(src, {
        "op": "svgtext", "loc": "4:4:0", "hash": create_site.block_hash("One"), "text": "   "})
    eq(st, 400, "empty label")
    eq(bd["error"], "empty_label", "error")
    eq(src.read_text(encoding="utf-8"), text, "file unchanged throughout")


# --------------------------------------------------------------------------- #
# Table row / column delete (op="rowdelete" / "coldelete")
# --------------------------------------------------------------------------- #

@test("rowdelete: removes a data row (not header/separator) and undoes")
def _():
    text = "| A | B |\n| --- | --- |\n| x | y |\n| p | q |\n"
    src = write_md(text)
    field = create_site.split_table_row(text.splitlines()[2])[0]  # "x" (row 3, cell 0)
    status, _ = edit_support.apply_edit(src, {
        "op": "rowdelete", "line": 3, "cell": 0, "hash": block_hash(field)})
    eq(status, 200, "status")
    eq(src.read_text(encoding="utf-8"), "| A | B |\n| --- | --- |\n| p | q |\n", "row x/y removed")
    st2, b2 = edit_support.apply_edit(src, {"op": "rowdelete", "line": 2, "cell": 0, "hash": "x"})
    eq(st2, 400, "the separator row is not deletable")
    eq(b2["error"], "bad_row", "error")
    # the header row (line 1, the one before the separator) is not deletable either
    st3, b3 = edit_support.apply_edit(src, {"op": "rowdelete", "line": 1, "cell": 0, "hash": "x"})
    eq(st3, 400, "the header row is not deletable")
    eq(b3["error"], "bad_row", "error")
    edit_support.apply_undo(src)
    eq(src.read_text(encoding="utf-8"), text, "undo restores the row byte-identical")


@test("rowdelete: a data row above an all-dashes divider row is still deletable")
def _():
    # line 4 is a legit data row that is all dashes (a divider); the header
    # heuristic must not mistake line 3 (above it) for a header.
    text = "| A | B |\n| --- | --- |\n| x | y |\n| --- | --- |\n| p | q |\n"
    src = write_md(text)
    field = create_site.split_table_row(text.splitlines()[2])[0]  # "x" on line 3
    status, _ = edit_support.apply_edit(src, {
        "op": "rowdelete", "line": 3, "cell": 0, "hash": block_hash(field)})
    eq(status, 200, "the data row above an all-dashes row deletes")
    eq(src.read_text(encoding="utf-8"),
       "| A | B |\n| --- | --- |\n| --- | --- |\n| p | q |\n", "row x/y removed")


@test("coldelete: refuses to delete the last column; strips a header-only table's separator")
def _():
    # one-column table: deleting the column would leave zero columns -> refused.
    one = "| A |\n| --- |\n| x |\n"
    src = write_md(one)
    st, bd = edit_support.apply_edit(src, {
        "op": "coldelete", "start": 1, "end": 3, "col": 0, "line": 1, "cell": 0,
        "hash": block_hash("A")})
    eq(st, 400, "can't delete the last column")
    eq(bd["error"], "last_column", "error")
    eq(src.read_text(encoding="utf-8"), one, "unchanged")

    # header-only table (no data rows): the client's range includes the separator
    # (start..start+1), so BOTH the header and the separator drop the column.
    hdr = "| A | B |\n| --- | --- |\n"
    src2 = write_md(hdr)
    st2, _ = edit_support.apply_edit(src2, {
        "op": "coldelete", "start": 1, "end": 2, "col": 1, "line": 1, "cell": 1,
        "hash": block_hash("B")})
    eq(st2, 200, "status")
    eq(src2.read_text(encoding="utf-8"), "| A |\n| --- |\n", "header AND separator lost the column")


@test("coldelete: a ragged/short row in range is rejected (409)")
def _():
    # a data row with fewer fields than the header (drift) -> refuse, no write.
    text = "| A | B | C |\n| --- | --- | --- |\n| x | y |\n"
    src = write_md(text)
    st, _ = edit_support.apply_edit(src, {
        "op": "coldelete", "start": 1, "end": 3, "col": 2, "line": 1, "cell": 2,
        "hash": block_hash("C")})
    eq(st, 409, "ragged row rejected")
    eq(src.read_text(encoding="utf-8"), text, "unchanged")


@test("rowdelete: 409 on a stale cell hash, nothing removed")
def _():
    text = "| A | B |\n| --- | --- |\n| x | y |\n"
    src = write_md(text)
    status, _ = edit_support.apply_edit(src, {"op": "rowdelete", "line": 3, "cell": 0, "hash": "nope"})
    eq(status, 409, "stale")
    eq(src.read_text(encoding="utf-8"), text, "unchanged")


@test("coldelete: removes a column from every row (header/sep/data) and undoes")
def _():
    text = "| A | B | C |\n| --- | --- | --- |\n| x | y | z |\n"
    src = write_md(text)
    field = create_site.split_table_row(text.splitlines()[0])[1]  # "B" (header, cell 1)
    status, body = edit_support.apply_edit(src, {
        "op": "coldelete", "start": 1, "end": 3, "col": 1, "line": 1, "cell": 1,
        "hash": block_hash(field)})
    eq(status, 200, "status")
    eq(body["line_delta"], 0, "column delete never changes line count")
    eq(src.read_text(encoding="utf-8"), "| A | C |\n| --- | --- |\n| x | z |\n",
       "column B stripped from header, separator, and data")
    edit_support.apply_undo(src)
    eq(src.read_text(encoding="utf-8"), text, "undo restores the column byte-identical")


@test("coldelete: 409 on a stale hash; out-of-range column is a no-op 409")
def _():
    text = "| A | B |\n| --- | --- |\n| x | y |\n"
    src = write_md(text)
    eq(edit_support.apply_edit(src, {
        "op": "coldelete", "start": 1, "end": 3, "col": 0, "line": 1, "cell": 0, "hash": "bad"})[0],
       409, "stale hash")
    # col 9 exists in no row -> nothing removed -> 409
    field = create_site.split_table_row(text.splitlines()[0])[0]
    eq(edit_support.apply_edit(src, {
        "op": "coldelete", "start": 1, "end": 3, "col": 9, "line": 1, "cell": 0,
        "hash": block_hash(field)})[0], 409, "no column removed")
    eq(src.read_text(encoding="utf-8"), text, "unchanged throughout")


def main() -> int:
    print(f"editmode test suite  ({len(TESTS)} tests)")
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
