# webdoc editing mode — design spec

Status: approved 2026-06-28. Owner: webdoc skill.

## Goal

In a served webdoc, toggle an edit mode and fix prose in place. Each edit round-trips surgically into the canonical `.md` (which stays the source of truth). Human edits are marked so a later agent pass does not silently overwrite them.

## Locked decisions

| Area | Decision |
|---|---|
| Save target | Round-trip to the canonical `.md` |
| Editable | Paragraphs, headings, list items, table cells. Embeds, diagrams, stepper, fenced code = view-only |
| Mechanism | Inline WYSIWYG (contenteditable) in place |
| Formatting | Limited rich: bold, italic, inline code, link. Each maps 1:1 to Markdown; anything else is stripped on save |
| Mode | Explicit Edit / Done toggle; read view by default |
| Lint on save | Warn inline, never block (human override always wins) |
| Round-trip | Per-block source line-range + content hash |

## Components

### 1. create_site.py — emit block identity
When rendering an editable block, wrap it so the client can locate its source:
- `data-md-start`, `data-md-end`: 1-based inclusive source line range in the `.md`.
- `data-md-hash`: a stable hash (sha256, short) of the exact source slice `lines[start-1:end]`.
- `data-md-type`: one of `paragraph | heading | listitem | tablecell` (carry heading level / list marker info needed to re-wrap).
Non-editable blocks (embed/stepper/code) get `data-noedit`. Bundle `edit.js` and `edit.css` into the site (loaded but inert until Edit). The doc.html export is unaffected (no edit chrome).

### 2. edit.js — the in-page editor
Inert until the user clicks Edit. On Edit:
- Add a top banner ("Editing — saves to <file>") with a Done button and a live edited-block count.
- On hover, outline editable blocks; non-editable blocks show an inert cursor.
- Click a block: make it contenteditable, show a floating toolbar (Bold, Italic, Inline code, Link). Keyboard: Cmd/Ctrl+B/I, Cmd/Ctrl+Enter saves and blurs, Esc cancels.
- On blur or Cmd+Enter: serialize the block's inner HTML, POST to `/api/edit`. Show "saving…" then "saved ✓".
- On response: swap the block's rendered HTML, update its `data-md-*`, stamp an "edited" badge, render any lint warnings inline (non-blocking), and shift following blocks' `data-md-start/end` by `line_delta`.

### 3. serve_site.py — POST /api/edit
Guards: the connecting TCP peer must be loopback (the authoritative check — unspoofable, enforced even under `--allow-lan`), the `Host` header must also be loopback (DNS-rebinding defence), and the body is size-capped. There is no session key. Payload: `{start, end, hash, html, type}`.
1. Read `source_path` from the site manifest. Confirm it is the manifest's path and exists; never write elsewhere.
2. Read the `.md`. Slice `lines[start-1:end]`, hash it, compare to `hash`. Mismatch → `409 conflict` (do not write).
3. Convert `html` → Markdown via the whitelist converter (below).
4. Re-wrap by `type` (heading keeps its `#` level; list item keeps its marker + indent; table cell stays in its row/pipe structure; paragraph as-is).
5. Replace `lines[start-1:end]` with the new Markdown lines. Write the `.md` atomically (temp + rename).
6. Append to the override ledger (below).
7. Run the linter on the new block text; collect warning/suggestion findings (never block).
8. Re-render just that block to HTML. Return `{ok:true, new_html, new_hash, new_start, new_end, line_delta, lint:[{severity,rule,message}]}`.

### 4. HTML → Markdown converter (strict whitelist)
Walk the contenteditable DOM. Allowed: text nodes; `strong`/`b` → `**x**`; `em`/`i` → `*x*`; `code` → `` `x` ``; `a[href]` → `[text](href)`; `br` → newline. Any other element contributes only its `textContent` (formatting stripped). Collapse nested identical marks. Escape Markdown control chars in plain text where needed. Deterministic and total (never throws on unexpected input).

### 5. Override ledger + agent contract
- Sibling file `<source_stem>.edits.json` (e.g. `doc.md` → `doc.edits.json`): one entry per human-edited block carrying its identity (`start`/`end` for line-range blocks, or `line`/`cell` for table cells), plus `type`, `content_hash`, `edited_at`, and an `excerpt`. The upsert keys on block identity; `check_overrides` matches on `content_hash` alone, so it is resilient to line drift.
- `check_overrides(source_path, block_markdown)` helper: returns whether a block's current markdown hash is in the ledger (i.e. human-authored).
- Documented rule in SKILL.md: before any agent rewrites a block (e.g. a humanizer pass), it must call `check_overrides`; if the block is human-overridden and unchanged, it must not silently overwrite — flag or ask. This is the anti-clobber guarantee.

## Data flow
read → Edit → click block → edit inline → blur → POST /api/edit → server writes `.md` + ledger, lints, re-renders → page swaps the block, updates `data-md-*`, "saved ✓", "edited" badge, inline lint warnings; following blocks shifted by `line_delta`.

## Conflict / drift
The hash check is the concurrency guard. If the `.md` changed under the open tab (an agent edited it), the save returns `409` and the UI says "this block changed on disk — reload" instead of overwriting.

## Safety
Localhost-only: the write path is gated on the real loopback TCP peer (plus a loopback `Host` header for DNS-rebinding), not a session key; size-capped; writes confined to the manifest `source_path`; HTML sanitized through the whitelist (no script/style/arbitrary tags survive, and disallowed-scheme links are dropped). There is no CSRF token by design: a valid write needs the per-block `content_hash`, which a cross-origin page cannot read under the same-origin policy, and the server binds an OS-random loopback port. (A disallowed-scheme href that decodes to U+FFFD is left as a broken, non-executing link rather than stripped to text.)

## Testing
- Unit: HTML→md converter (nested/overlapping marks, links, code, disallowed tags stripped); line-range replace; hash compute/compare; ledger read/write; re-wrap per block type.
- Integration: edit a block → `.md` updated correctly → re-render matches; multi-edit line-delta accounting; 409 on stale hash.
- Override protection: a simulated agent regeneration skips/flags a ledgered block.
- Browser (playwright/claude-in-chrome): enter edit mode, edit a paragraph, confirm the `.md` on disk changed, the badge appears, a lint warning shows for a deliberately bad edit, and embeds are not editable.

## Scope / non-goals (v1)
In: edit existing editable blocks' text + table cell text inline. Out (v2): create/delete/reorder blocks, edit embeds/diagrams/code, multi-user or real-time collaboration, full rich text, image editing.

## Acceptance
Five independent Opus reviewers evaluate the implementation and the live behavior against this spec and must each be satisfied (no blocking issues) before the feature is considered done. Iterate rounds until 5/5.
