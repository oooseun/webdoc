# webdoc

**Turn any document into a local website that presents the key information to your team — and collects their feedback back to your machine.**

`webdoc` is a skill for [Claude Code](https://code.claude.com/docs/en/skills) and [OpenAI Codex](https://developers.openai.com/codex/skills). Point it at a report, dossier, research memo, comparison matrix, project status, or handoff, and it renders a polished, navigable, local‑first website — tables, charts, diagrams, a table of contents — with an optional feedback form that writes responses to durable local storage the author can read directly. No cloud, no copy‑paste, no public exposure by default.

## Why

A long Markdown report is hard to scan and harder to gather sign‑off on. `webdoc` gives the document a presentation layer your team can read in a browser, and turns "what did everyone think?" into a file you can read instead of a thread you have to chase. The source document stays canonical; the website is a view of it.

## What it does

- **Renders** a Markdown/report source into a static site (`index.html`, `style.css`, `manifest.json`).
- **Serves** it on `127.0.0.1` only, on an OS‑assigned port, with a finite TTL and clean shutdown.
- **Captures feedback** — the browser UI is never the source of truth; submissions `POST` to a localhost API and the server appends them to `feedback.jsonl` in the site directory. The agent reads that file directly.
- **Stays local** — loopback binding by default, no external assets, no symlink escape, no "kill whatever owns port 3000."

## Requirements

- Python 3 (standard library only — no pip install needed).

## Install

Clone, then make the skill discoverable to your agent by symlinking it into the skills directory:

```bash
git clone https://github.com/oooseun/webdoc.git
ln -s "$(pwd)/webdoc" ~/.claude/skills/webdoc     # Claude Code
ln -s "$(pwd)/webdoc" ~/.codex/skills/webdoc      # Codex
```

The agent will invoke the skill automatically when it's about to produce a report, dossier, research memo, or similar durable artifact. You can also drive the scripts by hand.

## Usage

Create a site from a Markdown file:

```bash
python3 scripts/create_site.py report.md
python3 scripts/create_site.py report.md --out ./report_site --title "Research Report"
```

Serve it locally (loopback only, auto‑assigned port, 2‑hour TTL):

```bash
python3 scripts/serve_site.py start ./report_site --ttl 7200
python3 scripts/serve_site.py status ./report_site
python3 scripts/serve_site.py stop ./report_site
```

Read the team's feedback:

```bash
cat ./report_site/feedback.jsonl
```

## How it fits together

- `SKILL.md` — the full instruction set the agent follows: when to create vs. offer vs. skip, the decision matrix, the feedback rule, visual‑explanation guidance, and the quality bar.
- `scripts/create_site.py` — Markdown/report → static website with feedback UI.
- `scripts/serve_site.py` — start/stop/inspect a localhost‑only preview server with durable `feedback.jsonl`.
- `references/presenter-role.md` — role prompt for a narrow "presenter" subagent that lays out and verifies the site without touching the analysis.
- `references/research-basis.md` — the research and source map the design is built on.
- `agents/openai.yaml` — Codex skill interface metadata.
- `assets/report.css` — base stylesheet.

## Design principles

- The source document is canonical; the website is a view, never a rewrite.
- Website input must become agent‑readable local state — never make a human copy‑paste feedback back into chat.
- Static output over dev servers; build tools only when real interactivity is needed.
- Local‑only by default. A local site may carry secrets, so never host or share one off the machine without explicit intent.

## License

MIT — see [LICENSE](LICENSE).
