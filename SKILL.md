---
name: webdoc
description: Use when creating, offering, opening, or updating documents, reports, dossiers, handoffs, project status files, research memos, comparison matrices, table-heavy analyses, local HTML websites, or webdoc artifacts, including review pages where the user enters feedback in the website that the agent reads from durable local storage.
---

# Webdoc

Create and open a local website version of durable written artifacts. The source document remains canonical unless the user explicitly promotes the website as canonical.

## Default Behavior

- Always consider a website companion when offering a document, report, dossier, long research answer, handoff, or project status update.
- Autocreate and open a localhost preview for durable, long, visual, table-heavy, source-heavy, or repeatedly revisited artifacts unless the user requested text-only output.
- If the agent wants the user to give feedback through the website, the feedback must save to durable local storage that the agent can read directly. Do not make the user copy/paste website input back into chat.
- Include code-created visuals when they clarify the document: charts, static diagrams, and animated diagrams should be generated from inspectable source code/specs, rendered, and verified.
- Offer, but do not create, for short notes, email drafts, sensitive content, or cases where a second artifact could confuse provenance.
- Do not create a website when previewing would require network exposure beyond localhost, untrusted remote assets, or background services that cannot be cleaned up.
- Prefer static output over watch/dev servers. Use build tools only when the site needs real interactivity.

## Workflow

1. Identify the canonical source path and intended audience. If there is no file yet, create the canonical document first.
2. Decide create vs offer vs skip using the matrix below.
3. For static Markdown/report sources, run:

   ```bash
   python3 scripts/create_site.py path/to/report.md
   ```

4. If the site was autocreated, user feedback is requested, or the user needs a hosted preview, start a localhost server:

   ```bash
   python3 scripts/serve_site.py start ~/agent-artifacts/sites/<artifact-id>
   ```

5. Verify `index.html` exists. If a server is started, verify the reported `http://127.0.0.1:<port>/` URL returns successfully and open it in the available browser surface. If browser automation is unavailable, use the OS browser opener with approval or clearly provide the URL.
6. Report the canonical document path, website path, localhost URL when running, feedback storage path when enabled, and cleanup command. Never kill a process by port alone.

## Decision Matrix

Autocreate and open the website when:
- The artifact is a report, dossier, handoff, research memo, project status file, or master summary.
- The artifact has dense tables, source lists, charts, comparisons, or sections the user will scan repeatedly.
- The user asks for a website, dashboard, artifact, visual presentation, polished document, or local preview.
- The task is research-heavy and a navigable table of contents/source index helps review.
- The agent asks the user to review or give feedback on the artifact in a local website.

Offer first when:
- The artifact is under roughly one page.
- The content is private, legally/medically/financially sensitive, or provenance-sensitive.
- The user asked for a message, email, PR text, short recap, or paste-ready text.
- Creating files or starting a server would be surprising in the current task.

Skip when:
- The user explicitly says no files, no website, text only, or chat only.
- The output is transient debugging narration or a tiny command result.
- The website would need non-local hosting, public sharing, or external network assets. A local-only site may carry secrets; never place secrets in a site that must be hosted or shared off this machine.

## Persistent Website Feedback

Use the persisted-feedback pattern: the browser UI is not the source of truth; user actions POST to a localhost API and the server writes durable local state.

- Generated websites include a feedback form.
- When served with `scripts/serve_site.py`, `POST /api/feedback` appends to `feedback.jsonl` in the site directory.
- The agent reads `feedback.jsonl` directly after the user submits feedback. The user should not copy/paste the feedback into chat.
- Browser `localStorage`, unsent textarea contents, or DOM state are not durable enough.
- For richer workflows, use SQLite or another explicit local store, but keep the same rule: website input must become agent-readable local state.
- Use server-side validation and idempotency for multi-step decisions. For simple feedback notes, append-only JSONL is enough.

## Visual Explanation Guidance

Use visuals to explain structure, comparisons, flows, mechanisms, time, magnitude, or tradeoffs. Do not add decorative charts.

Default stack:

- Data charts: prefer Vega-Lite, Observable Plot, or a small D3/SVG module with explicit data and chart spec.
- Static system/process diagrams: prefer Mermaid, PlantUML, Graphviz/DOT, D2, or hand-authored SVG when layout precision matters.
- Animated explanations: prefer code-driven HTML/SVG/CSS/JS, Animated Vega-Lite for data transitions, or Manim when the point is mathematical/spatial explanation.

Prompting rules for the agent:

- State the question each visual answers before choosing a chart type.
- Specify data fields, units, encodings, sort order, annotations, and what should not be shown.
- Separate data transformation from visual encoding; keep transformed data inspectable.
- Give the model examples or a house template when aesthetics matter. Few-shot/example-guided prompting performs better than vague "make it pretty" prompts.
- Render and critique the result: check data accuracy, labels, axes, contrast, responsive sizing, and whether a reader can answer the intended question.
- Prefer small multiples, direct labels, annotations, and restrained palettes over novelty charts.
- Avoid pie/radar/3D charts unless the artifact has a clear reason and the data shape supports them.
- For animated diagrams, add play/pause controls, a reduced-motion fallback, and a static fallback frame.
- Keep all visual source files beside the website so future agents can edit them.

## Storage And Hosting

- Default generated websites live under `~/agent-artifacts/sites/<artifact-id>/`.
- The artifact id is stable per source path by default; use `--snapshot` when a time-stamped copy is desired.
- Each generated website must include `manifest.json` with source path, source hash, output path, generated time, title, and feedback path.
- Hosted previews must bind to `127.0.0.1` by default. Require explicit user intent for LAN exposure.
- Use OS-assigned ports by default and write the resolved URL to `server.json`. This prevents hostname and port clashes.
- Stop only servers recorded in a manifest and owned by this workflow. Never kill "whatever owns port 3000".
- Use TTLs for background servers. The serve script defaults to a finite lifetime and supports explicit `stop`.
- Avoid symlinks in generated website directories. Local static servers can follow symlinks and escape the intended tree.

## Scripts

- `scripts/create_site.py`: Convert a Markdown/report file into a static website with `index.html`, `style.css`, `manifest.json`, and feedback UI.
- `scripts/serve_site.py`: Start, stop, inspect, or clean up a localhost-only static preview server with `server.json` and durable `feedback.jsonl`.

Useful commands:

```bash
python3 scripts/create_site.py report.md
python3 scripts/create_site.py report.md --out ./report_site --title "Research Report"
python3 scripts/serve_site.py start ./report_site --ttl 7200
python3 scripts/serve_site.py status ./report_site
python3 scripts/serve_site.py stop ./report_site
```

## Presenter Agent

Use a dedicated webdoc subagent after the main artifact is stable. Its job is presentation only: convert, lay out, verify, open, persist website feedback, and report the local URL. It must not rewrite the analysis, add claims, change conclusions, or invent citations.

No dedicated user-level `webdoc` subagent exists by default. If one is configured, prefer it; otherwise either agent (Claude Code or Codex) can spawn a narrow presenter subagent using `references/presenter-role.md` as the role prompt, passing only the canonical source path plus sensitivity/autocreate instructions.

## Quality Bar

- Preserve headings, tables, code blocks, links, and citations from the canonical document.
- Add a table of contents for multi-section docs.
- Use local CSS and local files only by default.
- Keep the page readable on desktop and mobile, with responsive tables and print styles.
- Make feedback save status visible in the website when feedback is enabled.
- Say clearly whether the website was created, merely offered, or skipped, and why.

## References

- `references/presenter-role.md`: Prompt and constraints for a dedicated webdoc presenter agent.
- `references/research-basis.md`: Current research basis and official-doc source map from May 2026.
