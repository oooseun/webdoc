# Diagrams and interactive visuals in webdoc

How to add diagrams and explorables to a webdoc so they look like one professional family, render offline from `file://`, survive the Google-Docs export, and can be built reliably by subagents. Captured from the 2026 diagramming research sweep plus a production build of nine technical diagrams (torus topology, dataflow steppers, log-scale magnitude charts, a live-slider explorable).

Load this when a webdoc would benefit from diagrams. Default to diagrams whenever a relationship is structural, comparative, a flow, a mechanism, a magnitude, or a change over time. Skip them for prose that is genuinely linear.

## The one rule that prevents most failures

Every diagram is a self-contained, scoped, offline HTML fragment dropped into one webdoc `embed` block. It must:

- render with no network (no CDN, no web fonts required, no `fetch`),
- scope every id and CSS class under a per-diagram prefix so 9 diagrams on one page never collide,
- read with JavaScript off, then enhance with JS (see "Static fallback by diagram type" for what "read" means per type),
- put the claim-sentence title in a real HTML element above the SVG (a `<p>`/`<h*>`), not only in an SVG `<text>` node, so it survives the JS-off frame and the document outline,
- use direct on-figure labels, not a separate legend. A color-swatch legend never counts as a label, and never as the second channel for a filled mark.

If a diagram needs a chart library, pre-render it to SVG at build time and inline the SVG. Do not ship client-side Mermaid/D2/Graphviz bundles.

## Tool per job

| Job | Tool | How it ships |
|---|---|---|
| Structure / mechanism / topology (torus, datapath, CAM lifecycle) | hand-authored inline SVG + ~30 lines vanilla JS | inline, default choice |
| Nested block / containment (chip → tile → router) | D2 | `d2 in.d2 out.svg`, inline the SVG |
| Mesh/torus auto-layout seed | Graphviz `neato`/`fdp` `-n` | pre-render to SVG, then hand-tune |
| Sequence / handshake | Mermaid `sequenceDiagram` | `mmdc` to SVG, inline |
| Decision tree (≤15 nodes) | Mermaid `flowchart TD` | `mmdc` to SVG, inline |
| Quantitative chart (bars, log-scale, proportions) | Observable Plot (local UMD) or hand SVG | inline data, local bundle, or hand SVG |
| Parameter sensitivity | native `<input type=range>` + inline SVG, pure-JS recompute | inline |
| Clock/pipeline timing | WaveDrom (local) or a CSS-grid stage×cycle | inline |
| Bit-field / coordinate-exact schematic | Typst + CeTZ | `typst compile --format svg`, inline |

Hand-authored SVG is the workhorse. Reach for a tool only when auto-layout genuinely earns its keep, and always pre-render.

## The style system (Okabe-Ito)

One visual grammar across every figure. Colorblind-safe; color is never the only channel, always paired with line style, shape, or a direct label.

Palette:
- data / activation path: blue `#0072B2` (solid edges)
- control / gate / config: orange `#E69F00` (dashed `4 2`)
- compute / expert: green `#009E73` (fill tint `#E5F4EF`)
- multicast / replication / the one focus accent per view: vermillion `#D55E00`
- secondary / combine / Z-axis: reddish-purple `#CC79A7`
- light data / hover: sky `#56B4E9`
- neutrals: dimmed `#94A3B8`, node border `#CBD5E1`, gridline / unfilled track or rail (a bar's empty channel, remaining-capacity background) `#E2E8F0`, ink `#1A1A1A`, axis text `#475569`. Do not reach for Tailwind slate (`#F1F5F9`) or other off-palette neutrals.
- sequential scalar field (probability, congestion): Viridis stops `#440154 #414487 #2a788e #22a884 #7ad151 #fde725`. Never rainbow/jet.
- avoid yellow `#F0E442` on white (low contrast).

Coloring marks (hue must encode something the label does not):
- Single-series magnitude chart (every bar is the same quantity): do not color bars categorically for decoration. Use one data hue. Reserve vermillion for at most one focus bar. If the bars are an ordered scalar (e.g. increasing latency), use the Viridis stops, never a mix of categorical Okabe-Ito hues.
- Filled regions (bars, stacked segments, tiles): the "color is never the only channel" rule applies to fills, not just strokes. Pair each fill with a direct in-mark text label (e.g. `miss 10 ns` on the segment) or a hatch/pattern. An external color-swatch legend does not satisfy the second channel, and small multiples should replace a legend, not stack one on top.
- Magnitudes on a log axis use a dot/lollipop, never a filled bar. A bar drawn from a baseline on a log axis is misleading: its length is not proportional to the value and it implies a false zero (a 90 ns bar looks half a 500,000 ns bar). Place a marker at the value with an optional thin neutral stem. Filled bars are only for linear axes.
- When an ordered series is Viridis-encoded, every hue already encodes magnitude, so do not recolor a focus mark and never invent an off-palette color for it (no ad-hoc gold). Highlight the focus with a non-hue channel: a bold direct label, a 1px `#1A1A1A` outline, or a callout. The light Viridis stops (`#7ad151`, `#fde725`) are low-contrast on white; map the most important value to a darker stop, truncate the ramp to dark-to-mid, or give a light mark a 1px `#1A1A1A` outline so the focal value is never the least legible.

Type: `'IBM Plex Sans',ui-sans-serif,system-ui,-apple-system,sans-serif`; mono `'IBM Plex Mono',ui-monospace,SFMono-Regular,monospace`. Do not `@import` fonts; rely on fallbacks. Three working levels: title (a complete claim sentence) 15-16px / weight 600 / `#1A1A1A`; label 11-12px; axis/unit 10px / `#475569`. Never below 9px. Bold only the 1-2 key numbers. One optional fourth level is allowed: a single hero metric in an explorable (28-40px / weight 600, the one focal number the slider drives), and nothing else at that size.

Strokes: data 2px solid + arrowhead; control 1.5px dashed orange `4 2`; wraparound/fallback 1.5px curved lighter arc; traffic-volume thickness mapped 1-6px to the number, with the number printed inline; container 1px `#CBD5E1`; node 1.25px; gridline 0.5px. Reference / value / threshold marker lines use a thin dotted neutral (`#475569`, `1px`, `stroke-dasharray:2 2`), visually distinct from the orange control-dashed stroke, so an annotation line is never read as a control signal. Arrowheads only for true directed dependency, not to lead the eye (use left-to-right layout and proximity for that).

Layout: flat geometry, corners ≤2px, no gradients/shadows/3D. 8px base grid, ≥16px container padding, ≥24px between siblings, max two enclosure levels per view. Responsive via `viewBox`. Title states the claim ("Multicast replicates at branch routers, cutting source injections K-fold"), not a noun phrase. One callout per insight, adjacent to its referent. Annotate key numbers directly on the element; pick one unit convention and never mix. Exception: on a log axis spanning many orders of magnitude, label each tick and bar in its natural unit (ns / µs / ms), provided every tick and annotation prints its unit and the axis title carries "(log scale)"; this avoids unreadable labels like `500,000 ns`.

## Interaction patterns

Reserve JavaScript for two cases. Everything else is static.

1. Click-to-advance stepper (additive reveal). For a mechanism or flow with a natural order. The reader clicks Next; nothing loops. Each step keeps prior reveals, dims them to ~30%, and highlights the new action in vermillion. Provide Prev / Next / Reset and a "Step n / N" readout with a one-line description. Static fallback: the final fully-revealed state renders in markup; JS resets to step 1 and adds controls.
2. One live slider (explorable). For parameter sensitivity where feeling the curve is the point. Native `<input type=range>`, pure-JS recompute, no library. The driven number may use the optional hero-metric type level. Static fallback: two small-multiple states at the reference values, annotated, sharing the live view's scale/ticks and computed from the same formula (see "Static fallback by diagram type").

Supporting forms: small multiples (CSS grid) for same-scale comparison (beats a legend-mediated overlay); hover-to-highlight a node and its edges (enhancement only, must degrade to a fully-visible graph). Avoid scrollytelling for fewer than three steps; a stepper is simpler and lets the reader go backward. Never use looping CSS keyframes or GIFs for technical content.

## The embed contract (give this verbatim to a builder subagent)

Each diagram is built by one agent that returns only the fragment. The contract:

- Return only the HTML fragment, no markdown fences, no prose around it.
- Wrap in `<figure class="diag" id="DID-root"> … </figure>` where DID is the diagram id (e.g. `d4`). Use `<figure>` (not `<div>`) so the caption is valid HTML.
- Title as the first rendered child: `<p class="DID-title">…the exact claim sentence…</p>` (a real HTML element, never an SVG `<text>` as the only title). A single scoped `<style>` may precede it; "first child" means the first flow/rendered element is the title.
- Put all CSS in one `<style>` with every selector scoped under `#DID-root`. Prefix every id and class with DID (`id="d4-step2"`, `class="d4-link"`). No bare/global selectors.
- Inline `<svg viewBox="0 0 W H" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;max-width:780px">`. Use viewBox units, not fixed px.
- If interactive: one `<script>(function(){ … })();</script>`. Open it with the canonical scoped scaffold and use it for every lookup:
  ```js
  var root = document.getElementById('DID-root');
  var $ = function (s) { return root.querySelector(s); };
  var $$ = function (s) { return root.querySelectorAll(s); };
  ```
  `getElementById` is acceptable only with DID-prefixed ids; never reach unprefixed global ids. No network, no required web fonts.
- Static fallback present, per "Static fallback by diagram type" below.
- End with `<figcaption class="DID-cap">` one sentence (valid because the wrapper is a `<figure>`). One sentence means one period: semicolon or parenthetical clauses are fine, a second full sentence is not. Example: "Access latency on a log axis, each tier marked at its value (1 ns L1 to 500 µs network RPC)."
- Follow the style system above. Use the exact title sentence supplied.

### Static fallback by diagram type

"Reads with JS off" means something different per type. Be explicit so the builder does not improvise:

- Stepper / mechanism / topology: render the final fully-revealed state in static SVG markup; the JS resets to step 1 and adds controls. The figure is fully readable with JS off.
- Slider / explorable: render small multiples at the reference values (e.g. h=90% and h=99%) in static markup, computed from the same formula and units as the live recompute, and sharing the live view's horizontal scale, ticks, and labels so the JS-on transition is continuous. Cross-check the fallback numbers against the live math. Three continuity rules that catch silent jumps: (1) when the live recompute moves a labeled mark, update the label's `textContent` too, not only its geometry, so an on-mark number never degrades to a bare word ("miss" instead of "miss 10 ns"); (2) tick label text is identical in both frames, including the zero tick (both "0 ns", do not drop the unit in one view); (3) same decimal places in both (both `toFixed(2)`), so formatting does not jump. Each small-multiple panel may carry its own hero metric (one per panel, a few px smaller than the live hero); this does not break the single-hero rule, which governs the live view.
- Quant chart whose marks are entirely JS-loop-drawn: you cannot keep the bars in static SVG and also draw them by loop. Ship a `<noscript>` table listing the series as the fallback. Note the trade-off: `<noscript>` covers JS-disabled but not JS-broken (a thrown script leaves a blank frame), so keep the script trivial and the title in HTML so at least the claim survives.

### Token-budget gotcha (learned the hard way)

A subagent that hand-writes a fragment past the 32k output-token cap fails outright with nothing returned. This is not chart-specific: a stepper that hand-authors each stage's full SVG blows it just as a chart that hand-lists every gridline does. Two rules:

1. Keep the whole fragment small. Target ~7 KB; a fragment over ~12 KB is a warning sign. If you are typing near-identical elements, you are doing it wrong.
2. Generate all repeated geometry from a small data array with a JS loop inside the IIFE, never as hundreds of literal `<line>`/`<rect>`/`<g>` elements. This covers log axes, grids, bar arrays, torus link lattices, and stepper stages alike. For a stepper, write one parameterized stage renderer driven by a `STAGES` array; do not paste each stage. Hand-place only the bespoke geometry that genuinely differs.

This keeps every fragment well within the cap and makes the diagram easier to edit.

### Stepper recipe (draw the scene once)

A stepper is the most common way to blow the cap, because a builder draws the scene five times, once per step, often with detailed glyphs. Do not. Draw the scene a single time and let each step only toggle classes. Use labeled boxes, not detailed icons (a box reading "ALU" beats a drawn ALU). Skeleton:

```html
<figure class="diag" id="dX-root">
  <p class="dX-title">…claim sentence…</p>
  <svg viewBox="0 0 760 200" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;max-width:780px">
    <!-- draw ALL stages ONCE; give each a stable id -->
    <g id="dX-s0">…</g> <g id="dX-s1">…</g> … <g id="dX-s4">…</g>
  </svg>
  <div class="dX-ctrls"><button class="dX-prev">← Prev</button>
    <span class="dX-num">Step 1 / 5</span><button class="dX-next">Next →</button></div>
  <figcaption class="dX-cap">…one sentence…</figcaption>
  <script>(function(){
    var root=document.getElementById('dX-root'), $=function(s){return root.querySelector(s)};
    var STAGES=[                      // text only; the SVG is already drawn
      {desc:'Fetch the instruction.'},{desc:'Decode and read registers.'},
      {desc:'Execute in the ALU.'},{desc:'Access memory.'},{desc:'Write back.'}];
    var i=0, n=STAGES.length;
    function render(){ for(var k=0;k<n;k++){ var g=$('#dX-s'+k);
        g.setAttribute('opacity', k< i?0.3 : k===i?1 : 0.12); }   // past dim, current full, future faint
      $('.dX-num').textContent='Step '+(i+1)+' / '+n; }
    $('.dX-next').onclick=function(){ i=(i+1)%n; render(); };
    $('.dX-prev').onclick=function(){ i=(i-1+n)%n; render(); };
    render();
  })();</script>
</figure>
```

The static fallback is automatic: with JS off, every `<g>` is at full opacity, so the final all-stages-revealed scene shows. Build steppers and explorables with a capable model (see "Build many diagrams in parallel"); the recipe keeps even a smaller model under the cap, but the interactive ones still come out best on the stronger tier.

## Build many diagrams in parallel

For a set of diagrams, fan out one builder agent per diagram (a workflow `parallel`/`pipeline`, or parallel Agent calls). Hand each the same style system plus a precise per-diagram data spec and the embed contract. Use a capable model for the interactive/geometric ones (torus, steppers, explorables) and a cheaper one for static bars and trees. Then assemble the returned fragments into one showcase markdown, each in its own `embed` block, and build the site. This produced nine consistent diagrams in one pass; the two failures were the token-budget gotcha above, fixed by switching those to JS-loop drawing.

## Verify before showing a human

Treat correctness as a build artifact, not a vibe.

1. Spec first: write the real node/edge/data set (the actual topology, routes, numbers) before generating.
2. Generate source as text only (SVG/D2/Mermaid/DOT); never an LLM-emitted raster. Decompose past ~15 nodes / ~30 edges into L0/L1/L2 views; LLMs hallucinate plausible-but-wrong edges above that.
3. Parse/render gate: load the assembled page in a real browser (Playwright/Chrome MCP). Check the console for errors.
4. Structural check: for each diagram root, count SVG children (non-zero, sensible), confirm interactive controls exist (buttons for steppers, `input[type=range]` for sliders), and that no diagram is blank.
5. Functional check: drive the slider and read the live output back; confirm in-mark numeric labels update their text (not just that bars resize) and match the math; click the stepper through all N steps and confirm the step label advances and reveals change.
6. Design pass: Tufte data-ink (delete marks that encode nothing), CVD simulation, and confirm the static fallback reads with JS off.

A blank page is usually a stray template reference or an id collision; the scoping and static-fallback rules above prevent most of it.

## Google-Docs export

The webdoc `doc.html` export drops custom JavaScript and interactive embeds. For a Doc that keeps the visuals, render each diagram to a 2× PNG at build time (`mmdc -s 2`, `d2 --scale 2`, or screenshot the inline SVG) and provide those as the Doc-side fallback. Commit source + SVG + PNG together so rebuilds are deterministic; pin tool versions.

## Quick checklist

- [ ] `<figure class="diag" id="DID-root">` wrapper; title in an HTML `<p>` above the SVG; `<figcaption>` at the end
- [ ] every id/class prefixed and scoped under `#DID-root`; IIFE uses the `root`/`$` scaffold
- [ ] no network: no CDN, no web fonts required, no `fetch`
- [ ] static fallback correct for the type (final state / small multiples / noscript table)
- [ ] Okabe-Ito palette; hue encodes something the label does not; fills carry a direct label, not a legend swatch
- [ ] claim-sentence title; one unit (or per-tick units with "(log scale)"); reference lines dotted-neutral, not control-orange
- [ ] repetitive geometry (incl. stepper stages) drawn by JS loop; fragment ≲7 KB (token budget)
- [ ] rendered in a real browser, console clean, controls verified; explorable fallback numbers match the live math
- [ ] PNG fallback rendered if a Google-Docs export is needed
