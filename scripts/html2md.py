#!/usr/bin/env python3
"""Strict, total HTML -> Markdown converter for webdoc editing mode.

The in-page editor (assets/edit.js) hands the server a block's contenteditable
innerHTML. This turns that HTML back into the limited Markdown webdoc supports,
on a whitelist so nothing else survives:

    text                 -> escaped plain text
    <strong>/<b>         -> **x**
    <em>/<i>             -> *x*
    <code>               -> `x`   (content kept literal; fence widens if it
                                   contains backticks, CommonMark-style)
    <a href="u">t</a>    -> [t](u)   (dropped to plain text if u is a
                                      javascript:/data:/vbscript: scheme)
    <br>                 -> newline

Any other element contributes only its text content (its tag is dropped); a
block-level element (div/p/li/h*/blockquote/tr/...) also emits a word boundary
so an Enter-inserted <div> can't mash words together. Nested identical marks
collapse (only the outer delimiter is emitted). Markdown-control characters in
plain text are backslash-escaped so they round-trip as literal characters
rather than re-parsing as formatting. The function is deterministic and never
raises - malformed input degrades to its text content.

Pure stdlib (html.parser); importable as a module.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Inline marks we emit, by delimiter. Nested identical marks collapse to one.
_MARK_TAGS = {"strong": "**", "b": "**", "em": "*", "i": "*"}

# Block-level elements: emit a word boundary on enter/leave so an editor's
# Enter-inserted <div>/<p> (or a stray <tr>/<li>) does not concatenate the
# words on either side ("A.<div>B.</div>" -> "A. B.").
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "details", "div", "dl", "dd",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}

# Characters webdoc's inline_md (create_site.py) treats as Markdown syntax. We
# backslash-escape these in plain text so a literal char the user typed is not
# re-interpreted as a code span / emphasis / link on the next render. inline_md
# honours these backslash escapes (added there) and shows the bare character.
# NOTE: '|' is deliberately NOT escaped here - paragraphs allow a literal pipe;
# a pipe inside a *table cell* is escaped cell-scoped on write (edit_support).
_ESCAPE = {"\\": "\\\\", "`": "\\`", "*": "\\*", "[": "\\[", "]": "\\]"}

# Schemes a link must never carry into the canonical .md (a live-scheme link is
# stored XSS waiting to render). Mirror create_site.safe_href's policy: strip
# ALL control/space chars first so "java\tscript:" can't sneak past the test.
_CTRL_RUN = re.compile(r"[\x00-\x20]+")


def _escape_text(text: str) -> str:
    return "".join(_ESCAPE.get(ch, ch) for ch in text)


def _escape_href(href: str) -> str:
    # inline_md's link rule stops the destination at the first ')'. Percent-encode
    # parens (and strip whitespace) so a URL with parens still round-trips.
    href = href.strip()
    return href.replace("(", "%28").replace(")", "%29")


def _disallowed_scheme(href: str) -> bool:
    """True if href resolves to a javascript:/data:/vbscript: scheme.

    Browsers ignore interior/leading control + whitespace chars in a scheme, so
    we strip them before testing - "java\\tscript:" and "\\x01javascript:" are
    both disallowed."""
    probe = _CTRL_RUN.sub("", href).lower()
    return probe.startswith(("javascript:", "data:", "vbscript:"))


def _fence_code(content: str) -> str:
    """Wrap code-span content in a backtick fence wide enough to contain it.

    CommonMark: a code span is delimited by a run of N backticks longer than any
    backtick run inside it; if the content starts or ends with a backtick, a
    single padding space (stripped on render) keeps the fence legible."""
    longest = 0
    run = 0
    for ch in content:
        if ch == "`":
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    fence = "`" * (longest + 1)
    if content[:1] == "`" or content[-1:] == "`":
        return f"{fence} {content} {fence}"
    return f"{fence}{content}{fence}"


class _Converter(HTMLParser):
    def __init__(self) -> None:
        # convert_charrefs decodes entities (&amp; -> &) into handle_data.
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        # Each open element pushes a record so its end tag can reverse the start.
        self.stack: list[dict] = []
        self.code_depth = 0
        # Buffer for the current top-level <code>'s text, so the closing tag can
        # size its backtick fence to the content (CommonMark).
        self.code_buf: list[str] = []
        # How many delimiters of each kind are currently open (for collapsing).
        self.mark_depth: dict[str, int] = {"**": 0, "*": 0}

    # -- word boundaries -----------------------------------------------------
    def _boundary(self) -> None:
        """Emit a single separating space between block-level neighbours.

        No-op inside a code span, at the very start, or right after existing
        whitespace - so it never doubles spaces or leaks into code."""
        if self.code_depth > 0 or not self.out:
            return
        last = self.out[-1]
        if last and not last[-1].isspace():
            self.out.append(" ")

    # -- starts --------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self.out.append("\n")
            return
        # Inside <code>, everything is literal: no nested marks, no escaping,
        # no boundaries. A nested <code> just deepens the span.
        if self.code_depth > 0:
            if tag == "code":
                self.code_depth += 1
            self.stack.append({"tag": tag, "emit": False})
            return
        if tag == "code":
            # Start buffering; the fence is sized and emitted on the end tag.
            self.code_depth += 1
            self.code_buf = []
            self.stack.append({"tag": tag, "emit": True, "code": True})
            return
        if tag in _MARK_TAGS:
            delim = _MARK_TAGS[tag]
            emit = self.mark_depth[delim] == 0
            if emit:
                self.out.append(delim)
            self.mark_depth[delim] += 1
            self.stack.append({"tag": tag, "emit": emit, "mark": delim})
            return
        if tag == "a":
            href = None
            for key, value in attrs:
                if key.lower() == "href" and value:
                    href = value
                    break
            # A disallowed-scheme link is dropped to plain text: the canonical
            # .md must never store a live-scheme link.
            if href and not _disallowed_scheme(href):
                self.out.append("[")
                self.stack.append({"tag": tag, "emit": True, "href": href})
            else:
                self.stack.append({"tag": tag, "emit": False})
            return
        # Any other element: keep its text content only. Block-level elements
        # also emit a word boundary so neighbouring words stay separated.
        is_block = tag in _BLOCK_TAGS
        if is_block:
            self._boundary()
        self.stack.append({"tag": tag, "emit": False, "block": is_block})

    # -- ends ----------------------------------------------------------------
    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "br":
            return
        # Find the nearest matching open record; close it and any unclosed
        # elements opened after it (treat them as implicitly closed). This keeps
        # the converter total on malformed nesting.
        idx = None
        for j in range(len(self.stack) - 1, -1, -1):
            if self.stack[j]["tag"] == tag:
                idx = j
                break
        if idx is None:
            return
        popped = self.stack[idx:]
        del self.stack[idx:]
        for rec in reversed(popped):
            self._close(rec)

    def _close(self, rec: dict) -> None:
        if rec["tag"] == "code":
            if self.code_depth > 0:
                self.code_depth -= 1
            if rec.get("code"):  # the outermost <code>: size + emit the fence
                content = "".join(self.code_buf)
                self.code_buf = []
                self.out.append(_fence_code(content))
        elif "mark" in rec:
            delim = rec["mark"]
            if self.mark_depth[delim] > 0:
                self.mark_depth[delim] -= 1
            if rec.get("emit"):
                self.out.append(delim)
        elif rec["tag"] == "a" and rec.get("emit"):
            self.out.append("](" + _escape_href(rec["href"]) + ")")
        elif rec.get("block"):
            self._boundary()

    # -- text ----------------------------------------------------------------
    def handle_data(self, data: str) -> None:
        # Non-breaking spaces (&nbsp; / U+00A0) are an editing artifact; treat
        # them as ordinary spaces so words stay separable, not glued.
        data = data.replace("\xa0", " ")
        if self.code_depth > 0:
            self.code_buf.append(data)  # literal inside a code span
        else:
            self.out.append(_escape_text(data))

    def result(self) -> str:
        # Close anything still open (e.g. <strong> with no </strong>).
        for rec in reversed(self.stack):
            self._close(rec)
        self.stack.clear()
        # Strip the boundary spaces that can collect at the very edges; internal
        # newlines (from <br>) survive for flatten() to collapse if it wants.
        return "".join(self.out).strip()


def html_to_markdown(html_fragment: str) -> str:
    """Convert a contenteditable HTML fragment to whitelisted Markdown.

    Total and deterministic: any parse problem degrades to text content, never
    an exception."""
    if not html_fragment:
        return ""
    conv = _Converter()
    try:
        conv.feed(str(html_fragment))
        conv.close()
    except Exception:
        # HTMLParser is forgiving, but never let a surprise propagate.
        pass
    return conv.result()


if __name__ == "__main__":  # pragma: no cover - tiny manual smoke
    import sys

    print(html_to_markdown(sys.stdin.read()))
