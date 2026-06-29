# webdoc

webdoc turns the output of a Claude Code or Codex session into a local website that reads better than raw Markdown in a terminal. It is a skill for [Claude Code](https://code.claude.com/docs/en/skills) and [OpenAI Codex](https://developers.openai.com/codex/skills): when an agent produces something long, say a report, a research memo, a comparison matrix, a handoff, webdoc renders that output into a navigable local‑first site with a table of contents, tables that stay tables, charts, diagrams. Because the page is live, you can leave feedback right on it; the agent reads your notes back from durable local storage and revises.

## Why

A long agent answer is hard to read as raw Markdown in a terminal. You scroll, you lose your place, dense tables wrap into noise. webdoc gives that output a page you can scan and search and come back to. The interactivity closes the loop: you annotate the page, and the agent picks your notes up from a local file and revises, with no pasting 'change this, fix that' back into chat. The loop is for *you* first. Sharing the result with a team is one way to use it.

The source document stays canonical. The website is a view of it.

## What it does

Point `create_site.py` at a Markdown or report file and it writes a static site: `index.html`, `style.css`, `manifest.json`. The page carries a table of contents and responsive tables, with visuals generated from code. You author the interactive pieces in the same Markdown: a ` ```stepper ` block becomes a click‑to‑step state machine, and a ` ```embed ` block drops in your own HTML and JS, or media like audio clips, photo galleries, toggles. Nothing loops on its own, so the reader sets the pace.

Tables matter here, since dense answers live or die on them. Bold the advantageous value with `**…**`, and lead a cell with `[+]` / `[-]` / `[~]` for green / red / amber conditional formatting. Every build also writes a single self‑contained `doc.html`: upload it to Google Drive, then open it with Google Docs. A fidelity ledger names whatever interactive or audio pieces won't survive that conversion, so nothing drops without warning.

The prose itself has to pass a gate. `lint_prose.py`, pure standard library with no external binary, runs on every build and looks for the structural tells a wordlist can't catch: negative parallelism, signpost openers, stacked negation, the reflexive rule‑of‑three, em and en dashes, a staccato run of clipped sentences. The high‑precision tells block the build; the rest come back as warnings. Rules and severities live in an editable `lint/rules.json`, and the bundled [avoid‑ai‑writing](https://github.com/conorbronsdon/avoid-ai-writing) ruleset shapes the prose guidance the agent follows.

Once a site is served you can fix it in place. Click Edit, then change any block right where it sits: a paragraph, a heading, a list item, a table cell. Each save round‑trips surgically back into the canonical `.md`, keyed by a per‑block source line‑range plus a content hash, and a sibling override ledger keeps a later agent pass from quietly overwriting what you changed. The write path stays localhost‑only, hash‑checked against drift, filtered through a strict HTML‑to‑Markdown whitelist so the round‑trip stays lossless.

Feedback closes the same loop. Leave notes on the page and they `POST` to a localhost API, appending to `feedback.jsonl`, which the agent reads directly before it revises.

Diagrams are a first‑class output here, on a colorblind‑safe palette and a self‑contained offline embed contract: hand‑authored SVG when layout matters, pre‑rendered chart specs otherwise, click‑to‑step explanations for anything that changes over time (see `references/diagramming.md`). When a visual is worth getting right, generate five distinct concepts and review them on one gallery page: flip between them, pop any one out full‑screen, let your pick land back with the agent. Swap the whole look with `--template` (color, type, spacing). Save a look you like as a private, reusable category; a project or team then shares one consistent style.

The finished site auto‑opens in your browser, unless you switch that off in `~/.config/webdoc/settings.json`. Everything stays on the machine: loopback binding, an OS‑assigned port, a finite TTL, a clean shutdown, no symlink escape.

## Requirements

- Python 3.8+ (standard library only, no pip install needed).
- A modern browser to view served sites. Optional: `xdg-open` (Linux) or `open` (macOS) for auto‑open; falls back to Python's `webbrowser`.

## Install

Clone, then make the skill discoverable to your agent by symlinking it into the skills directory:

```bash
git clone https://github.com/oooseun/webdoc.git
ln -s "$(pwd)/webdoc" ~/.claude/skills/webdoc     # Claude Code
ln -s "$(pwd)/webdoc" ~/.codex/skills/webdoc      # Codex
```

The agent invokes the skill on its own when it's about to produce a report, a research memo, a handoff, any durable artifact worth keeping. You can also drive the scripts by hand.

## Usage

Create a site from a Markdown file:

```bash
python3 scripts/create_site.py report.md
python3 scripts/create_site.py report.md --out ./report_site --title "Research Report"
python3 scripts/create_site.py report.md --css custom.css --js widget.js --asset clip.mp3
python3 scripts/create_site.py report.md --template ./fashion-theme.css   # swap the whole look
```

Each build also writes `doc.html` next to `index.html`. To get it into Google Docs, upload that file to Google Drive, then right‑click → Open with → Google Docs.

Serve it locally (loopback only, auto‑assigned port, 2‑hour TTL; opens the browser unless `--no-open`):

```bash
python3 scripts/serve_site.py start ./report_site --ttl 7200
python3 scripts/serve_site.py status ./report_site
python3 scripts/serve_site.py stop ./report_site
```

Read your feedback (what the agent consumes to iterate):

```bash
cat ./report_site/feedback.jsonl
```

## How it fits together

- `SKILL.md`: the full instruction set the agent follows: when to create vs. offer vs. skip, the decision matrix, the feedback rule, the visual‑explanation guidance, the quality bar.
- `scripts/create_site.py`: Markdown/report → unified site (`index.html`) plus the self‑contained `doc.html` export; handles `stepper`/`embed` blocks, conditional‑format cells, `--css/--js/--asset` bundling.
- `scripts/serve_site.py`: start/stop/inspect a localhost‑only preview server with durable `feedback.jsonl`, the editing‑mode write API, config‑driven auto‑open.
- `scripts/lint_prose.py`: the native, dependency‑free prose linter; run automatically as a build gate and also usable standalone (`python3 scripts/lint_prose.py file.md`, with `--warn-only` / `--json`). Rules in `lint/rules.json`.
- `scripts/edit_support.py`: editing‑mode round‑trip: validate, hash‑check for drift, write the `.md` atomically, then maintain the override ledger (`check_overrides`, the anti‑clobber contract).
- `scripts/html2md.py`: the strict, total HTML‑to‑Markdown whitelist converter used by the edit round‑trip.
- `scripts/templates.py`: list built‑in templates and save a stylesheet as a private reusable category.
- `scripts/gallery.py`: assemble a one‑page concept switcher over several built sites (the concept‑variations workflow).
- `scripts/settings.py`: reads user config from `~/.config/webdoc/settings.json`.
- `templates/standard/style.css`: the `standard` theme. `assets/stepper.js`: the click‑to‑step primitive. `assets/edit.js` and `assets/edit.css`: the in‑page editor, bundled into every site and inert until you click Edit.
- `references/avoid-ai-writing.md`: the full anti‑AI‑writing ruleset, applied to all generated prose.
- `references/structural-tells.md`: the structural AI‑writing tells a wordlist misses, the counted second‑pass audit, the way the linter enforces them.
- `references/diagramming.md`: the diagram and interactive‑visual system: palette, the offline embed contract, the stepper recipe, the render/verify gates.
- `references/presenter-role.md`: role prompt for a narrow "presenter" subagent that lays out and verifies the site without touching the analysis.
- `references/research-basis.md`: the research and source map the design is built on.
- `tests/`: standard‑library test suites for the linter, the editing‑mode round‑trip, the write‑path guards. Run any with `python3 tests/<file>.py`.
- `agents/openai.yaml`: Codex skill interface metadata.

## Design principles

- The source document is canonical; the website is a view, never a rewrite.
- Website input must become agent‑readable local state. Never make a human copy‑paste feedback back into chat.
- Static output over dev servers; build tools only when real interactivity is needed.
- Local‑only by default. A local site may carry secrets, so never host or share one off the machine without explicit intent.

## Attribution

webdoc embeds the anti‑AI‑writing ruleset from [conorbronsdon/avoid-ai-writing](https://github.com/conorbronsdon/avoid-ai-writing) (MIT). Its operational core is inlined in `SKILL.md` and the full text is bundled verbatim as `references/avoid-ai-writing.md`. The structural‑tell catalogue and counted audit draw on [matteoroversi/anti-ai-rhetoric](https://github.com/matteoroversi/anti-ai-rhetoric) (MIT) and Wikipedia's "[Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing)" (CC BY‑SA 4.0); patterns are re‑expressed in our own words, none reproduced verbatim. See [`NOTICE`](NOTICE) for full credits and license terms.

## License

MIT: see [LICENSE](LICENSE). Bundled third‑party material retains its own license; see [`NOTICE`](NOTICE).
