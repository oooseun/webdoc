// mermaid-init.js — render every ```mermaid fence webdoc emitted.
// create_site.py emits each diagram as <pre class="mermaid"> with its source
// HTML-escaped; this reads the source back via textContent (which un-escapes it)
// and swaps in the rendered SVG. Loaded deferred, after mermaid.min.js, so
// window.mermaid is defined. Inert on any page without the library or a diagram.
(function () {
  "use strict";

  if (typeof window === "undefined" || !window.mermaid) return;
  var mermaid = window.mermaid;

  // Manual render (startOnLoad false) so we control ids and error handling.
  // securityLevel "strict" keeps diagram-authored HTML/JS from executing.
  mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral" });

  async function renderAll() {
    var blocks = Array.prototype.slice.call(document.querySelectorAll("pre.mermaid"));
    for (var i = 0; i < blocks.length; i++) {
      var pre = blocks[i];
      var id = "webdoc-mermaid-" + i; // unique per diagram so several on one page work
      var src = pre.textContent; // textContent un-escapes the HTML-escaped source
      try {
        var result = await mermaid.render(id, src);
        var container = document.createElement("div");
        container.className = "mermaid-rendered";
        container.innerHTML = result.svg;
        if (pre.parentNode) pre.parentNode.replaceChild(container, pre);
      } catch (err) {
        // One bad diagram must not stop the others: keep the source <pre> visible,
        // flag it, and remove any orphan node mermaid left behind. On failure the
        // library may leave a stray element whose id is the render id (often
        // prefixed with "d"); drop it so no broken graphic lingers.
        pre.classList.add("mermaid-error");
        if (window.console && console.warn) console.warn("mermaid: render failed for " + id, err);
        [id, "d" + id].forEach(function (orphanId) {
          var orphan = document.getElementById(orphanId);
          if (orphan && orphan !== pre && orphan.parentNode) {
            orphan.parentNode.removeChild(orphan);
          }
        });
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { renderAll(); });
  } else {
    renderAll();
  }
})();
