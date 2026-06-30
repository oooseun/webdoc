// edit.js — webdoc editing mode (in-page WYSIWYG over the canonical Markdown).
//
// Inert until the reader clicks "Edit". Then: editable blocks (paragraph,
// heading, list item, table cell) carry data-md-* identity emitted by
// create_site; clicking one makes it contenteditable with a small formatting
// toolbar. On blur / Cmd+Enter the block's HTML is POSTed to /api/edit, which
// round-trips it surgically into the source .md, lints (advisory), re-renders,
// and returns the new HTML + shifted line range. Esc cancels. Read view and the
// existing feedback form are untouched. Loaded only from index.html, never the
// doc.html export.
(function () {
  "use strict";

  // Served context only: the round-trip needs the localhost API. On a file://
  // open (or the doc export) there is nothing to talk to, so stay fully inert.
  if (location.protocol !== "http:" && location.protocol !== "https:") return;

  var EDITABLE = "[data-md-type]";

  var state = {
    on: false,
    active: null,
    original: "" // innerHTML of the active block, for cancel + change detection
  };

  var toggleBtn = null;
  var banner = null;
  var countEl = null;
  var toolbar = null;
  var notice = null; // LAN "editing is local-only" explainer
  var linkPopover = null; // inline link add/edit/remove UI
  var savedRange = null;  // selection saved while the link input has focus

  // ---- small DOM helpers --------------------------------------------------

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function isEditable(node) {
    return node && node.nodeType === 1 && node.matches(EDITABLE) && !node.hasAttribute("data-noedit");
  }

  function pageRect(node) {
    var r = node.getBoundingClientRect();
    return {
      top: r.top + window.scrollY,
      left: r.left + window.scrollX,
      right: r.right + window.scrollX,
      bottom: r.bottom + window.scrollY,
      width: r.width
    };
  }

  // The write path (/api/edit) is loopback-only by design: it edits the source
  // file, so the server refuses it from any non-loopback peer even under
  // --allow-lan. Detect a LAN open up front so we can explain rather than fail.
  function isLoopbackHost() {
    var h = location.hostname;
    return h === "localhost" || h === "127.0.0.1" || h === "::1" ||
           h === "[::1]" || /^127\./.test(h);
  }

  function loopbackUrl() {
    var port = location.port ? ":" + location.port : "";
    return "http://127.0.0.1" + port + location.pathname + location.search + location.hash;
  }

  // Shown when Edit is used over a non-loopback (LAN) address: explain why
  // editing is local-only and give the exact loopback URL to use instead.
  function showLanNotice() {
    if (!notice) {
      notice = el("div", "webdoc-edit-notice");
      document.body.appendChild(notice);
    }
    notice.textContent = "";
    notice.appendChild(el("strong", null, "Editing runs only on the host machine"));
    notice.appendChild(el("p", null,
      "You are viewing this site over the network (" + location.host + "). The " +
      "editor writes back to the source file, so it works only on the computer " +
      "serving the site. Reading and feedback work fine from here."));
    var howto = el("p");
    howto.appendChild(document.createTextNode("To edit, open this page there at "));
    var link = el("a", null, loopbackUrl());
    link.href = loopbackUrl();
    howto.appendChild(link);
    howto.appendChild(document.createTextNode("."));
    notice.appendChild(howto);
    var close = el("button", null, "Got it");
    close.type = "button";
    close.addEventListener("click", function () {
      if (notice) { notice.remove(); notice = null; }
    });
    notice.appendChild(close);
  }

  // ---- toggle + banner ----------------------------------------------------

  function buildToggle() {
    toggleBtn = el("button", "webdoc-edit-toggle", "Edit");
    toggleBtn.type = "button";
    if (!isLoopbackHost()) {
      toggleBtn.title = "Editing runs only on the host machine (open via 127.0.0.1)";
    }
    toggleBtn.addEventListener("click", function () {
      if (state.on) exitEditMode();
      else enterEditMode();
    });
    document.body.appendChild(toggleBtn);
  }

  function buildBanner() {
    banner = el("div", "webdoc-edit-banner");
    var label = el("span");
    var src = document.body.getAttribute("data-webdoc-source") || "the source file";
    label.appendChild(el("strong", null, "Editing"));
    label.appendChild(document.createTextNode(" — saves to " + src));
    countEl = el("span", "webdoc-edit-count", "0 blocks edited");
    var spacer = el("span", "webdoc-spacer");
    var done = el("button", null, "Done");
    done.type = "button";
    done.addEventListener("click", exitEditMode);
    banner.appendChild(label);
    banner.appendChild(spacer);
    banner.appendChild(countEl);
    banner.appendChild(done);
    document.body.appendChild(banner);
  }

  function updateCount() {
    if (!countEl) return;
    var n = document.querySelectorAll("[data-webdoc-edited]").length;
    countEl.textContent = n + (n === 1 ? " block edited" : " blocks edited");
  }

  // ---- toolbar ------------------------------------------------------------

  function buildToolbar() {
    toolbar = el("div", "webdoc-toolbar");
    toolbar.hidden = true;
    var buttons = [
      ["bold", "B", "Bold (Cmd/Ctrl+B)"],
      ["italic", "I", "Italic (Cmd/Ctrl+I)"],
      ["strike", "S", "Strikethrough"],
      ["code", "<>", "Inline code"],
      ["link", "Link", "Link (add / edit / remove)"],
      ["clear", "Clear", "Clear formatting"]
    ];
    buttons.forEach(function (spec) {
      var b = el("button", null, spec[1]);
      b.type = "button";
      b.setAttribute("data-cmd", spec[0]);
      b.title = spec[2];
      // Keep the contenteditable selection/focus when a toolbar button is hit.
      b.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
      b.addEventListener("click", function (ev) {
        ev.preventDefault();
        runCommand(spec[0]);
      });
      toolbar.appendChild(b);
    });
    document.body.appendChild(toolbar);
  }

  function showToolbar(target) {
    if (!toolbar) return;
    toolbar.hidden = false;
    var r = pageRect(target);
    // Measure, then place above the block (fall back to below near the top).
    var tw = toolbar.offsetWidth;
    var th = toolbar.offsetHeight;
    var top = r.top - th - 8;
    if (top < window.scrollY + 4) top = r.bottom + 8;
    var left = r.left;
    var maxLeft = window.scrollX + document.documentElement.clientWidth - tw - 8;
    if (left > maxLeft) left = Math.max(window.scrollX + 8, maxLeft);
    toolbar.style.top = top + "px";
    toolbar.style.left = left + "px";
  }

  function hideToolbar() {
    if (toolbar) toolbar.hidden = true;
  }

  function runCommand(cmd) {
    if (!state.active) return;
    if (cmd === "bold") {
      document.execCommand("bold");
    } else if (cmd === "italic") {
      document.execCommand("italic");
    } else if (cmd === "strike") {
      document.execCommand("strikeThrough");
    } else if (cmd === "code") {
      toggleInlineCode();
    } else if (cmd === "link") {
      openLinkPopover();
    } else if (cmd === "clear") {
      clearFormatting();
    }
  }

  // -- selection helpers, scoped to the active block -----------------------

  // The Selection, but only when its range lives inside the block being edited.
  function blockSelection() {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    var r = sel.getRangeAt(0);
    if (state.active && state.active.contains(r.commonAncestorContainer)) return sel;
    return null;
  }

  // Nearest ancestor element with this tag, never escaping the active block.
  function closestTag(node, tagName) {
    tagName = tagName.toUpperCase();
    while (node && node !== state.active) {
      if (node.nodeType === 1 && node.tagName === tagName) return node;
      node = node.parentNode;
    }
    return null;
  }

  // Replace an element with its own children (drop the tag, keep the content).
  function unwrap(elemt) {
    var parent = elemt.parentNode;
    if (!parent) return;
    while (elemt.firstChild) parent.insertBefore(elemt.firstChild, elemt);
    parent.removeChild(elemt);
  }

  function wrapSelection(tagName) {
    var sel = blockSelection();
    if (!sel) return;
    var range = sel.getRangeAt(0);
    if (range.collapsed) return;
    var wrapper = document.createElement(tagName);
    try {
      range.surroundContents(wrapper);
    } catch (e) {
      var frag = range.extractContents();
      wrapper.appendChild(frag);
      range.insertNode(wrapper);
    }
    sel.removeAllRanges();
  }

  // Inline code toggles: a selection inside a <code> unwraps it; otherwise the
  // selection is wrapped. (Bold/italic/strike already toggle via execCommand.)
  function toggleInlineCode() {
    var sel = blockSelection();
    if (!sel) return;
    var code = closestTag(sel.anchorNode, "code") || closestTag(sel.focusNode, "code");
    if (code) { unwrap(code); return; }
    wrapSelection("code");
  }

  // Strip inline formatting from the selection: marks via removeFormat, links
  // via unlink, plus any <code> spans the selection touches (removeFormat keeps
  // those). The text itself is untouched.
  function clearFormatting() {
    var sel = blockSelection();
    if (!sel) return;
    document.execCommand("removeFormat");
    document.execCommand("unlink");
    sel = blockSelection();
    if (!sel || !sel.rangeCount) return;
    var range = sel.getRangeAt(0);
    // removeFormat leaves <code>, and does not touch <del> (what a saved-then-
    // reloaded strikethrough renders as) nor reliably <s>/<strike>. Unwrap them
    // explicitly so Clear works on freshly-struck and round-tripped text alike.
    var marks = state.active.querySelectorAll("code, del, s, strike");
    for (var i = marks.length - 1; i >= 0; i--) {
      if (range.intersectsNode(marks[i])) unwrap(marks[i]);
    }
  }

  // -- link popover (add / edit / remove) ----------------------------------

  // Mirror the server + html2md scheme policy: drop control/space chars, then
  // refuse javascript:/data:/vbscript:. The server strips these on save too;
  // this is just so the editor never shows a link it would then silently drop.
  function isUnsafeScheme(url) {
    var probe = url.replace(/[\u0000-\u0020]+/g, "").toLowerCase();
    return probe.lastIndexOf("javascript:", 0) === 0 ||
           probe.lastIndexOf("data:", 0) === 0 ||
           probe.lastIndexOf("vbscript:", 0) === 0;
  }

  function buildLinkPopover() {
    linkPopover = el("div", "webdoc-link-pop");
    linkPopover.hidden = true;
    var input = el("input", "webdoc-link-input");
    input.type = "text";
    input.placeholder = "https://…";
    var apply = el("button", "webdoc-link-apply", "Apply");
    var remove = el("button", "webdoc-link-remove", "Remove");
    var cancel = el("button", "webdoc-link-cancel", "Cancel");
    [apply, remove, cancel].forEach(function (b) {
      b.type = "button";
      // A mousedown inside the popover must not blur/commit the active block.
      b.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
    });
    apply.addEventListener("click", function () { applyLink(input.value); });
    remove.addEventListener("click", removeLink);
    cancel.addEventListener("click", closeLinkPopover);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); applyLink(input.value); }
      else if (ev.key === "Escape") { ev.preventDefault(); closeLinkPopover(); }
    });
    linkPopover.appendChild(input);
    linkPopover.appendChild(apply);
    linkPopover.appendChild(remove);
    linkPopover.appendChild(cancel);
    linkPopover.__input = input;
    linkPopover.__remove = remove;
    document.body.appendChild(linkPopover);
  }

  function openLinkPopover() {
    var sel = blockSelection();
    if (!sel || !sel.rangeCount) return;
    if (!linkPopover) buildLinkPopover();
    savedRange = sel.getRangeAt(0).cloneRange();
    var anchor = closestTag(sel.anchorNode, "a") || closestTag(sel.focusNode, "a");
    linkPopover.__anchor = anchor || null;
    linkPopover.__input.value = anchor ? (anchor.getAttribute("href") || "") : "";
    linkPopover.__input.classList.remove("webdoc-invalid");
    linkPopover.__remove.style.display = anchor ? "" : "none";
    linkPopover.hidden = false;
    var r = pageRect(state.active);
    var top = r.top - linkPopover.offsetHeight - 8;
    if (top < window.scrollY + 4) top = r.bottom + 8;
    linkPopover.style.top = top + "px";
    linkPopover.style.left = r.left + "px";
    linkPopover.__input.focus();
    linkPopover.__input.select();
  }

  function closeLinkPopover() {
    if (linkPopover) linkPopover.hidden = true;
    savedRange = null;
    if (state.active) state.active.focus();
  }

  function restoreSaved() {
    if (!savedRange) return null;
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(savedRange);
    return sel;
  }

  function applyLink(url) {
    url = (url || "").trim();
    var anchor = linkPopover && linkPopover.__anchor;
    if (!url) { // empty input: remove the link if editing one, else just close
      if (anchor) removeLink(); else closeLinkPopover();
      return;
    }
    if (isUnsafeScheme(url)) {
      linkPopover.__input.classList.add("webdoc-invalid");
      linkPopover.__input.focus();
      return;
    }
    if (anchor) {
      anchor.setAttribute("href", url);
      closeLinkPopover();
      return;
    }
    restoreSaved();
    wrapLink(url);
    closeLinkPopover();
  }

  function removeLink() {
    var anchor = linkPopover && linkPopover.__anchor;
    if (anchor) {
      unwrap(anchor);
    } else {
      restoreSaved();
      document.execCommand("unlink");
    }
    closeLinkPopover();
  }

  function wrapLink(url) {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    var range = sel.getRangeAt(0);
    var anchor = document.createElement("a");
    anchor.setAttribute("href", url);
    if (range.collapsed) {
      anchor.textContent = url;
      range.insertNode(anchor);
    } else {
      try {
        range.surroundContents(anchor);
      } catch (e) {
        anchor.appendChild(range.extractContents());
        range.insertNode(anchor);
      }
    }
    sel.removeAllRanges();
  }

  // ---- per-block status pill ---------------------------------------------

  function setStatus(target, kind, text) {
    var pill = target.__wdStatus;
    if (!pill) {
      pill = el("div", "webdoc-status");
      document.body.appendChild(pill);
      target.__wdStatus = pill;
    }
    pill.setAttribute("data-state", kind);
    pill.textContent = text;
    var r = pageRect(target);
    pill.style.top = Math.max(window.scrollY + 2, r.top - 26) + "px";
    pill.style.left = (r.right - pill.offsetWidth) + "px";
    if (target.__wdStatusTimer) clearTimeout(target.__wdStatusTimer);
    if (kind === "saved" || kind === "error") {
      var ms = kind === "saved" ? 1600 : 3200;
      target.__wdStatusTimer = setTimeout(function () {
        if (target.__wdStatus) {
          target.__wdStatus.remove();
          target.__wdStatus = null;
        }
      }, ms);
    }
  }

  // ---- lint findings (advisory, non-blocking) -----------------------------

  function lintAnchor(target) {
    if (target.dataset.mdType === "tablecell") {
      return target.closest(".table-wrap") || target;
    }
    return target;
  }

  function renderLint(target, findings) {
    if (target.__wdLint) {
      target.__wdLint.remove();
      target.__wdLint = null;
    }
    if (!findings || !findings.length) return;
    var list = el("ul", "webdoc-lint");
    findings.forEach(function (f) {
      var li = el("li");
      li.setAttribute("data-severity", f.severity || "warning");
      var rule = el("span", "webdoc-lint-rule", (f.rule || "lint") + ": ");
      li.appendChild(rule);
      li.appendChild(document.createTextNode(f.message || ""));
      list.appendChild(li);
    });
    var anchor = lintAnchor(target);
    anchor.parentNode.insertBefore(list, anchor.nextSibling);
    target.__wdLint = list;
  }

  // ---- activate / deactivate / commit ------------------------------------

  function activate(target) {
    state.active = target;
    state.original = target.innerHTML;
    state.cancelling = false;
    target.contentEditable = "true";
    target.classList.add("webdoc-active");
    target.classList.remove("webdoc-conflict");
    showToolbar(target);
  }

  function deactivate(target) {
    target.contentEditable = "false";
    target.classList.remove("webdoc-active");
    hideToolbar();
  }

  function cancelActive() {
    var target = state.active;
    if (!target) return;
    state.cancelling = true;
    target.innerHTML = state.original;
    deactivate(target);
    state.active = null;
  }

  function commit(target) {
    if (!target) return;
    deactivate(target);
    if (state.cancelling) {
      state.cancelling = false;
      return;
    }
    var htmlNow = target.innerHTML;
    if (htmlNow === state.original) return; // nothing changed

    // Snapshot the pre-edit HTML now: state.original is overwritten the moment
    // another block is activated, but this async save may resolve later. On any
    // failed save we restore the block to this, so it never lingers broken.
    var original = state.original;
    var type = target.dataset.mdType;
    var payload, oldEnd;
    if (type === "tablecell") {
      oldEnd = parseInt(target.dataset.mdLine, 10);
      payload = {
        type: "tablecell",
        line: parseInt(target.dataset.mdLine, 10),
        cell: parseInt(target.dataset.mdCell, 10),
        hash: target.dataset.mdHash,
        html: htmlNow
      };
    } else {
      oldEnd = parseInt(target.dataset.mdEnd, 10);
      payload = {
        type: type,
        start: parseInt(target.dataset.mdStart, 10),
        end: parseInt(target.dataset.mdEnd, 10),
        hash: target.dataset.mdHash,
        html: htmlNow
      };
    }

    setStatus(target, "saving", "saving…");
    fetch("/api/edit", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        return { status: resp.status, ok: resp.ok, body: body };
      });
    }).then(function (res) {
      if (res.status === 409) {
        // The source drifted under the tab; keep what the user typed visible
        // and flag it so they reload (do not silently revert their work).
        onConflict(target, res.body);
        return;
      }
      if (!res.ok || !res.body || !res.body.ok) {
        target.innerHTML = original; // restore the pre-edit HTML on a failed save
        if (res.status === 403 && res.body && res.body.error === "loopback_only") {
          showLanNotice(); // came over the LAN: explain, never show a bare code
          setStatus(target, "error", "editing is local-only");
        } else {
          setStatus(target, "error", friendlyError(res.body));
        }
        return;
      }
      applyResult(target, res.body, oldEnd);
    }).catch(function () {
      target.innerHTML = original; // restore the pre-edit HTML on a failed save
      setStatus(target, "error", "couldn't save (offline?)");
    });
  }

  function onConflict(target, body) {
    target.classList.add("webdoc-conflict");
    setStatus(target, "error", (body && body.message) || "changed on disk, reload");
  }

  // Map server error codes to short, human messages for the status pill, so a
  // failed save never surfaces a bare machine code.
  function friendlyError(body) {
    if (body && body.message) return body.message;
    var map = {
      body_too_large: "edit too large to save",
      empty_body: "nothing to save",
      bad_json: "couldn't save (bad data)",
      bad_content_length: "couldn't save (bad request)",
      source_missing: "source file not found",
      no_manifest: "site manifest missing",
      no_source_path: "no source file for this site",
      editing_unavailable: "editing not available here",
      edit_failed: "couldn't save that change"
    };
    var code = body && body.error;
    return (code && map[code]) || "couldn't save";
  }

  function applyResult(target, body, oldEnd) {
    target.innerHTML = body.new_html;
    if (body.type === "tablecell") {
      target.dataset.mdHash = body.new_hash;
    } else {
      shiftFollowing(oldEnd, body.line_delta || 0, target);
      target.dataset.mdStart = body.new_start;
      target.dataset.mdEnd = body.new_end;
      target.dataset.mdHash = body.new_hash;
    }
    target.setAttribute("data-webdoc-edited", "1");
    renderLint(target, body.lint || []);
    setStatus(target, "saved", "saved ✓");
    updateCount();
  }

  // After a block's line count changes by `delta`, every later block's source
  // range shifts. Threshold is the edited block's OLD end; the edited block
  // itself (exceptEl) is updated separately by the caller.
  function shiftFollowing(oldEnd, delta, exceptEl) {
    if (!delta) return;
    document.querySelectorAll("[data-md-start]").forEach(function (node) {
      if (node === exceptEl) return;
      var s = parseInt(node.dataset.mdStart, 10);
      var e = parseInt(node.dataset.mdEnd, 10);
      if (s > oldEnd) {
        node.dataset.mdStart = s + delta;
        node.dataset.mdEnd = e + delta;
      }
    });
    document.querySelectorAll("[data-md-line]").forEach(function (node) {
      if (node === exceptEl) return;
      var ln = parseInt(node.dataset.mdLine, 10);
      if (ln > oldEnd) node.dataset.mdLine = ln + delta;
    });
  }

  // ---- mode toggle --------------------------------------------------------

  function enterEditMode() {
    // The save path is loopback-only; explain instead of letting the user type
    // an edit the server will then refuse with a bare "loopback_only".
    if (!isLoopbackHost()) {
      showLanNotice();
      return;
    }
    state.on = true;
    document.body.classList.add("webdoc-edit-on");
    if (!banner) buildBanner();
    else banner.style.display = "";
    updateCount();
    if (toggleBtn) toggleBtn.style.display = "none";
  }

  function exitEditMode() {
    if (state.active) {
      var target = state.active;
      state.active = null;
      commit(target);
    }
    state.on = false;
    document.body.classList.remove("webdoc-edit-on");
    if (banner) banner.style.display = "none";
    hideToolbar();
    if (linkPopover) linkPopover.hidden = true;
    savedRange = null;
    if (toggleBtn) toggleBtn.style.display = "";
  }

  // ---- global interaction wiring -----------------------------------------

  function onMouseDown(ev) {
    if (!state.on) return;
    if (toolbar && toolbar.contains(ev.target)) return;
    if (banner && banner.contains(ev.target)) return;
    if (toggleBtn && toggleBtn.contains(ev.target)) return;
    // A click inside the open link popover must not commit/blur the block.
    if (linkPopover && !linkPopover.hidden && linkPopover.contains(ev.target)) return;
    // A click anywhere else dismisses an open popover (without re-focusing,
    // since we may be activating a different block on the same click).
    if (linkPopover && !linkPopover.hidden) { linkPopover.hidden = true; savedRange = null; }

    var block = ev.target.closest ? ev.target.closest(EDITABLE) : null;
    if (block && block.hasAttribute("data-noedit")) block = null;
    if (block === state.active) return; // moving the caret inside the active block

    var prev = state.active;
    state.active = null;
    if (prev) commit(prev);
    if (block && isEditable(block)) activate(block);
  }

  function onKeyDown(ev) {
    // Keys typed in the link popover are its own concern. This handler runs in
    // the capture phase (before the input's handler), so without this guard an
    // Esc/Enter in the popover would also cancel/commit the underlying block.
    if (linkPopover && !linkPopover.hidden && linkPopover.contains(ev.target)) return;
    if (!state.on || !state.active) return;
    var mod = ev.metaKey || ev.ctrlKey;
    if (ev.key === "Escape") {
      ev.preventDefault();
      cancelActive();
    } else if (mod && ev.key === "Enter") {
      ev.preventDefault();
      var target = state.active;
      state.active = null;
      commit(target);
    } else if (mod && (ev.key === "b" || ev.key === "B")) {
      ev.preventDefault();
      document.execCommand("bold");
    } else if (mod && (ev.key === "i" || ev.key === "I")) {
      ev.preventDefault();
      document.execCommand("italic");
    }
  }

  function onScrollOrResize() {
    if (state.on && state.active) showToolbar(state.active);
  }

  function init() {
    if (!document.querySelector(EDITABLE)) return; // nothing editable on this page
    buildToggle();
    buildToolbar();
    document.addEventListener("mousedown", onMouseDown, true);
    document.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
