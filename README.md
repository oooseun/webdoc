# webdoc

**Turn the output of a Claude Code (or Codex) session into an interactive local website — easier to read, digest, and iterate on than scrolling Markdown in a terminal.**

`webdoc` is a skill for [Claude Code](https://code.claude.com/docs/en/skills) and [OpenAI Codex](https://developers.openai.com/codex/skills). When an agent produces a long report, dossier, research memo, comparison matrix, project status, or handoff, webdoc renders that output into a polished, navigable, local‑first website — a table of contents, real tables, charts, diagrams — so you can actually take it in. Because the page is interactive, you can leave feedback right on it; the agent reads your notes back from durable local storage and revises. No cloud, no copy‑paste, no public exposure by default.

## Why

A long agent answer is hard to read as raw Markdown in a terminal — you scroll, you lose the structure, dense tables wrap into noise. webdoc gives that output a real reading surface: a navigable page you can scan, search, and return to. And the interactivity closes the loop — instead of pasting "change this, fix that" back into chat, you annotate the page and the agent picks it up from a local file and iterates. The loop is for *you* first; sharing the result with a team is just one way to use it.

The source document stays canonical — the website is a view of it, never a rewrite.

## What it does

- **Renders** a Markdown/report source into a static site (`index.html`, `style.css`, `manifest.json`) with a table of contents, responsive tables, and code‑generated visuals.
- **Interactive components in one source.** Author click‑to‑step state machines (` ```stepper `) and arbitrary HTML/JS/media (` ```embed ` — audio, photo galleries, toggles) right in the Markdown. No looping animations; the reader controls the pace.
- **Dense tables.** Bold the advantageous value with `**…**`; lead a cell with `[+]` / `[-]` / `[~]` for green / red / amber conditional formatting.
- **Google Docs export.** Every build also writes a single self‑contained `doc.html` — upload it to Google Drive and Open with Google Docs. A fidelity ledger reports exactly which interactive/audio bits won't survive the conversion.
- **Anti‑AI prose.** Applies an embedded copy of the [avoid‑ai‑writing](https://github.com/conorbronsdon/avoid-ai-writing) ruleset to everything it writes.
- **Concept variations.** For a substantial visual, generate five distinct concepts and review them in one gallery page — flip between them, pop any out full‑screen, and your pick is recorded for the agent.
- **Themeable.** Swap the whole look with `--template` (color, type, spacing); save a look you like as a private, reusable category for consistent output across a project or team.
- **Captures feedback for iteration** — leave notes on the page; submissions `POST` to a localhost API and append to `feedback.jsonl`. The agent reads that file directly and revises — no copy‑paste back into chat.
- **Auto‑opens** the finished site in your browser (toggle in `~/.config/webdoc/settings.json`).
- **Stays local** — loopback binding, OS‑assigned port, finite TTL, clean shutdown; no symlink escape.

## Requirements

- Python 3 (standard library only — no pip install needed).

## Install

Clone, then make the skill discoverable to your agent by symlinking it into the skills directory:

```bash
git clone https://github.com/oooseun/webdoc.git
ln -s "$(pwd)/webdoc" ~/.claude/skills/webdoc     # Claude Code
ln -s "$(pwd)/webdoc" ~/.codex/skills/webdoc      # Codex
```

The agent invokes the skill automatically when it's about to produce a report, dossier, research memo, or similar durable artifact. You can also drive the scripts by hand.

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

- `SKILL.md` — the full instruction set the agent follows: when to create vs. offer vs. skip, the decision matrix, the feedback rule, visual‑explanation guidance, and the quality bar.
- `scripts/create_site.py` — Markdown/report → unified site (`index.html`) plus the self‑contained `doc.html` export; handles `stepper`/`embed` blocks, conditional‑format cells, and `--css/--js/--asset` bundling.
- `scripts/serve_site.py` — start/stop/inspect a localhost‑only preview server with durable `feedback.jsonl` and config‑driven auto‑open.
- `scripts/templates.py` — list built‑in templates and save a stylesheet as a private reusable category.
- `scripts/gallery.py` — assemble a one‑page concept switcher over several built sites (the concept‑variations workflow).
- `scripts/settings.py` — reads user config from `~/.config/webdoc/settings.json`.
- `templates/standard/style.css` — the `standard` theme; `assets/stepper.js` — the click‑to‑step interaction primitive.
- `references/avoid-ai-writing.md` — the full anti‑AI‑writing ruleset, applied to all generated prose.
- `references/presenter-role.md` — role prompt for a narrow "presenter" subagent that lays out and verifies the site without touching the analysis.
- `references/research-basis.md` — the research and source map the design is built on.
- `agents/openai.yaml` — Codex skill interface metadata.

## Design principles

- The source document is canonical; the website is a view, never a rewrite.
- Website input must become agent‑readable local state — never make a human copy‑paste feedback back into chat.
- Static output over dev servers; build tools only when real interactivity is needed.
- Local‑only by default. A local site may carry secrets, so never host or share one off the machine without explicit intent.

## Attribution

webdoc embeds the anti‑AI‑writing ruleset from [conorbronsdon/avoid-ai-writing](https://github.com/conorbronsdon/avoid-ai-writing) (MIT) — its operational core is inlined in `SKILL.md` and the full text is bundled verbatim as `references/avoid-ai-writing.md`. See [`NOTICE`](NOTICE).

## License

MIT — see [LICENSE](LICENSE). Bundled third‑party material retains its own license; see [`NOTICE`](NOTICE).
