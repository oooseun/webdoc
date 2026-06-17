---
name: webdoc
description: Use when creating, offering, opening, or updating documents, reports, dossiers, handoffs, project status files, research memos, comparison matrices, table-heavy analyses, interactive explainers, local HTML websites, or webdoc artifacts — including step-through visualizations and exports the user can open in Google Docs, plus review pages where the user enters feedback in the website that the agent reads from durable local storage.
---

# Webdoc

webdoc is a lightweight static-site generator for durable written work. Prose and interactive components live in **one source artifact** from the start, rendered into a local website you can read, iterate on, and export. The source document stays canonical unless the user explicitly promotes the website as canonical.

## Default Behavior

- Always consider a website companion when offering a document, report, dossier, long research answer, handoff, or project status update.
- Autocreate and open a localhost preview for durable, long, visual, table-heavy, source-heavy, or repeatedly revisited artifacts unless the user requested text-only output.
- Apply the **Avoid AI Writing** rules (below) to every word you write into a site or doc.
- Prefer dense tables to bullets, and include the exact code snippet whenever you explain or refer to code.
- For anything that needs to show change over time (a clock cycle, a pipeline, an algorithm), build a **click-to-step state machine**, never a looping animation.
- If the agent wants the user to give feedback through the website, the feedback must save to durable local storage that the agent can read directly. Do not make the user copy/paste website input back into chat.
- Offer, but do not create, for short notes, email drafts, sensitive content, or cases where a second artifact could confuse provenance.
- Do not create a website when previewing would require network exposure beyond localhost, untrusted remote assets, or background services that cannot be cleaned up.
- Prefer static output. Use build tools only when the site needs real interactivity beyond what the `stepper`/`embed` blocks provide.

## Workflow

1. Identify the canonical source path and intended audience. If there is no file yet, create the canonical document first.
2. Decide create vs offer vs skip using the matrix below.
3. Plan the UI/UX of any interactive element **before** writing it. State the question each component answers and the simplest interaction that answers it. Do not spend tokens on hundreds of lines of custom HTML/CSS before this is settled.
4. Build the site:

   ```bash
   python3 scripts/create_site.py path/to/report.md
   ```

5. If the site was autocreated, user feedback is requested, or the user needs a hosted preview, start a localhost server (it auto-opens the browser unless turned off — see Auto-Open):

   ```bash
   python3 scripts/serve_site.py start ~/agent-artifacts/sites/<artifact-id>
   ```

6. Verify `index.html` exists. If a server is started, verify the reported `http://127.0.0.1:<port>/` URL returns successfully.
7. Report the canonical document path, website path, localhost URL when running, the `doc.html` export path and its Google Docs steps, any dropped features, the feedback storage path when enabled, and the cleanup command. Never kill a process by port alone.

## Decision Matrix

Autocreate and open the website when:
- The artifact is a report, dossier, handoff, research memo, project status file, or master summary.
- The artifact has dense tables, source lists, charts, comparisons, step-through explanations, or sections the user will scan repeatedly.
- The user asks for a website, dashboard, artifact, visual presentation, polished document, interactive explainer, or local preview.
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

## Avoid AI Writing (always apply)

Write so the prose does not read as machine-generated. This is the operational core; the full ruleset (P0/P1/P2 severity tiers, ~80 patterns, context and voice profiles) is in `references/avoid-ai-writing.md` — apply it. *Embedded from [conorbronsdon/avoid-ai-writing](https://github.com/conorbronsdon/avoid-ai-writing) (MIT); see `NOTICE`.*

- **Em dashes:** target zero, hard max one per 1,000 words — in headings too. Rewrite as commas, periods, or parentheses, or split into two sentences. Catch both `—` and `--`.
- **No bold spam:** at most one bolded phrase per major section, or none. If it matters, restructure the sentence to lead with it.
- **Headings:** sentence case, no emoji. Use headers that say something specific, not "Overview / Key Points / Conclusion."
- **Bullets:** convert bullet-heavy prose into paragraphs. Bullets only for genuinely list-like content (this skill's own rule lists qualify).
- **Kill the formulae:** no "It's not X, it's Y"; no hollow intensifiers (`genuine`, `truly`, `really`, `it's worth noting that`); no hedging (`perhaps`, `could potentially`); vary the compulsive rule of three.
- **Tier 1 words — always replace:** delve, leverage, utilize, robust, comprehensive, seamless, embark, paradigm, realm, landscape (metaphor), tapestry, beacon, testament to, cutting-edge, pivotal, meticulous, game-changer, deep dive, unpack, showcasing, in order to, due to the fact that, serves as, boasts, underscores.
- **Tier 2 — flag when 2+ cluster in a paragraph:** harness, navigate, foster, elevate, streamline, empower, facilitate, ecosystem, myriad, plethora, crucial, catalyze, reimagine, transformative, cornerstone.
- **Tier 3 — flag at high density:** significant, innovative, effective, dynamic, compelling, unprecedented, exceptional, sophisticated.
- **Transitions to cut:** Moreover / Furthermore / Additionally; "In today's …"; "It's worth noting that" / "Notably"; "In conclusion / In summary"; "When it comes to"; "At the end of the day"; "That said."
- **Rhythm:** vary sentence length (mix 3–8 word sentences with 20+) and paragraph length. No formulaic openings ("In the rapidly evolving world of…"). No generic future closers ("the future looks bright," "only time will tell").
- **No chatbot residue:** cut "Great question!", "I hope this helps", "Let's dive in," "In this article we will explore."
- **Attribution and copula:** cite a specific source or drop "experts believe / studies show"; prefer "is/has" over "serves as / boasts / represents."

Five principles for the rewrite: vary sentence length; be concrete (numbers, names, dates); have a voice; cut the neutrality; earn your emphasis. The replacement lists are defaults, not mandates — keep a flagged word when it is genuinely the right one.

## Dense Information

- Prefer **tables over bullets** to pass information densely — comparison matrices, spec sheets, bit-field/register tables. A table the reader can scan beats a wall of bullets.
- When one option has a clear advantage in a field, **bold the advantageous value** with `**…**` so the winner is obvious at a glance.
- When context warrants Google-Sheets-style good/bad signaling, lead a table cell with a marker: `[+]` renders green, `[-]` red, `[~]` amber. Example row: `| p95 latency | [+] **12 ms** | [-] 380 ms |`. These inline-style through to the Google Docs export.
- When you explain or refer to code, include the **exact snippet** in a fenced code block, not a paraphrase.

## Interactivity

Plan the UX first (see Workflow step 3). Then author components inside the single source with two fenced blocks.

**Step-through state machine** — for clock cycles, timelines, pipelines, algorithms. The reader clicks to advance one step; nothing loops. Steps are split on `---`:

````
```stepper title="Clock cycle walkthrough"
Cycle 1 — fetch the instruction at the program counter.
---
Cycle 2 — decode the opcode and read the source registers.
---
Cycle 3 — the ALU computes the result and writes it back.
```
````

webdoc generates the controls and includes `stepper.js`. In the doc export the steps flatten to a numbered list, so the information survives even without the interaction.

**Raw embed** — for audio/music, photo galleries, toggles, or any custom HTML/JS/CSS:

````
```embed
<audio controls src="assets/intro.mp3"></audio>
<details><summary>Show the full derivation</summary><p>…</p></details>
<div class="gallery"><img src="assets/a.jpg" alt="…"><img src="assets/b.jpg" alt="…"></div>
```
````

Embeds are injected verbatim (local-only; never host an embed that carries secrets off this machine). Bundle files with flags: `--asset clip.mp3` copies into the site's `assets/` (reference it as `assets/clip.mp3`); `--css custom.css` and `--js widget.js` bundle and link extra styles/scripts.

Never use looping CSS keyframes or GIFs for technical content — they are noisy and hard to read. A `stepper` is almost always the better tool.

## Concept Variations

For a substantial animation or rich interactive visual — or whenever the user asks — do not commit to the first idea. Generate at least five distinct concepts and let the user choose. Skip this for trivial bits (a single toggle, a fade); build those directly.

1. Plan the UX: write the question the visual answers and five genuinely different ways to answer it (discrete stepper, annotated timeline, before/after toggle, layered build-up, interactive diagram). Different approaches, not restyles of one.
2. Build each concept as its own webdoc site (a separate `create_site.py` run into its own dir). You can generate the concepts in parallel with subagents.
3. Assemble a gallery the user flips through on one page:

   ```bash
   python3 scripts/gallery.py --out ~/agent-artifacts/sites/<id>-gallery --title "Clock cycle" \
     --concept "Discrete steps=<site_a>" --concept "Annotated timeline=<site_b>" --concept "Before/after=<site_c>"
   ```

4. Serve the gallery dir (it auto-opens). The user clicks tabs to compare, pops any concept out full-screen, and presses "Choose this concept" — the pick lands in the gallery's `feedback.jsonl`.
5. Read `feedback.jsonl` for the `CHOSEN:` line, then build the final artifact around that concept.

## Doc Export And Fidelity

Every build also writes a single self-contained `doc.html` (all CSS inline, no scripts, images embedded as data URIs) for Google Docs.

- Tell the user the exact path: **upload `doc.html` to Google Drive, then right-click it → Open with → Google Docs.** (There is no reliable in-Docs "Import HTML"; the Drive route is the one that works.)
- `create_site.py` reports `doc_export.dropped_features`. When it is non-empty, **say so plainly in chat**: list which interactive/audio/video/custom-JS/image bits will not appear in the Google Doc. This is the "if we're going too crazy, tell the user" check — the website keeps everything; the Doc keeps text, tables (with bold + red/green), code, and headings.
- If the user needs the rich media in a portable file, note that **PDF can embed audio** (and more), though it plays only in some viewers such as Acrobat. Offer it as the richer-but-heavier alternative.

## Auto-Open

`serve_site.py start` opens the site in the browser when the server is up, so it lands in focus. The toggle lives in `~/.config/webdoc/settings.json` (user-owned, outside the repo):

```json
{ "auto_open": true }
```

Set `auto_open` to `false` to stop auto-opening. Per run, `--open` / `--no-open` override the config. Only loopback URLs are ever opened.

## Templates

A template is a complete stylesheet that swaps webdoc's look — color, type, spacing — without touching the content. Pick one with `--template`:

```bash
python3 scripts/create_site.py report.md --template standard
python3 scripts/create_site.py report.md --template ./fashion-theme.css   # try a deviation
```

Only `standard` ships with the skill. To deviate for a kind of work (a fashion-trends look, a finance look), author a complete stylesheet and build with `--template ./that.css`. If it works for the user, offer to save it as a reusable category:

```bash
python3 scripts/templates.py save fashion-trends --from ./that.css
python3 scripts/templates.py list
```

Saved categories live privately under `~/.config/webdoc/templates/<name>/` (never uploaded). Reuse one with `--template fashion-trends`; a team gets a cohesive look by copying that directory. Templates control theme only — structural/layout templates are out of scope for now.

## Persistent Website Feedback

Use the persisted-feedback pattern: the browser UI is not the source of truth; user actions POST to a localhost API and the server writes durable local state.

- Generated websites include a feedback form.
- When served with `scripts/serve_site.py`, `POST /api/feedback` appends to `feedback.jsonl` in the site directory.
- The agent reads `feedback.jsonl` directly after the user submits feedback. The user should not copy/paste the feedback into chat.
- Browser `localStorage`, unsent textarea contents, or DOM state are not durable enough.
- For richer workflows, use SQLite or another explicit local store, but keep the same rule: website input must become agent-readable local state.

## Visual Explanation Guidance

Use visuals to explain structure, comparisons, flows, mechanisms, time, magnitude, or tradeoffs. Do not add decorative charts.

- **Data charts:** prefer Vega-Lite, Observable Plot, or a small D3/SVG module with explicit data and chart spec, injected via an `embed` block.
- **Static system/process diagrams:** prefer Mermaid, Graphviz/DOT, D2, or hand-authored SVG when layout precision matters.
- **Change over time / mechanism:** use a `stepper` (click-to-advance), not animation. If motion is genuinely required, keep it user-triggered with a reduced-motion fallback and a static fallback frame — never an infinite loop.

Prompting rules: state the question each visual answers before choosing a form; specify data fields, units, encodings, sort order, and what to leave out; separate data transformation from visual encoding; render and critique the result for accuracy, labels, axes, contrast, and responsive sizing. Keep all visual source beside the website so future agents can edit it.

## Storage And Hosting

- Default generated websites live under `~/agent-artifacts/sites/<artifact-id>/`.
- The artifact id is stable per source path by default; use `--snapshot` when a time-stamped copy is desired.
- Each generated website includes `manifest.json` with source path, source hash, output path, generated time, title, feedback path, and the `doc_export` block.
- Hosted previews must bind to `127.0.0.1` by default. Require explicit user intent for LAN exposure.
- Use OS-assigned ports by default and write the resolved URL to `server.json`.
- Stop only servers recorded in a manifest and owned by this workflow. Never kill "whatever owns port 3000".
- Use TTLs for background servers. The serve script defaults to a finite lifetime and supports explicit `stop`.
- Avoid symlinks in generated website directories. Local static servers can follow symlinks and escape the intended tree.

## Scripts

- `scripts/create_site.py`: Convert a Markdown/report source into a unified static website (`index.html`, `style.css`, `manifest.json`, feedback UI) plus the self-contained `doc.html` export. Supports `stepper`/`embed` blocks, `[+]/[-]/[~]` table cells, `--template`, and `--css/--js/--asset` bundling.
- `scripts/serve_site.py`: Start, stop, inspect, or clean up a localhost-only preview server with `server.json`, durable `feedback.jsonl`, and config-driven auto-open.
- `scripts/templates.py`: List built-in templates and save a stylesheet as a private reusable category.
- `scripts/gallery.py`: Assemble a concepts gallery — one switcher page over several built concept sites — for the Concept Variations workflow.
- `scripts/settings.py`: Read user config from `~/.config/webdoc/settings.json`.

Useful commands:

```bash
python3 scripts/create_site.py report.md
python3 scripts/create_site.py report.md --out ./report_site --title "Research Report"
python3 scripts/create_site.py report.md --css custom.css --js widget.js --asset clip.mp3
python3 scripts/serve_site.py start ./report_site --ttl 7200   # auto-opens per config
python3 scripts/serve_site.py start ./report_site --no-open
python3 scripts/serve_site.py status ./report_site
python3 scripts/serve_site.py stop ./report_site
```

## Presenter Agent

Use a dedicated webdoc subagent after the main artifact is stable. Its job is presentation only: convert, lay out, verify, open, persist website feedback, and report the local URL and doc export. It must not rewrite the analysis, add claims, change conclusions, or invent citations.

No dedicated user-level `webdoc` subagent exists by default. If one is configured, prefer it; otherwise either agent (Claude Code or Codex) can spawn a narrow presenter subagent using `references/presenter-role.md` as the role prompt, passing only the canonical source path plus sensitivity/autocreate instructions.

## Quality Bar

- Preserve headings, tables, code blocks, links, and citations from the canonical document.
- Add a table of contents for multi-section docs.
- Use local CSS and local files only by default.
- Keep the page readable on desktop and mobile, with responsive tables and print styles.
- Make feedback save status visible in the website when feedback is enabled.
- Say clearly whether the website was created, merely offered, or skipped, and why — and what will not survive the Google Docs export.

## References

- `references/avoid-ai-writing.md`: Full Avoid AI Writing ruleset (verbatim, MIT, from conorbronsdon/avoid-ai-writing). Always apply it.
- `references/presenter-role.md`: Prompt and constraints for a dedicated webdoc presenter agent.
- `references/research-basis.md`: Research basis and official-doc source map.
