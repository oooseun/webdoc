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
  var undoBtn = null;     // banner "Undo" button
  var toastEl = null;     // transient bottom note (delete confirmation, undo errors)
  var toastTimer = null;

  // Client half of the undo stack, in lockstep with the server's per-file stack.
  // Each entry holds the live DOM node so an undeleted block (incl. a list item
  // in its list) comes back as the exact element, no reconstruction. LIFO.
  var undoStack = [];
  var UNDO_LIMIT = 200;
  // Serialise the write path so the two stacks cannot diverge: never undo while a
  // save is in flight (its undo entry has not been pushed yet) or while another
  // undo is in flight (a double Cmd+Z would pop one entry but fire two requests).
  var pendingSaves = 0;
  var undoing = false;

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
    undoBtn = el("button", "webdoc-undo-btn", "Undo");
    undoBtn.type = "button";
    undoBtn.title = "Undo last change (Cmd/Ctrl+Z)";
    undoBtn.disabled = true;
    undoBtn.addEventListener("click", function () {
      // Commit any in-progress block first, then undo the resulting newest
      // change (so "Undo" while mid-edit reverts what you were just doing).
      var pending = Promise.resolve();
      if (state.active) {
        var t = state.active;
        state.active = null;
        pending = Promise.resolve(commit(t));
      }
      pending.then(doUndo);
    });
    var done = el("button", null, "Done");
    done.type = "button";
    done.addEventListener("click", exitEditMode);
    banner.appendChild(label);
    banner.appendChild(spacer);
    banner.appendChild(countEl);
    banner.appendChild(undoBtn);
    banner.appendChild(done);
    document.body.appendChild(banner);
  }

  function updateCount() {
    if (!countEl) return;
    var n = document.querySelectorAll("[data-webdoc-edited]").length;
    countEl.textContent = n + (n === 1 ? " block edited" : " blocks edited");
  }

  function updateUndoBtn() {
    if (undoBtn) undoBtn.disabled = undoStack.length === 0;
  }

  function pushUndo(entry) {
    undoStack.push(entry);
    if (undoStack.length > UNDO_LIMIT) undoStack.shift();
    updateUndoBtn();
  }

  // Transient bottom-centre note (block deleted, undo failed). Not a status pill:
  // those anchor to a block, and a deleted block has no rect to anchor to.
  function flash(msg) {
    if (!toastEl) {
      toastEl = el("div", "webdoc-toast");
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.classList.add("webdoc-toast-on");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      if (toastEl) toastEl.classList.remove("webdoc-toast-on");
    }, 2600);
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
      ["list", "List", "Toggle bullet list"],
      ["delete", "Delete", "Delete this block (undo with Cmd/Ctrl+Z)"]
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
      if (spec[0] === "delete") toolbar.__deleteBtn = b;
      if (spec[0] === "list") toolbar.__listBtn = b;
      toolbar.appendChild(b);
    });
    document.body.appendChild(toolbar);
  }

  function showToolbar(target) {
    if (!toolbar) return;
    toolbar.hidden = false;
    // Delete removes a whole range block; it has no meaning for a table cell
    // (removing one cell would break the row), so hide it there.
    if (toolbar.__deleteBtn) {
      toolbar.__deleteBtn.style.display =
        target.dataset.mdType === "tablecell" ? "none" : "";
    }
    // List toggles paragraph <-> bullet item; only those two types qualify. It
    // reads as pressed when the active block is already a list item.
    if (toolbar.__listBtn) {
      var t = target.dataset.mdType;
      var listable = t === "paragraph" || t === "listitem";
      toolbar.__listBtn.style.display = listable ? "" : "none";
      toolbar.__listBtn.classList.toggle("webdoc-cmd-on", t === "listitem");
    }
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
    } else if (cmd === "list") {
      toggleList();
    } else if (cmd === "delete") {
      deleteBlock();
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

  // -- delete a whole block ------------------------------------------------

  // Remove the active range block (paragraph / heading / list item) from the
  // source. The removed DOM node is kept (off-document) on the undo stack so
  // Cmd/Ctrl+Z restores the exact element where it was. Hash-checked server-side
  // (409 on drift). Not available for table cells (showToolbar hides the button).
  function deleteBlock() {
    var target = state.active;
    if (!target) return;
    var type = target.dataset.mdType;
    if (type === "tablecell") return;

    // We delete the *saved* block; drop any uncommitted typing so the node we
    // stash for undo matches the source it will be restored from.
    target.innerHTML = state.original;
    state.active = null;
    deactivate(target);

    var start = parseInt(target.dataset.mdStart, 10);
    var end = parseInt(target.dataset.mdEnd, 10);
    var prevEdited = target.hasAttribute("data-webdoc-edited");
    var parent = target.parentNode;
    var nextSibling = target.nextSibling;

    setStatus(target, "saving", "deleting…");
    pendingSaves++;
    fetch("/api/edit", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        op: "delete", type: type, start: start, end: end, hash: target.dataset.mdHash
      })
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        return { status: resp.status, ok: resp.ok, body: body };
      });
    }).then(function (res) {
      if (res.status === 409) {
        onConflict(target, res.body); // block changed on disk; keep it, flag it
        return;
      }
      if (!res.ok || !res.body || !res.body.ok) {
        if (res.status === 403 && res.body && res.body.error === "loopback_only") {
          showLanNotice();
          setStatus(target, "error", "editing is local-only");
        } else {
          setStatus(target, "error", friendlyError(res.body));
        }
        return;
      }
      // Clear the lingering "deleting…" pill (no auto-dismiss on "saving"),
      // then drop the node from the page but keep the reference for undo.
      if (target.__wdStatus) { target.__wdStatus.remove(); target.__wdStatus = null; }
      if (target.__wdLint) { target.__wdLint.remove(); target.__wdLint = null; }
      if (parent) parent.removeChild(target);
      shiftFollowing(end, res.body.line_delta || 0, null);
      pushUndo({
        label: "delete", node: target, parent: parent, nextSibling: nextSibling,
        prevEdited: prevEdited, expectStart: start
      });
      updateCount();
      flash("Block deleted. Undo with Cmd/Ctrl+Z.");
    }).catch(function () {
      setStatus(target, "error", "couldn't delete (offline?)");
    }).finally(function () {
      pendingSaves--;
    });
  }

  // -- vertical spacing (Enter / Backspace at a block's start) --------------

  // True when the caret is collapsed at the very start of the block (no text
  // before it), so Enter means "add a gap above" rather than a line break.
  function caretAtStart(block) {
    var sel = window.getSelection();
    if (!sel || !sel.isCollapsed || !sel.rangeCount) return false;
    var r = sel.getRangeAt(0);
    if (!block.contains(r.startContainer)) return false;
    var probe = document.createRange();
    probe.selectNodeContents(block);
    probe.setEnd(r.startContainer, r.startOffset);
    return probe.toString().length === 0;
  }

  // A gap element identical to what create_site emits for an extra blank line,
  // so a live-added gap and a rebuilt one look the same.
  function makeGap() {
    var g = el("div", "webdoc-gap");
    g.setAttribute("data-noedit", "");
    g.setAttribute("aria-hidden", "true");
    g.style.height = "0.7em";
    return g;
  }

  // Add or remove one blank line before the block (a visible gap), persisted to
  // source. The block's content is untouched. Undoable. `dir` is "add"|"remove".
  function changeSpace(block, dir) {
    var existingGap = dir === "remove" ? block.previousElementSibling : null;
    pendingSaves++;
    fetch("/api/edit", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        op: "space", type: block.dataset.mdType,
        start: parseInt(block.dataset.mdStart, 10),
        end: parseInt(block.dataset.mdEnd, 10),
        hash: block.dataset.mdHash, dir: dir
      })
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        return { status: resp.status, ok: resp.ok, body: body };
      });
    }).then(function (res) {
      if (res.status === 409) { onConflict(block, res.body); return; }
      if (!res.ok || !res.body || !res.body.ok) {
        if (res.status === 403 && res.body && res.body.error === "loopback_only") showLanNotice();
        else if (!(res.body && res.body.error === "no_space")) setStatus(block, "error", friendlyError(res.body));
        return;
      }
      var gapNode;
      if (dir === "add") {
        gapNode = makeGap();
        if (block.parentNode) block.parentNode.insertBefore(gapNode, block);
      } else {
        gapNode = existingGap;
        if (gapNode && gapNode.parentNode) gapNode.parentNode.removeChild(gapNode);
      }
      shiftFollowing(res.body.shift_threshold, res.body.line_delta || 0, null);
      pushUndo({ label: "space", dir: dir, block: block, gap: gapNode });
      setStatus(block, "saved", dir === "add" ? "space added" : "space removed");
    }).catch(function () {
      setStatus(block, "error", "couldn't change spacing (offline?)");
    }).finally(function () {
      pendingSaves--;
    });
  }

  // -- list toggle (paragraph <-> bullet item) -----------------------------

  function setBlockAttrs(node, type, body) {
    node.dataset.mdType = type;
    if (body.new_start != null) node.dataset.mdStart = body.new_start;
    if (body.new_end != null) node.dataset.mdEnd = body.new_end;
    if (body.new_hash != null) node.dataset.mdHash = body.new_hash;
  }

  // Convert the active paragraph to a bullet item, or a list item back to a
  // paragraph. The block's text is preserved; only its type (and its enclosing
  // <ul>/<ol>) changes. Undoable: the swap records how to reverse the DOM surgery.
  function toggleList() {
    var block = state.active;
    if (!block) return;
    var type = block.dataset.mdType;
    if (type !== "paragraph" && type !== "listitem") return;
    var target = type === "paragraph" ? "listitem" : "paragraph";
    var htmlNow = block.innerHTML;
    var prevEdited = block.hasAttribute("data-webdoc-edited");
    var start = parseInt(block.dataset.mdStart, 10);
    var oldEnd = parseInt(block.dataset.mdEnd, 10);

    state.active = null; // this block element is being replaced; stop tracking it
    deactivate(block);
    pendingSaves++;
    setStatus(block, "saving", "converting…");
    fetch("/api/edit", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        op: "retype", type: type, target: target,
        start: start, end: oldEnd, hash: block.dataset.mdHash, html: htmlNow
      })
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        return { status: resp.status, ok: resp.ok, body: body };
      });
    }).then(function (res) {
      if (res.status === 409) { onConflict(block, res.body); return; }
      if (!res.ok || !res.body || !res.body.ok) {
        if (res.status === 403 && res.body && res.body.error === "loopback_only") showLanNotice();
        else setStatus(block, "error", friendlyError(res.body));
        return; // block is untouched in the DOM; click it again to retry
      }
      if (block.__wdStatus) { block.__wdStatus.remove(); block.__wdStatus = null; }
      if (block.__wdLint) { block.__wdLint.remove(); block.__wdLint = null; }
      var swap = target === "listitem" ? swapToListItem(block, res.body)
                                       : swapToParagraph(block, res.body);
      shiftFollowing(oldEnd, res.body.line_delta || 0, swap.newNode);
      if (prevEdited) swap.newNode.setAttribute("data-webdoc-edited", "1");
      pushUndo({
        label: "retype", newNode: swap.newNode, oldNode: swap.oldNode,
        reverse: swap.reverse, prevEdited: prevEdited, expectStart: start
      });
      setStatus(swap.newNode, "saved", target === "listitem" ? "listed" : "unlisted");
      updateCount();
    }).catch(function () {
      setStatus(block, "error", "couldn't convert (offline?)");
    }).finally(function () {
      pendingSaves--;
    });
  }

  // paragraph -> list item: wrap it in a fresh standalone <ul>; drop the <p>.
  // Always standalone (never merged into an adjacent list): the paragraph's blank
  // line separators keep it a separate list in source, and the renderer breaks a
  // list on any blank line, so a rebuild renders a standalone list too - merging
  // live would diverge from the rebuilt page. `reverse` restores the <p>.
  function swapToListItem(p, body) {
    var li = el("li");
    li.innerHTML = body.new_html;
    setBlockAttrs(li, "listitem", body);
    var parent = p.parentNode, nextSibling = p.nextSibling;
    var ul = el("ul");
    ul.appendChild(li);
    parent.insertBefore(ul, p);
    parent.removeChild(p);
    return { newNode: li, oldNode: p, reverse: { p: p, parent: parent, nextSibling: nextSibling, dropUl: ul } };
  }

  function unswapListItem(r) {
    if (r.dropUl && r.dropUl.parentNode) r.dropUl.parentNode.removeChild(r.dropUl);
    r.parent.insertBefore(r.p, r.nextSibling);
  }

  // list item -> paragraph: pull the item out of its list, splitting the list if
  // it was in the middle. `reverse` rebuilds the list around the restored <li>.
  function swapToParagraph(li, body) {
    var p = el("p");
    p.innerHTML = body.new_html;
    setBlockAttrs(p, "paragraph", body);
    var ul = li.parentNode;                 // <ul>/<ol>
    var container = ul.parentNode;
    var items = [];
    for (var i = 0; i < ul.children.length; i++) items.push(ul.children[i]);
    var idx = items.indexOf(li);
    var reverse;
    if (items.length === 1) {
      container.insertBefore(p, ul);
      container.removeChild(ul);            // ul still holds li, kept for undo
      reverse = { kind: "restore-ul", ul: ul, p: p };
    } else if (idx === 0) {
      container.insertBefore(p, ul);
      ul.removeChild(li);
      reverse = { kind: "first", ul: ul, li: li, p: p };
    } else if (idx === items.length - 1) {
      container.insertBefore(p, ul.nextSibling);
      ul.removeChild(li);
      reverse = { kind: "last", ul: ul, li: li, p: p };
    } else {
      var newUl = el(ul.tagName.toLowerCase());
      for (var j = idx + 1; j < items.length; j++) newUl.appendChild(items[j]);
      ul.removeChild(li);
      container.insertBefore(p, ul.nextSibling);
      container.insertBefore(newUl, p.nextSibling);
      reverse = { kind: "split", ul: ul, newUl: newUl, li: li, p: p };
    }
    return { newNode: p, oldNode: li, reverse: reverse };
  }

  function unswapParagraph(r) {
    if (r.kind === "restore-ul") {
      r.p.parentNode.insertBefore(r.ul, r.p); // ul still contains li
    } else if (r.kind === "first") {
      r.ul.insertBefore(r.li, r.ul.firstChild);
    } else if (r.kind === "last") {
      r.ul.appendChild(r.li);
    } else { // split: put li back, fold newUl's items after it, drop newUl
      r.ul.appendChild(r.li);
      while (r.newUl.firstChild) r.ul.appendChild(r.newUl.firstChild);
      if (r.newUl.parentNode) r.newUl.parentNode.removeChild(r.newUl);
    }
    if (r.p.parentNode) r.p.parentNode.removeChild(r.p);
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
    if (!target) return Promise.resolve();
    deactivate(target);
    if (state.cancelling) {
      state.cancelling = false;
      return Promise.resolve();
    }
    var htmlNow = target.innerHTML;
    if (htmlNow === state.original) return Promise.resolve(); // nothing changed

    // Snapshot the pre-edit HTML now: state.original is overwritten the moment
    // another block is activated, but this async save may resolve later. On any
    // failed save we restore the block to this, so it never lingers broken.
    var original = state.original;
    var prevEdited = target.hasAttribute("data-webdoc-edited"); // for undo
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
    pendingSaves++;
    return fetch("/api/edit", {
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
      // Inverse for undo: restore the pre-edit HTML (and edited-badge state) of
      // this exact node. The expect* identity lets the client confirm the server
      // reversed THIS block before patching the node (desync backstop).
      pushUndo({
        label: type === "tablecell" ? "cell" : "edit",
        node: target,
        prevHTML: original,
        prevEdited: prevEdited,
        expectStart: type === "tablecell" ? null : payload.start,
        line: type === "tablecell" ? payload.line : null,
        cell: type === "tablecell" ? payload.cell : null
      });
    }).catch(function () {
      target.innerHTML = original; // restore the pre-edit HTML on a failed save
      setStatus(target, "error", "couldn't save (offline?)");
    }).finally(function () {
      pendingSaves--;
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

  // ---- undo ---------------------------------------------------------------

  function setRangeAttrs(node, body) {
    if (body.new_start != null) node.dataset.mdStart = body.new_start;
    if (body.new_end != null) node.dataset.mdEnd = body.new_end;
    if (body.new_hash != null) node.dataset.mdHash = body.new_hash;
  }

  function restoreEdited(node, was) {
    if (was) node.setAttribute("data-webdoc-edited", "1");
    else node.removeAttribute("data-webdoc-edited");
  }

  // Reverse the newest change. Calls the server (authoritative for the file),
  // and only on success pops our parallel stack and patches the DOM. A 400
  // (nothing to undo) or 409 (source drifted) means the stacks desynced - drop
  // ours so a stale node ref can never be replayed against a changed page.
  function doUndo() {
    if (!undoStack.length) { updateUndoBtn(); return; }
    // Stay strictly serial: a save in flight has not pushed its undo entry yet,
    // and a second undo would pop one entry but fire two requests. Either would
    // desync the two stacks.
    if (undoing || pendingSaves > 0) return;
    undoing = true;
    fetch("/api/undo", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}"
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        return { status: resp.status, ok: resp.ok, body: body };
      });
    }).then(function (res) {
      if (!res.ok || !res.body || !res.body.ok) {
        undoStack.length = 0;
        updateUndoBtn();
        if (res.status === 409) flash("Source changed on disk. Reload to edit.");
        else if (res.status === 403) showLanNotice();
        else if (res.status !== 400) flash("Couldn't undo.");
        return;
      }
      var entry = undoStack.pop();
      updateUndoBtn();
      if (entry) applyUndoEntry(entry, res.body);
    }).catch(function () {
      flash("Couldn't undo (offline?).");
    }).finally(function () {
      undoing = false;
    });
  }

  // The server reversed the newest op on the file; confirm it is the same block
  // our popped entry refers to before patching the DOM. They can disagree only
  // on a genuine desync (a second tab sharing the file's server stack); patching
  // the wrong node then would scramble it, so drop our stack and ask for a reload.
  function undoEntryMatches(entry, body) {
    if (body.label !== entry.label) return false;
    if (entry.label === "cell") return body.line === entry.line && body.cell === entry.cell;
    if (entry.label === "space") return true; // splice-level; no block identity to compare
    return body.new_start === entry.expectStart; // edit + delete
  }

  function applyUndoEntry(entry, body) {
    if (!undoEntryMatches(entry, body)) {
      undoStack.length = 0;
      updateUndoBtn();
      flash("Undo got out of sync. Reload to be safe.");
      return;
    }
    if (entry.label === "space") {
      // Reverse the gap: an added gap is removed, a removed one re-inserted; then
      // re-shift the block (and everything after) by the splice delta.
      if (entry.dir === "add") {
        if (entry.gap && entry.gap.parentNode) entry.gap.parentNode.removeChild(entry.gap);
      } else if (entry.block && entry.block.parentNode) {
        entry.block.parentNode.insertBefore(entry.gap, entry.block);
      }
      shiftFollowing(body.shift_threshold, body.line_delta || 0, null);
      setStatus(entry.block, "saved", "undone");
      updateCount();
      return;
    }
    if (entry.label === "retype") {
      // Reverse the element swap: restore the original block, drop the new one.
      if (entry.newNode.tagName === "LI") unswapListItem(entry.reverse);
      else unswapParagraph(entry.reverse);
      var restored = entry.oldNode;
      setRangeAttrs(restored, body); // original start/end/hash from the server
      shiftFollowing(body.shift_threshold, body.line_delta || 0, restored);
      restoreEdited(restored, entry.prevEdited);
      setStatus(restored, "saved", "undone");
      updateCount();
      return;
    }
    var node = entry.node;
    if (entry.label === "delete") {
      // Put the exact removed element back where it sat, then re-shift the
      // blocks below it (they moved up when it was deleted).
      if (entry.parent) entry.parent.insertBefore(node, entry.nextSibling || null);
      setRangeAttrs(node, body);
      shiftFollowing(body.shift_threshold, body.line_delta || 0, node);
    } else if (entry.label === "cell") {
      node.innerHTML = entry.prevHTML;
      if (body.new_hash != null) node.dataset.mdHash = body.new_hash;
    } else { // edit
      node.innerHTML = entry.prevHTML;
      setRangeAttrs(node, body);
      shiftFollowing(body.shift_threshold, body.line_delta || 0, node);
    }
    restoreEdited(node, entry.prevEdited);
    setStatus(node, "saved", "undone");
    updateCount();
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
    if (!state.on) return;
    var mod = ev.metaKey || ev.ctrlKey;
    // Cross-save undo only when no block is active; while a block is being
    // edited, Cmd/Ctrl+Z stays the browser's native in-block undo. With nothing
    // on the stack we also stand aside, so the keystroke isn't swallowed for no
    // effect (the browser keeps its native document-level undo).
    if (mod && !ev.shiftKey && (ev.key === "z" || ev.key === "Z") && !state.active) {
      if (!undoStack.length) return;
      ev.preventDefault();
      doUndo();
      return;
    }
    if (!state.active) return;
    // Spacing: at a paragraph/heading's very start, Enter adds a gap above it,
    // and Backspace removes one (when a gap is there) instead of doing nothing
    // across the contenteditable boundary. List items are excluded - a blank line
    // would split the list. Anywhere else, Enter/Backspace behave normally.
    var spaceable = state.active.dataset.mdType === "paragraph" ||
                    state.active.dataset.mdType === "heading";
    if (spaceable && ev.key === "Enter" && !mod && caretAtStart(state.active)) {
      ev.preventDefault();
      changeSpace(state.active, "add");
      return;
    }
    if (spaceable && ev.key === "Backspace" && !mod && caretAtStart(state.active)) {
      var prev = state.active.previousElementSibling;
      if (prev && prev.classList && prev.classList.contains("webdoc-gap")) {
        ev.preventDefault();
        changeSpace(state.active, "remove");
        return;
      }
    }
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
