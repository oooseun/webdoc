# Interactive, image-backed review sites

When the artifact is a **multi-item review** (listings, candidates, findings, a catalog the
user will scan and react to item-by-item), the generic Markdown converter is not enough.
Build a **data-driven single-file site** instead. This recipe is distilled from the C8
part-out listings review (June 2026).

## Shape
- One `index.html` with all data embedded as JSON in a `<script type="application/json">`
  tag (escape `</` as `<\/`). Render cards client-side. No build step, works offline, and a
  future agent can regenerate it from the canonical JSON.
- Keep a canonical machine-readable `*.json` beside the site so the data and the page stay
  separable (the JSON is the source of truth; the HTML is a view).
- Add **search + sort + filter** controls for anything over ~8 items.

## Images (never serve originals or symlinks)
- Generate web-optimized assets into `<site>/assets/<slug>/`: a thumbnail (~440px) and a
  lightbox size (~1500px) per photo. Originals stay in place; the static server only sees
  the assets tree (symlinks can escape it — see Storage And Hosting).
- Write an `images_manifest.json` (item → slug → [{name, thumb, full}]) so the build is
  reproducible and a future agent can re-skin without re-encoding.
- Per item: a **featured image + thumbnail strip + lightbox** (←/→/Esc), not a flat grid.

## Default-but-overridable selection (the "hero" pattern)
General UX rule whenever something has a *recommended* option (a hero photo, a top pick, a
default variant): **highlight the recommendation, default the prominent view to it, and let
the user click around to override.** Don't hide the alternatives, and don't force the
default. In practice: badge the recommended thumbnail (e.g. "★ HERO"), set the featured
image to it on load, and let any thumbnail swap the featured view. Match the recommendation
to the asset list defensively (filename equality → endswith → trailing `_NNN.jpg` seq).

## Per-item structured feedback (so the agent can act)
- Each item gets quick-action **chips** (Approve / Change price / Better photo / Funnier / …)
  plus a free-text note. On save, POST to `/api/feedback` with the item identity packed in
  so entries are self-describing: `page` = item name, and `feedback` = a tagged blob like
  `"[ITEM] …\n[ACTIONS] …\n[NOTE] …"` (the serve script only persists `feedback`,
  `artifact_id`, `page`).
- **Echo saved feedback back into the card** (a 💬 count + the saved lines) by GETting
  `/api/feedback` on load — closes the loop so the user sees their input landed.
- The agent then reads `feedback.jsonl` directly and revises. The user never pastes back.

## Pipeline that worked
1. Stabilize the canonical data (`*.json`, often produced by a Workflow fan-out).
2. `build_assets.py`: encode thumbnails + lightbox images → `assets/`, write `images_manifest.json`.
3. `build_site.py`: read the JSON + manifest (+ any review/annotation data), emit one
   `index.html`. Re-runnable; injects extra data (e.g. photo-review tips) when present.
4. `serve_site.py start … --ttl N` (default TTL is finite — restart if it lapses; the URL
   may get a new OS-assigned port).
5. Verify HTTP 200 + a sample asset 200, then open.

## Annotated images
For sales/explanatory photos, a caption bar (splice a dark strip, annotate brand + the
one-line value prop) is a cheap, high-trust win. Arrows/circles on specific features need
the agent to read the image and place coordinates — do those for the few highest-value shots.
Keep annotated outputs in a sibling `_annotated/` dir, never overwriting originals.
