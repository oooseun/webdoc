#!/usr/bin/env python3
"""CLI-contract test suite for webdoc's native prose linter.

These tests exercise scripts/lint_prose.py through its *stable* command-line
contract only - never its internal functions - so they keep passing across
refactors of the implementation:

  * findings:    python3 lint_prose.py <file> --json   -> JSON list of
                 {line, severity, rule, message} on stdout
  * gate:        python3 lint_prose.py <file>           -> exit 0 clean,
                 1 error-level tell, 2 config/source error

Each behaviour below is a SPEC the implementation must satisfy. Some may FAIL
against the present code - that is expected and intentional: a failing test
names a fix that still needs to land. The suite exits non-zero whenever any
spec behaviour is unmet, so it doubles as a gate once every fix is in.

Pure stdlib. Run it directly:

    python3 tests/test_lint_prose.py

Point it at a different linter with the LINT_PROSE env var if needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# --------------------------------------------------------------------------- #
# Locating the linter under test
# --------------------------------------------------------------------------- #

DEFAULT_LINTER = str(Path(__file__).resolve().parents[1] / "scripts" / "lint_prose.py")
LINTER = os.environ.get("LINT_PROSE", DEFAULT_LINTER)


# --------------------------------------------------------------------------- #
# CLI drivers - the only way these tests touch the linter
# --------------------------------------------------------------------------- #

def _write_tmp(md: str) -> str:
    """Write `md` to a temp .md file (utf-8, so em/en dashes survive) and
    return its path. Caller is responsible for unlinking."""
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(md)
    return path


def run_json(md: str) -> list[dict]:
    """Lint `md` via `--json` and return the parsed findings list."""
    path = _write_tmp(md)
    try:
        proc = subprocess.run(
            [sys.executable, LINTER, path, "--json"],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(path)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"--json did not emit valid JSON (rc={proc.returncode}): {exc}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    if not isinstance(data, list):
        raise AssertionError(f"--json output is not a list: {data!r}")
    return data


def run_exit(md: str, *extra: str) -> int:
    """Lint `md` via a plain (non-JSON) run and return the process exit code."""
    path = _write_tmp(md)
    try:
        proc = subprocess.run(
            [sys.executable, LINTER, path, *extra],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(path)
    return proc.returncode


# --------------------------------------------------------------------------- #
# Assertion helpers (scoped to one rule, so unrelated rules never interfere)
# --------------------------------------------------------------------------- #

def assert_fires(md: str, rule: str, severity: str | None = None,
                 line: int | None = None) -> None:
    alerts = run_json(md)
    hits = [a for a in alerts if a["rule"] == rule]
    assert hits, (
        f"expected rule {rule!r} to FIRE\n  input: {md!r}\n  got:   {alerts}"
    )
    if severity is not None:
        assert any(h["severity"] == severity for h in hits), (
            f"rule {rule!r} fired but not at severity {severity!r}\n  hits: {hits}"
        )
    if line is not None:
        assert any(h["line"] == line for h in hits), (
            f"rule {rule!r} fired but not on line {line}\n  hits: {hits}"
        )


def assert_quiet(md: str, rule: str) -> None:
    alerts = run_json(md)
    hits = [a for a in alerts if a["rule"] == rule]
    assert not hits, (
        f"expected rule {rule!r} to STAY QUIET\n  input: {md!r}\n  hits:  {hits}"
    )


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #

EM = "—"   # em dash  (U+2014)
EN = "–"   # en dash  (U+2013)

# Genuinely clean prose: no em dash, no signpost, no tricolon, no ", not",
# no ai-vocab, no short-sentence run. Used for the exit-0 gate test.
CLEAN_DOC = (
    "The configuration file stores connection settings for the database and "
    "the cache.\n"
    "Each value can be overridden through an environment variable when the "
    "service starts.\n"
)

# A doc carrying one unambiguous error-level tell (em dash).
ERROR_DOC = f"This release is great {EM} really.\n"


# --------------------------------------------------------------------------- #
# Test registry
# --------------------------------------------------------------------------- #

TESTS: list[tuple[str, "callable"]] = []


def test(name: str):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


# ---- stacked-negation (error) --------------------------------------------- #

@test("stacked-negation: FIRES on sentence-initial triad")
def _():
    assert_fires("Not a tool. Not a feature. A revolution.",
                 "stacked-negation", severity="error")


@test("stacked-negation: quiet on legit negations in different contexts")
def _():
    assert_quiet(
        "It isn't supported on Windows. On Linux, not all distros ship "
        "the library.",
        "stacked-negation",
    )


@test("stacked-negation: quiet on a single mid-sentence contrast")
def _():
    assert_quiet("Configure the server, not the client, and you are done.",
                 "stacked-negation")


# ---- signpost-opener (error) ---------------------------------------------- #

@test("signpost-opener: FIRES on 'Below is the plan.'")
def _():
    assert_fires("Below is the plan.", "signpost-opener", severity="error")


@test("signpost-opener: FIRES on \"Here's the thing:\"")
def _():
    assert_fires("Here's the thing: it works.",
                 "signpost-opener", severity="error")


@test("signpost-opener: quiet on 'The following holds for all n.'")
def _():
    assert_quiet("The following holds for all n.", "signpost-opener")


@test("signpost-opener: quiet on 'Now that we have the token, ...'")
def _():
    assert_quiet("Now that we have the token, the API accepts requests.",
                 "signpost-opener")


# ---- tricolon (suggestion) ------------------------------------------------ #

@test("tricolon: FIRES on 'fast, scalable, and secure'")
def _():
    assert_fires("It is fast, scalable, and secure.",
                 "tricolon", severity="suggestion")


@test("tricolon: quiet on a plain list (ideal) 'Python, Ruby, Go and Rust'")
def _():
    # Spec marks this 'ideally' quiet - a no-Oxford-comma list is not a tricolon.
    assert_quiet("We support Python, Ruby, Go and Rust.", "tricolon")


# ---- em-dash (error) ------------------------------------------------------ #

@test("em-dash: FIRES on a U+2014 em dash")
def _():
    assert_fires(f"This release is great {EM} really.",
                 "em-dash", severity="error")


@test("em-dash: FIRES on letter-flanked U+2013 ('cost-benefit')")
def _():
    assert_fires(f"The cost{EN}benefit analysis matters.",
                 "em-dash", severity="error")


@test("em-dash: FIRES on 'word--word'")
def _():
    assert_fires("This is word--word here.", "em-dash", severity="error")


@test("em-dash: quiet on numeric range '10-20' (U+2013)")
def _():
    assert_quiet(f"We shipped 10{EN}20 items.", "em-dash")


@test("em-dash: quiet on numeric range '14-28' (U+2013)")
def _():
    assert_quiet(f"Expect 14{EN}28 days of lead time.", "em-dash")


# ---- ai-vocab (warning) --------------------------------------------------- #

@test("ai-vocab: FIRES on 'leveraging'")
def _():
    assert_fires("We are leveraging the API.", "ai-vocab", severity="warning")


@test("ai-vocab: FIRES on 'utilizing'")
def _():
    assert_fires("Teams are utilizing the tool.", "ai-vocab", severity="warning")


@test("ai-vocab: FIRES on 'showcasing'")
def _():
    assert_fires("The demo is showcasing results.",
                 "ai-vocab", severity="warning")


@test("ai-vocab: FIRES on 'delving'")
def _():
    assert_fires("We are delving into the details.",
                 "ai-vocab", severity="warning")


@test("ai-vocab: FIRES on 'underscored'")
def _():
    assert_fires("The report underscored the risk.",
                 "ai-vocab", severity="warning")


# ---- negative-parallelism (warning) --------------------------------------- #

@test("negative-parallelism: FIRES on \"isn't about luck, it's about ...\"")
def _():
    assert_fires("Success isn't about luck, it's about preparation.",
                 "negative-parallelism", severity="warning")


@test("negative-parallelism: FIRES on 'AI, not ML.'")
def _():
    assert_fires("AI, not ML.", "negative-parallelism", severity="warning")


@test("negative-parallelism: quiet on transition 'However, not all ...'")
def _():
    assert_quiet("However, not all cases apply.", "negative-parallelism")


# ---- staccato (warning) --------------------------------------------------- #

@test("staccato: quiet when an abbreviation ('U.S.') fakes a short sentence")
def _():
    assert_quiet(
        "The U.S. government funded it. The program then expanded nationwide "
        "over the next decade with broad bipartisan support.",
        "staccato",
    )


@test("staccato: FIRES on a real run 'Fast. Cheap. Easy. Done.'")
def _():
    assert_fires("Fast. Cheap. Easy. Done.", "staccato", severity="warning")


# ---- generic-temporal-opener (warning) ------------------------------------ #

@test("generic-temporal-opener: FIRES on \"In today's fast-paced landscape\"")
def _():
    assert_fires(
        "In today's fast-paced landscape, staying ahead matters more than ever.",
        "generic-temporal-opener", severity="warning",
    )


# ---- balancing-hedge (warning) -------------------------------------------- #

@test("balancing-hedge: FIRES on 'While X, it also Y'")
def _():
    assert_fires("While AI offers efficiency, it also poses challenges.",
                 "balancing-hedge", severity="warning")


# ---- markdown-awareness --------------------------------------------------- #

@test("markdown: control - em dash in plain prose DOES fire")
def _():
    # Anchors the fenced/inline tests below: the same tell must fire in prose.
    assert_fires(f"A bare line with an em dash {EM} here.", "em-dash")


@test("markdown: em dash inside a ```embed fence does NOT fire")
def _():
    doc = "\n".join(["```embed", f"Caption with an em dash {EM} inside.", "```", ""])
    assert_quiet(doc, "em-dash")


@test("markdown: em dash inside a ~~~ tilde fence does NOT fire")
def _():
    doc = "\n".join(["~~~", f"Text with an em dash {EM} inside.", "~~~", ""])
    assert_quiet(doc, "em-dash")


@test("markdown: em dash inside an `inline code` span does NOT fire")
def _():
    assert_quiet(f"Use the value `a {EM} b` in the config.", "em-dash")


@test("markdown: 'leveraging' inside a ```embed fence does NOT fire")
def _():
    doc = "\n".join(["```embed", "We are leveraging the API here.", "```", ""])
    assert_quiet(doc, "ai-vocab")


@test("markdown: ai-vocab inside an `inline code` span does NOT fire")
def _():
    assert_quiet("Run `leverage --help` to see options.", "ai-vocab")


@test("markdown: prose AFTER a closed ```stepper block is still linted")
def _():
    # A literal ``` closing the stepper must not swallow the rest of the doc:
    # the em dash on the trailing prose line (line 5) must still be caught.
    doc = "\n".join([
        "```stepper",            # 1
        "Step one content here.",  # 2
        "```",                   # 3
        "",                      # 4
        f"Real prose with an em dash {EM} here.",  # 5
        "",
    ])
    assert_fires(doc, "em-dash", severity="error", line=5)


# ---- gate contract (exit codes) ------------------------------------------- #

@test("gate: clean doc exits 0")
def _():
    rc = run_exit(CLEAN_DOC)
    assert rc == 0, f"clean doc should exit 0, got {rc}"


@test("gate: error-level tell exits 1")
def _():
    rc = run_exit(ERROR_DOC)
    assert rc == 1, f"doc with an error-level tell should exit 1, got {rc}"


@test("gate: --warn-only downgrades an error tell to exit 0")
def _():
    rc = run_exit(ERROR_DOC, "--warn-only")
    assert rc == 0, f"--warn-only should exit 0 even with an error tell, got {rc}"


@test("gate: --no-lint exits 0 even with an error tell")
def _():
    rc = run_exit(ERROR_DOC, "--no-lint")
    assert rc == 0, f"--no-lint should exit 0, got {rc}"


@test("gate: missing source file exits 2")
def _():
    proc = subprocess.run(
        [sys.executable, LINTER, "/no/such/path/definitely-missing.md"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2, (
        f"missing source should exit 2, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )


@test("gate: a malformed rules.json exits 2 (config error)")
def _():
    # The CLI has no --rules flag, so to point the linter at a bad config we
    # copy the script into a throwaway scaffold whose ../lint/rules.json is
    # deliberately broken (RULES_PATH resolves relative to the script), then
    # run that copy through the same CLI contract. The real rules.json is never
    # touched.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "lint").mkdir()
        shutil.copy(LINTER, root / "scripts" / "lint_prose.py")
        (root / "lint" / "rules.json").write_text(
            "{ this is not valid json ,,, ", encoding="utf-8"
        )
        doc = root / "doc.md"
        doc.write_text("Some ordinary prose here.\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "lint_prose.py"), str(doc)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 2, (
        f"malformed rules.json should exit 2, got {proc.returncode}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def main() -> int:
    if not Path(LINTER).is_file():
        print(f"FATAL: linter not found at {LINTER!r} "
              f"(override with the LINT_PROSE env var)", file=sys.stderr)
        return 2

    print(f"lint_prose CLI contract suite  ({len(TESTS)} tests)")
    print(f"  linter: {LINTER}")
    print(f"  python: {sys.executable}")
    print("-" * 72)

    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"FAIL  {name}")
        except Exception:  # unexpected error in the test itself
            failed.append((name, traceback.format_exc()))
            print(f"ERROR {name}")
        else:
            passed.append(name)
            print(f"ok    {name}")

    print("-" * 72)
    if failed:
        print("\nFAILURES (each names a spec behaviour the linter does not yet meet):\n")
        for name, detail in failed:
            print(f"### {name}")
            for line in detail.rstrip().splitlines():
                print(f"    {line}")
            print()

    print(f"summary: {len(passed)} passed, {len(failed)} failed, "
          f"{len(TESTS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
