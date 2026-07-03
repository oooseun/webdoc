// audit.js — render-time layout audit (webdoc).
//
// After the real render settles, audit the page for layout defects: page-level
// horizontal overflow, constrained-element overflow, clipped or visibly
// spilling text, and overlapping text. Findings are POSTed once per change to
// /api/audit, which appends them to feedback.jsonl for the agent; a clean page
// posts nothing (and one all-clear entry when a previously-reported page comes
// clean). Mechanics ported from lavish-axi's artifact SDK (MIT), adapted to
// webdoc: no iframe, no gate, block-identity anchors alongside CSS selectors.
(function () {
  "use strict";

  // Served context only: file:// opens and the doc.html export have no API.
  if (location.protocol !== "http:" && location.protocol !== "https:") return;

  var EPSILON_PX = 1;        // slack before anything is a finding
  var ERROR_PX = 4;          // overflow beyond this is error-severity
  var SETTLE_MS = 180;       // ResizeObserver quiet period
  var SETTLE_MAX_MS = 2000;  // hard cap on the settle wait
  var DEBOUNCE_MS = 50;
  var SETTLE_OBSERVED_MAX = 800; // documentElement/body + first N descendants
  var OVERLAP_CANDIDATES_MAX = 200;
  var SEEN_KEYS_MAX = 200;

  var SIG_KEY = "webdoc:audit:sig:" + location.pathname;
  var SEEN_KEY = "webdoc:audit:seen:" + location.pathname;

  var auditRun = 0; // generation counter: a newer schedule aborts older runs

  // ---- element filters ------------------------------------------------------

  // webdoc's own chrome (toolbar, pills, toasts, annotation UI, gap spacers)
  // must never be audited nor accepted as an overlap hit. An EXPLICIT list, not
  // a "webdoc-" class-prefix or [data-noedit] match: .webdoc-embed carries both
  // and is CONTENT - embeds are precisely where layout breakage lives (noedit
  // means "not editable", not "not content"). Keep in step with the chrome
  // edit.js appends to <body>.
  var CHROME_SELECTOR = [
    ".webdoc-edit-toggle", ".webdoc-ann-toggle", ".webdoc-edit-banner",
    ".webdoc-edit-notice", ".webdoc-toolbar", ".webdoc-link-pop",
    ".webdoc-status", ".webdoc-toast", ".webdoc-lint", ".webdoc-svg-input",
    ".webdoc-ann-card", ".webdoc-ann-panel", ".webdoc-gap"
  ].join(", ");

  function isChrome(el) {
    return !!(el.closest && el.closest(CHROME_SELECTOR));
  }

  function isVisible(el, style, rect) {
    if (rect.width <= 0 || rect.height <= 0) return false;
    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
  }

  function isScrollerX(style) {
    return style.overflowX === "auto" || style.overflowX === "scroll";
  }

  function hasReadableText(el) {
    return !!(el.textContent && el.textContent.trim());
  }

  function isTruncationIntentional(style) {
    if (style.textOverflow === "ellipsis") return true;
    var clamp = parseInt(style.webkitLineClamp, 10);
    return !isNaN(clamp) && clamp > 0;
  }

  // Walk from body, pruning intentional horizontal scrollers' subtrees (our
  // .table-wrap is overflow-x:auto, so wide tables are exempt by design) and
  // all webdoc chrome.
  function collectElements() {
    var out = [];
    function walk(el) {
      for (var i = 0; i < el.children.length; i++) {
        var child = el.children[i];
        if (isChrome(child)) continue;
        var style = getComputedStyle(child);
        if (isScrollerX(style)) continue; // prune: inside is intentionally scrollable
        out.push({ el: child, style: style });
        walk(child);
      }
    }
    if (document.body) walk(document.body);
    return out;
  }

  // ---- selectors + anchors ----------------------------------------------------

  // Short CSS path: <=5 segments, stop at the first #id, :nth-of-type only when
  // same-tag siblings make it ambiguous.
  function selectorFor(el) {
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && node !== document.body && parts.length < 5) {
      if (node.id) {
        parts.unshift("#" + (window.CSS && CSS.escape ? CSS.escape(node.id) : node.id));
        break;
      }
      var tag = node.tagName.toLowerCase();
      var seg = tag;
      var parent = node.parentElement;
      if (parent) {
        var sameTag = 0, index = 0;
        for (var i = 0; i < parent.children.length; i++) {
          if (parent.children[i].tagName === node.tagName) {
            sameTag++;
            if (parent.children[i] === node) index = sameTag;
          }
        }
        if (sameTag > 1) seg += ":nth-of-type(" + index + ")";
      }
      parts.unshift(seg);
      node = parent;
    }
    return parts.join(" > ") || el.tagName.toLowerCase();
  }

  // The nearest editable block's identity: a far better anchor for the agent
  // than a CSS path (hash-keyed, survives line drift, maps to the source).
  function blockFor(el) {
    var block = el.closest ? el.closest("[data-md-type]") : null;
    if (!block) return null;
    var d = block.dataset;
    if (d.mdType === "tablecell") {
      return { type: d.mdType, line: parseInt(d.mdLine, 10), cell: parseInt(d.mdCell, 10), hash: d.mdHash };
    }
    return { type: d.mdType, start: parseInt(d.mdStart, 10), end: parseInt(d.mdEnd, 10), hash: d.mdHash };
  }

  // ---- the checks ------------------------------------------------------------

  function makeCollector() {
    var seen = {};
    var findings = [];
    return {
      push: function (el, kind, overflowPx, severity) {
        var selector = el === document.documentElement ? "html" : selectorFor(el);
        var key = kind + ":" + selector;
        if (seen[key]) return;
        seen[key] = true;
        var finding = {
          selector: selector,
          kind: kind,
          overflowPx: Math.round(overflowPx * 10) / 10,
          viewportWidth: Math.round(window.innerWidth),
          severity: severity,
          _el: el // stripped before delivery; used for the ancestor-chain dedup
        };
        var block = el.closest ? blockFor(el) : null;
        if (block) finding.block = block;
        findings.push(finding);
      },
      list: function () {
        // One overflowing child bubbles the same scrollWidth into every
        // unconstrained ancestor. Keep the INNERMOST of a chain (closest to the
        // culprit) and drop ancestors reporting the same overflow. The tolerance
        // is relative because each ancestor adds its own padding/border to the
        // measurement (a 2200px overflow arrives as ~2230px one level up).
        var kept = findings.filter(function (f) {
          if (f.kind !== "element-scroll-overflow") return true;
          return !findings.some(function (g) {
            if (g === f || g.kind !== f.kind || !f._el.contains(g._el)) return false;
            var tolerance = Math.max(8, 0.05 * Math.max(f.overflowPx, g.overflowPx));
            return Math.abs(g.overflowPx - f.overflowPx) <= tolerance;
          });
        });
        kept.forEach(function (f) { delete f._el; });
        return kept;
      }
    };
  }

  function severityFor(overflowPx) {
    return overflowPx > ERROR_PX ? "error" : "warning";
  }

  function auditPageOverflow(out) {
    var doc = document.documentElement;
    var overflow = doc.scrollWidth - (window.innerWidth || doc.clientWidth);
    if (overflow > EPSILON_PX) {
      out.push(doc, "page-horizontal-overflow", overflow, severityFor(overflow));
    }
  }

  function auditElements(entries, out) {
    var spillCandidates = [];
    for (var i = 0; i < entries.length; i++) {
      var el = entries[i].el;
      var style = entries[i].style;
      var rect = el.getBoundingClientRect();
      if (!isVisible(el, style, rect)) continue;

      // Horizontal: scrollWidth > clientWidth only happens when something
      // CONSTRAINS the box, so auto-sized elements never false-positive.
      if (el.clientWidth > 0) {
        var hOver = el.scrollWidth - el.clientWidth;
        if (hOver > EPSILON_PX) {
          var clippedH = hasReadableText(el) &&
            (style.overflowX === "hidden" || style.overflowX === "clip") &&
            !isTruncationIntentional(style);
          if (clippedH) out.push(el, "clipped-text", hOver, "error");
          else out.push(el, "element-scroll-overflow", hOver, severityFor(hOver));
        }
      }

      // Vertical: intentional scrollers are fine; hidden/clip on text is a hard
      // clip; overflow:visible text SPILLS and is resolved to the innermost
      // culprit below (the spill bubbles into every unconstrained ancestor).
      // The floor is line-height-aware: font ascender/descender overhang puts a
      // few px of scrollHeight on perfectly healthy text (a heading measures
      // sh 73 / ch 67 with nothing wrong), while real clipped/spilled text is
      // off by the better part of a line.
      if (el.clientHeight > 0 && style.overflowY !== "auto" && style.overflowY !== "scroll") {
        var vOver = el.scrollHeight - el.clientHeight;
        var lineH = parseFloat(style.lineHeight);
        if (isNaN(lineH)) lineH = (parseFloat(style.fontSize) || 16) * 1.4;
        var vFloor = Math.max(ERROR_PX, lineH * 0.6);
        if (vOver > vFloor && hasReadableText(el) && !isTruncationIntentional(style)) {
          if (style.overflowY === "hidden" || style.overflowY === "clip") {
            out.push(el, "clipped-text", vOver, "error");
          } else {
            spillCandidates.push({ el: el, overflowPx: vOver, bottom: rect.bottom + vOver });
          }
        }
      }

      // Parent overflow: the element pokes out of its parent's CONTENT box.
      var parent = el.parentElement;
      if (parent && parent !== document.body && parent !== document.documentElement &&
          rect.width * rect.height > 1) {
        var box = contentBoxRect(parent);
        if (box) {
          var pOver = rect.right - box.right;
          if (pOver > EPSILON_PX) {
            var positioned = style.position === "absolute" || style.position === "fixed" ||
                             style.position === "sticky";
            out.push(el, "element-parent-overflow", pOver,
                     positioned ? "warning" : severityFor(pOver));
          }
        }
      }
    }

    // Innermost-spill dedup: drop any candidate that CONTAINS another with the
    // same bottom spill edge (within 1px).
    for (var a = 0; a < spillCandidates.length; a++) {
      var keep = true;
      for (var b = 0; b < spillCandidates.length; b++) {
        if (a === b) continue;
        if (Math.abs(spillCandidates[a].bottom - spillCandidates[b].bottom) <= 1 &&
            spillCandidates[a].el.contains(spillCandidates[b].el)) {
          keep = false;
          break;
        }
      }
      if (keep) {
        out.push(spillCandidates[a].el, "clipped-text", spillCandidates[a].overflowPx, "error");
      }
    }
  }

  function contentBoxRect(el) {
    var rect = el.getBoundingClientRect();
    if (rect.width <= 0) return null;
    var style = getComputedStyle(el);
    return {
      left: rect.left + (parseFloat(style.borderLeftWidth) || 0) + (parseFloat(style.paddingLeft) || 0),
      right: rect.right - (parseFloat(style.borderRightWidth) || 0) - (parseFloat(style.paddingRight) || 0)
    };
  }

  // Overlapping text without a pairwise loop: for each text leaf's line
  // fragments, hit-test 3 sample points and let the browser's own hit-testing
  // find whatever sits on top. Always warning severity.
  function auditOverlappingText(entries, out) {
    var candidates = [];
    for (var i = 0; i < entries.length && candidates.length < OVERLAP_CANDIDATES_MAX; i++) {
      var e = entries[i];
      if (e.el.children.length === 0 && hasReadableText(e.el) && e.style.position === "static") {
        var rect = e.el.getBoundingClientRect();
        if (isVisible(e.el, e.style, rect)) candidates.push(e.el);
      }
    }
    for (var c = 0; c < candidates.length; c++) {
      var el = candidates[c];
      var frags = el.getClientRects();
      if (!frags.length) frags = [el.getBoundingClientRect()];
      for (var f = 0; f < frags.length; f++) {
        var frag = frags[f];
        var area = frag.width * frag.height;
        if (area < 16) continue;
        var insetX = Math.min(4, frag.width / 4);
        var insetY = Math.min(4, frag.height / 4);
        var points = [
          [frag.left + frag.width / 2, frag.top + frag.height / 2],
          [frag.left + insetX, frag.top + insetY],
          [frag.right - insetX, frag.bottom - insetY]
        ];
        var hit = null;
        for (var p = 0; p < points.length && !hit; p++) {
          var x = points[p][0], y = points[p][1];
          if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) continue;
          var at = document.elementFromPoint(x, y);
          if (!at || at === el || at.contains(el) || el.contains(at)) continue;
          if (isChrome(at)) continue;
          var atStyle = getComputedStyle(at);
          if (atStyle.position !== "static") continue; // tooltips/overlays are deliberate
          if (!fragmentsSignificantlyOverlap(frag, at)) continue;
          hit = at;
        }
        if (hit) {
          out.push(el, "overlapping-text", 0, "warning");
          break; // one finding per element
        }
      }
    }
  }

  // Confirm with fragment-level geometry (per-line rects, not bounding boxes,
  // so wrapped inline text never false-positives).
  function fragmentsSignificantlyOverlap(frag, other) {
    var rects = other.getClientRects();
    if (!rects.length) rects = [other.getBoundingClientRect()];
    var need = Math.min(frag.width * frag.height * 0.25, 24);
    for (var i = 0; i < rects.length; i++) {
      var r = rects[i];
      var w = Math.min(frag.right, r.right) - Math.max(frag.left, r.left);
      var h = Math.min(frag.bottom, r.bottom) - Math.max(frag.top, r.top);
      if (w > 0 && h > 0 && w * h >= need) return true;
    }
    return false;
  }

  function audit() {
    var out = makeCollector();
    auditPageOverflow(out);
    var entries = collectElements();
    auditElements(entries, out);
    auditOverlappingText(entries, out);
    return out.list();
  }

  // ---- settle + scheduling ----------------------------------------------------

  function fontsReady() {
    try {
      if (document.fonts && document.fonts.ready) {
        return document.fonts.ready.catch(function () {});
      }
    } catch (e) { /* fall through */ }
    return Promise.resolve();
  }

  function resizeSettled() {
    return new Promise(function (resolve) {
      var observed = [document.documentElement, document.body];
      var all = document.body ? document.body.querySelectorAll("*") : [];
      for (var i = 0; i < all.length && i < SETTLE_OBSERVED_MAX; i++) observed.push(all[i]);
      var timer = null;
      var done = false;
      var ro = null;
      function finish() {
        if (done) return;
        done = true;
        if (ro) ro.disconnect();
        resolve();
      }
      function arm() {
        if (timer) clearTimeout(timer);
        timer = setTimeout(finish, SETTLE_MS);
      }
      try {
        ro = new ResizeObserver(arm);
        for (var j = 0; j < observed.length; j++) {
          if (observed[j]) ro.observe(observed[j]);
        }
      } catch (e) { finish(); return; }
      arm();
      setTimeout(finish, SETTLE_MAX_MS);
    });
  }

  function doubleRaf() {
    return new Promise(function (resolve) {
      requestAnimationFrame(function () { requestAnimationFrame(resolve); });
    });
  }

  var scheduleTimer = null;
  function schedule() {
    if (scheduleTimer) clearTimeout(scheduleTimer);
    var runId = ++auditRun;
    scheduleTimer = setTimeout(function () { run(runId); }, DEBOUNCE_MS);
  }

  function run(runId) {
    fontsReady()
      .then(resizeSettled)
      .then(doubleRaf)
      .then(function () {
        if (runId !== auditRun) return; // a newer run superseded this one
        deliver(audit());
      });
  }

  // ---- delivery ---------------------------------------------------------------

  function readStore(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) { return fallback; }
  }

  function writeStore(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* best-effort */ }
  }

  function deliver(findings) {
    // No overflowPx in the signature: the amount shifts with viewport width, and
    // a window resize must not re-post an otherwise-unchanged finding set.
    var signature = JSON.stringify(findings.map(function (f) {
      return f.kind + ":" + f.selector + ":" + f.severity;
    }));
    var lastSig = readStore(SIG_KEY, null);
    // Unchanged set (including still-clean): silence. A page that stays broken
    // does not re-spam feedback.jsonl on every open; a clean page costs nothing.
    if (signature === lastSig) { notify(findings); return; }
    if (!findings.length && lastSig === null) { writeStore(SIG_KEY, signature); notify(findings); return; }

    var seen = readStore(SEEN_KEY, []);
    var seenSet = {};
    for (var i = 0; i < seen.length; i++) seenSet[seen[i]] = true;
    var keys = [];
    for (var j = 0; j < findings.length; j++) {
      var key = findings[j].kind + ":" + findings[j].selector;
      findings[j].persistent = !!seenSet[key]; // the agent saw this and it is still here
      keys.push(key);
    }

    fetch("/api/audit", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        findings: findings,
        page: location.pathname,
        viewport: { w: window.innerWidth, h: window.innerHeight }
      })
    }).then(function (resp) {
      if (!resp.ok) return;
      writeStore(SIG_KEY, signature);
      writeStore(SEEN_KEY, seen.concat(keys).slice(-SEEN_KEYS_MAX));
    }).catch(function () { /* server gone: the next open retries */ });
    notify(findings);
  }

  // Let the editor chrome (edit.js) surface a note without coupling the files.
  function notify(findings) {
    var errors = 0;
    for (var i = 0; i < findings.length; i++) {
      if (findings[i].severity === "error") errors++;
    }
    try {
      document.dispatchEvent(new CustomEvent("webdoc:layout-findings", {
        detail: { errors: errors, warnings: findings.length - errors }
      }));
    } catch (e) { /* older browsers: telemetry already delivered */ }
  }

  // ---- triggers ---------------------------------------------------------------

  function start() {
    schedule();
    window.addEventListener("load", schedule, { once: true });
    window.addEventListener("resize", schedule, { passive: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
