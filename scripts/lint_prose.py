#!/usr/bin/env python3
"""Native prose linter for webdoc - no external binary, pure stdlib.

Reimplements the slice of Vale we actually need so the skill works anywhere
Python 3 runs (macOS, Linux, CI), with nothing to `brew install`:

  * markdown-aware  - strips fenced code blocks (``` ... ```, ~~~ ... ~~~,
    including ```embed and ```stepper) and inline `code` spans before matching,
    so rules never fire inside diagram/source code. Fence handling is
    CommonMark-ish: it tracks the opening fence char + run length and only
    closes on a same-char run of >= that length, so a literal ``` nested inside
    a longer fence (e.g. a 4-backtick block) does not close it early.
  * regex rule engine with severities - rules live in ../lint/rules.json.
      kind "existence"  : regex search per prose line.
      kind "occurrence" : count regex matches per paragraph, flag when >= min.
      kind "staccato"   : flag runs of very short sentences per paragraph.
      severity "error"  : blocks (exit 1). "warning"/"suggestion": report only.

Exit codes: 0 = clean (or only advisories), 1 = error-level tells found,
2 = config error (rules.json missing/malformed) or source not found.

Used two ways:
  * create_site.py runs it as a default-on gate before building a site.
  * standalone:  python3 lint_prose.py path/to/doc.md   [--warn-only] [--json] [--no-lint]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
RULES_PATH = SKILL_DIR / "lint" / "rules.json"

# A fence opener: up to 3 leading spaces, then a run of >= 3 backticks or tildes.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# A fence closer: up to 3 leading spaces, a run of fences, only whitespace after.
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

# Markdown list-item line: "- x", "* x", "+ x", "1. x", "2) x".
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

# Abbreviations whose trailing period must not split a sentence. The dotted
# acronym pattern covers e.g./i.e./U.S./a.m. (single letters each + dot).
_DOTTED_ACRONYM = re.compile(r"\b(?:[A-Za-z]\.){2,}")
_ABBREV_WORDS = re.compile(
    r"\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|St|vs|etc|Fig|No|Inc|Ltd|Co)\.",
    re.IGNORECASE,
)

# ASCII control chars (incl. ESC 0x1b), minus tab/newline/carriage-return.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ConfigError(Exception):
    """rules.json is missing, malformed, or contains a bad rule."""


def _fence_open(line: str) -> tuple[str, int] | None:
    """If `line` opens a code fence, return (fence_char, run_length); else None."""
    m = _FENCE_OPEN.match(line)
    if not m:
        return None
    fence = m.group(1)
    info = m.group(2)
    # CommonMark: a backtick opener's info string may not contain a backtick
    # (otherwise it is ambiguous with an inline code span, not a fence).
    if fence[0] == "`" and "`" in info:
        return None
    return fence[0], len(fence)


def _fence_closes(line: str, char: str, run_len: int) -> bool:
    """True if `line` closes a fence opened with `run_len` of `char`."""
    m = _FENCE_CLOSE.match(line)
    if not m:
        return False
    fence = m.group(1)
    return fence[0] == char and len(fence) >= run_len


def _blank_inline_code(text: str) -> str:
    """Blank inline code spans (preserving length) so rules can't match inside
    them. Handles backtick runs of length N - a span opens with N backticks and
    closes at the next run of *exactly* N - and treats a backslash-escaped
    backtick (\\`) as literal text, not a delimiter."""
    chars = list(text)
    n = len(chars)
    out: list[str] = []
    i = 0
    while i < n:
        c = chars[i]
        # Escaped backtick: literal, copy both chars verbatim.
        if c == "\\" and i + 1 < n and chars[i + 1] == "`":
            out.append(c)
            out.append(chars[i + 1])
            i += 2
            continue
        if c == "`":
            # Measure the opening run length.
            j = i
            while j < n and chars[j] == "`":
                j += 1
            run = j - i
            # Scan for a closing run of exactly `run` backticks.
            k = j
            close = None
            while k < n:
                if chars[k] == "\\" and k + 1 < n and chars[k + 1] == "`":
                    k += 2  # escaped backtick is literal content, skip it
                    continue
                if chars[k] == "`":
                    m = k
                    while m < n and chars[m] == "`":
                        m += 1
                    if m - k == run:
                        close = m
                        break
                    k = m  # wrong length: part of the content, keep scanning
                    continue
                k += 1
            if close is not None:
                out.append(" " * (close - i))  # blank span incl. both delimiters
                i = close
                continue
            # No closing run: opening backticks are literal text.
            out.append("`" * run)
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _mask_abbreviations(text: str) -> str:
    """Replace the periods inside common abbreviations with a NUL sentinel so the
    sentence splitter does not break after them. Length-preserving."""
    def mask(m: re.Match) -> str:
        return m.group().replace(".", "\x00")

    text = _DOTTED_ACRONYM.sub(mask, text)
    text = _ABBREV_WORDS.sub(mask, text)
    return text


def _strip_control(s: str) -> str:
    """Drop ASCII control characters so a crafted snippet can't emit terminal
    escape sequences in the human-readable (non-JSON) output."""
    return _CONTROL.sub("", s)


def prose_lines(md: str) -> list[tuple[int, str]]:
    """Return (lineno, text) for prose lines only: outside fenced code blocks,
    with inline code blanked (kept same length) so rules cannot match inside
    code spans. Tracks the opening fence char + run length so a literal fence
    nested inside a longer fence does not close it early. An unclosed fence
    swallows the rest of the file (it is not linted) and emits one warning."""
    out: list[tuple[int, str]] = []
    fence_char: str | None = None
    fence_len = 0
    fence_open_line = 0
    for i, raw in enumerate(md.splitlines(), 1):
        if fence_char is None:
            opened = _fence_open(raw)
            if opened:
                fence_char, fence_len = opened
                fence_open_line = i
                continue
            out.append((i, _blank_inline_code(raw)))
        else:
            if _fence_closes(raw, fence_char, fence_len):
                fence_char = None
                fence_len = 0
            # fence delimiter and content lines are never linted
            continue
    if fence_char is not None:
        print(
            f"lint_prose: unclosed fence opened at line {fence_open_line}",
            file=sys.stderr,
        )
    return out


def paragraphs(lines: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Group consecutive non-blank prose lines into paragraphs."""
    paras: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    for lineno, text in lines:
        if text.strip():
            cur.append((lineno, text))
        elif cur:
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)
    return paras


def load_rules() -> list[dict]:
    """Load and shallow-validate rules.json. Raises ConfigError on any problem."""
    try:
        raw = RULES_PATH.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise ConfigError(f"rules file not found: {RULES_PATH}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read rules file {RULES_PATH}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"rules file is not valid JSON ({RULES_PATH}): {e}") from e
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        raise ConfigError(f"rules file must be a list or have a 'rules' list: {RULES_PATH}")
    return rules


def lint(md: str, rules: list[dict]) -> list[tuple[int, str, str, str]]:
    """Return sorted alerts as (lineno, severity, rule_name, message).

    Raises ConfigError if a rule is malformed (missing key / bad regex)."""
    lines = prose_lines(md)
    paras = paragraphs(lines)
    alerts: list[tuple[int, str, str, str]] = []
    for rule in rules:
        kind = rule.get("kind", "existence")
        # Pull every config-derived value (and compile regexes) up front so a
        # malformed rule surfaces as a ConfigError (exit 2), not a traceback.
        try:
            sev = rule["severity"]
            name = rule["name"]
            msg = rule["message"]
            flags = re.IGNORECASE if rule.get("ignorecase", True) else 0
            if kind == "existence":
                pats = [re.compile(p, flags) for p in rule["patterns"]]
            elif kind == "occurrence":
                pat = re.compile(rule["pattern"], flags)
                need = int(rule.get("min", 2))
            elif kind == "staccato":
                max_words = int(rule.get("max_words", 5))
                need = int(rule.get("min_run", 2))
        except (KeyError, re.error, ValueError, TypeError) as e:
            raise ConfigError(f"bad rule {rule.get('name', '?')!r}: {e}") from e

        if kind == "existence":
            # NOTE: existence rules match per line, so a pattern that spans a
            # line break is a known limitation (kept per-line by design).
            for lineno, text in lines:
                for p in pats:
                    m = p.search(text)
                    if m:
                        snippet = " ".join(m.group().split())[:60]
                        alerts.append((lineno, sev, name, msg.replace("%s", snippet)))
                        break
        elif kind == "occurrence":
            for para in paras:
                joined = " ".join(t for _, t in para)
                n = len(pat.findall(joined))
                if n >= need:
                    # Report the first line in the paragraph that actually
                    # contains a match, not the paragraph's first line.
                    report_line = para[0][0]
                    for ln, t in para:
                        if pat.search(t):
                            report_line = ln
                            break
                    alerts.append((report_line, sev, name, msg.replace("%s", str(n))))
        elif kind == "staccato":
            # Manufactured-drama tell: runs of very short declarative sentences.
            # Skip markdown list items ("- Fast." / "1. Stop.") so genuine
            # bullet lists don't read as staccato, and don't split on common
            # abbreviations (U.S., e.g., Dr.) that end in a period.
            splitter = re.compile(r"(?<=[.!?])\s+")
            for para in paras:
                body = [(ln, t) for ln, t in para if not _LIST_MARKER.match(t)]
                if not body:
                    continue
                # Join the body and keep a char-offset -> lineno map so we can
                # report the line where the offending run starts.
                parts: list[str] = []
                offsets: list[tuple[int, int]] = []  # (char_start, lineno), ascending
                pos = 0
                for ln, t in body:
                    offsets.append((pos, ln))
                    parts.append(t)
                    pos += len(t) + 1  # +1 for the join space
                masked = _mask_abbreviations(" ".join(parts))  # length-preserving

                sentences: list[tuple[int, str]] = []  # (char_start, text)
                prev = 0
                for m in splitter.finditer(masked):
                    sentences.append((prev, masked[prev:m.start()]))
                    prev = m.end()
                sentences.append((prev, masked[prev:]))

                run = worst = 0
                run_start = best_start = 0
                for start, sent in sentences:
                    wc = len(sent.split())
                    if 0 < wc <= max_words:
                        if run == 0:
                            run_start = start
                        run += 1
                        if run > worst:
                            worst = run
                            best_start = run_start
                    else:
                        run = 0
                if worst >= need:
                    report_line = body[0][0]
                    for char_start, ln in offsets:
                        if char_start <= best_start:
                            report_line = ln
                        else:
                            break
                    alerts.append((report_line, sev, name, msg.replace("%s", str(worst))))
        else:
            print(
                f"lint_prose: unknown rule kind {kind!r} in rule {name!r}; skipping",
                file=sys.stderr,
            )
    return sorted(alerts, key=lambda a: (a[0], a[2]))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Lint Markdown prose for structural AI-writing tells (webdoc gate, no external deps)."
    )
    ap.add_argument("source", type=Path, help="Markdown file to lint")
    ap.add_argument("--no-lint", action="store_true", help="skip the gate entirely (escape hatch)")
    ap.add_argument("--warn-only", action="store_true", help="report findings but always exit 0")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON on stdout")
    args = ap.parse_args()

    if args.no_lint:
        return 0

    src = args.source.expanduser()
    if not src.is_file():
        print(f"lint_prose: source not found: {src}", file=sys.stderr)
        return 2

    md = src.read_text(encoding="utf-8", errors="replace")
    try:
        rules = load_rules()
        alerts = lint(md, rules)
    except ConfigError as e:
        print(f"lint_prose: config error: {e}", file=sys.stderr)
        return 2

    errors = [a for a in alerts if a[1] == "error"]
    advisory = [a for a in alerts if a[1] != "error"]

    if args.json:
        # JSON consumers get faithful data; json.dumps escapes control chars.
        print(json.dumps(
            [{"line": l, "severity": s, "rule": r, "message": m} for l, s, r, m in alerts],
            indent=2,
        ))
    else:
        for line, sev, rule, message in alerts:
            # Strip control chars so a crafted snippet can't drive the terminal.
            print(f"  {sev.upper():10} L{line:>4}  {rule}: {_strip_control(message)}", file=sys.stderr)
        print(
            f"prose lint: {len(errors)} error(s), {len(advisory)} advisory  ({src.name})",
            file=sys.stderr,
        )

    if errors and not args.warn_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
