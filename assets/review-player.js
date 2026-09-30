// review-player.js — webdoc's video review player.
// Progressively enhances every plain <video> on the page into a review tool:
// large surface, real scrub bar with buffered range, keyboard transport
// (space/arrows/frame-step/speed/mute/fullscreen), an A/B loop for replaying a
// join, and clickable [m:ss] timestamps anywhere in the page text that seek
// whichever player they're closest to. If this script fails or never loads,
// each <video> keeps its native `controls` attribute and plays fine on its
// own — enhancement only removes that attribute after a player wraps
// successfully. Inert on a page with no <video>.
(function () {
  "use strict";

  var FRAME = 1001 / 30000; // one NTSC 29.97fps frame, in seconds
  var SPEEDS = [0.5, 0.75, 1, 1.5, 2];
  var SMALL_SEEK = 5;
  var TINY_SEEK = 1;
  var players = []; // in DOM order
  var active = null;

  function fmtTime(t) {
    if (!isFinite(t) || t < 0) t = 0;
    var m = Math.floor(t / 60);
    var s = Math.floor(t % 60);
    var f = Math.floor((t - Math.floor(t)) * 10);
    return m + ":" + (s < 10 ? "0" : "") + s + "." + f;
  }

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function el(tag, className, attrs) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (attrs) {
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) node.setAttribute(k, attrs[k]);
      }
    }
    return node;
  }

  function isFullscreen(node) {
    var fs = document.fullscreenElement || document.webkitFullscreenElement;
    return !!fs && (fs === node);
  }

  function requestFullscreen(node) {
    var fn = node.requestFullscreen || node.webkitRequestFullscreen;
    if (fn) fn.call(node);
  }

  function exitFullscreen() {
    var fn = document.exitFullscreen || document.webkitExitFullscreen;
    if (fn) fn.call(document);
  }

  function isTypingTarget(node) {
    if (!node) return false;
    var tag = (node.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    if (node.isContentEditable) return true;
    return false;
  }

  function Player(video) {
    this.video = video;
    this.loopIn = null;
    this.loopOut = null;
    this.looping = false;
    this.speedIdx = SPEEDS.indexOf(1);
    this.build();
  }

  Player.prototype.build = function () {
    var video = this.video;
    var wrap = el("div", "review-player", { "data-review-player": "", tabindex: "0" });
    video.parentNode.insertBefore(wrap, video);

    var surface = el("div", "rp-surface");
    wrap.appendChild(surface);
    surface.appendChild(video);

    var hint = el("button", "rp-playhint", { type: "button", "aria-label": "Play" });
    hint.textContent = "▶";
    surface.appendChild(hint);

    var scrub = el("div", "rp-scrub", { role: "slider", "aria-label": "Seek", tabindex: "0" });
    var buffered = el("div", "rp-buffered");
    var loopRange = el("div", "rp-loop-range");
    loopRange.hidden = true;
    var progress = el("div", "rp-progress");
    var markerIn = el("div", "rp-marker rp-marker-in");
    markerIn.hidden = true;
    var markerOut = el("div", "rp-marker rp-marker-out");
    markerOut.hidden = true;
    var playhead = el("div", "rp-playhead");
    scrub.appendChild(buffered);
    scrub.appendChild(loopRange);
    scrub.appendChild(progress);
    scrub.appendChild(markerIn);
    scrub.appendChild(markerOut);
    scrub.appendChild(playhead);
    wrap.appendChild(scrub);

    var controls = el("div", "rp-controls");
    var backBtn = el("button", "rp-btn rp-skip rp-back5", {
      type: "button", "aria-label": "Back 5 seconds", title: "Back 5 seconds"
    });
    backBtn.textContent = "-5s";
    var playBtn = el("button", "rp-btn rp-playpause", { type: "button", "aria-label": "Play" });
    playBtn.textContent = "▶";
    var fwdBtn = el("button", "rp-btn rp-skip rp-fwd5", {
      type: "button", "aria-label": "Forward 5 seconds", title: "Forward 5 seconds"
    });
    fwdBtn.textContent = "+5s";
    var time = el("span", "rp-time");
    time.textContent = "0:00.0 / 0:00.0";
    var speed = el("select", "rp-btn rp-speed", {
      "aria-label": "Playback speed", title: "Playback speed (pitch preserved)"
    });
    SPEEDS.forEach(function (rate) {
      var option = el("option", "", { value: String(rate) });
      option.textContent = rate === 1 ? "1× (Normal)" : rate + "×";
      speed.appendChild(option);
    });
    speed.value = String(video.playbackRate);
    // Use the browser's time stretching so faster playback keeps natural voices.
    var pitchPreserved = false;
    ["preservesPitch", "webkitPreservesPitch", "mozPreservesPitch"].forEach(function (key) {
      if (key in video) {
        video[key] = true;
        pitchPreserved = pitchPreserved || video[key] === true;
      }
    });
    speed.title = pitchPreserved ? "Playback speed (pitch preserved)" :
      "Playback speed (pitch correction unavailable in this browser)";
    var loopBtn = el("button", "rp-btn rp-loop", { type: "button", "aria-label": "Toggle A/B loop" });
    loopBtn.textContent = "loop";
    var muteBtn = el("button", "rp-btn rp-mute", { type: "button", "aria-label": "Mute" });
    muteBtn.textContent = "🔊";
    var fsBtn = el("button", "rp-btn rp-fullscreen", { type: "button", "aria-label": "Fullscreen" });
    fsBtn.textContent = "⛶";
    var statsBtn = el("button", "rp-btn rp-stats-toggle", {
      type: "button", "aria-label": "Toggle playback stats", title: "Playback stats",
      "aria-pressed": "false"
    });
    statsBtn.textContent = "Stats";
    var reloadBtn = el("button", "rp-btn rp-reload", {
      type: "button", title: "Reload a stuck or black video while keeping your place and notes"
    });
    reloadBtn.textContent = "Reload video";
    controls.appendChild(backBtn);
    controls.appendChild(playBtn);
    controls.appendChild(fwdBtn);
    controls.appendChild(time);
    controls.appendChild(speed);
    controls.appendChild(loopBtn);
    controls.appendChild(muteBtn);
    controls.appendChild(fsBtn);
    controls.appendChild(statsBtn);
    controls.appendChild(reloadBtn);
    wrap.appendChild(controls);

    var statsPanel = el("div", "rp-stats", { role: "status" });
    statsPanel.hidden = true;
    wrap.appendChild(statsPanel);

    var recoveryStatus = el("div", "rp-recovery-status", { role: "status" });
    wrap.appendChild(recoveryStatus);

    var legend = el("div", "rp-legend");
    legend.textContent =
      "space play/pause · ←/→ 5s · shift+←/→ 1s · , . frame step · [ ] speed · " +
      "m mute · f fullscreen · i/o set loop in/out · l toggle loop";
    wrap.appendChild(legend);

    this.wrap = wrap;
    this.surface = surface;
    this.hint = hint;
    this.scrub = scrub;
    this.buffered = buffered;
    this.loopRangeEl = loopRange;
    this.progress = progress;
    this.markerIn = markerIn;
    this.markerOut = markerOut;
    this.playhead = playhead;
    this.playBtn = playBtn;
    this.backBtn = backBtn;
    this.fwdBtn = fwdBtn;
    this.timeEl = time;
    this.speedEl = speed;
    this.loopBtn = loopBtn;
    this.muteBtn = muteBtn;
    this.fsBtn = fsBtn;
    this.statsBtn = statsBtn;
    this.statsPanel = statsPanel;
    this.reloadBtn = reloadBtn;
    this.recoveryStatus = recoveryStatus;

    this.wire();

    // Metrics is best-effort instrumentation only: a failure here must never
    // stop the controls handoff below, so it is fully contained.
    try {
      this.metrics = new PlayerMetrics(this);
    } catch (err) {
      this.metrics = null;
      if (window.console && console.warn) console.warn("review-player: metrics init failed", err);
    }

    // Only now does the native controls UI hand off to the custom one — if
    // anything above threw, this line never runs and the plain <video
    // controls> keeps working untouched.
    video.removeAttribute("controls");
    this.render();
  };

  Player.prototype.setActive = function () {
    if (active && active !== this) active.wrap.classList.remove("rp-active");
    active = this;
    this.wrap.classList.add("rp-active");
  };

  Player.prototype.togglePlay = function () {
    if (this.video.paused) this.video.play().catch(function () {});
    else this.video.pause();
  };

  Player.prototype.reloadVideo = function () {
    if (this.reloadBtn.disabled) return;
    var self = this;
    var video = this.video;
    // Keep the same element: feedback tools hold references and listeners on it.
    // Retain the snapshot after failure so retry doesn't replace it with time 0.
    var saved = this.reloadState || {
      time: isFinite(video.currentTime) ? video.currentTime : 0,
      paused: video.paused, rate: video.playbackRate,
      muted: video.muted, volume: video.volume
    };
    this.reloadState = saved;
    this.reloadBtn.disabled = true;
    this.recoveryStatus.textContent = "Reloading video at " + fmtTime(saved.time) + "…";
    var target = saved.time;
    var positioned = false;
    var timer;
    function cleanup() {
      clearTimeout(timer);
      video.removeEventListener("loadedmetadata", restore);
      video.removeEventListener("canplay", ready);
      video.removeEventListener("seeked", ready);
      video.removeEventListener("error", failed);
      self.reloadBtn.disabled = false;
    }
    function failed() {
      cleanup();
      self.recoveryStatus.textContent = "Video reload stalled. Your place (" +
        fmtTime(saved.time) + ") is saved for retry. Your notes remain on this page.";
    }
    function restore() {
      try {
        target = isFinite(video.duration) ? clamp(saved.time, 0, video.duration) : saved.time;
        video.playbackRate = saved.rate;
        video.muted = saved.muted;
        video.volume = saved.volume;
        video.currentTime = target;
        positioned = true;
        ready();
      } catch (err) { failed(); }
    }
    function ready() {
      if (!positioned || video.readyState < 3 || video.seeking ||
          Math.abs(video.currentTime - target) > 0.1) return;
      cleanup();
      self.reloadState = null;
      self.recoveryStatus.textContent = "Video reloaded at " + fmtTime(target) + ".";
      if (!saved.paused) video.play().catch(function () {
        self.recoveryStatus.textContent = "Video reloaded. Press play to continue.";
      });
      self.render();
    }
    video.addEventListener("loadedmetadata", restore);
    video.addEventListener("canplay", ready);
    video.addEventListener("seeked", ready);
    video.addEventListener("error", failed);
    timer = setTimeout(failed, 15000);
    // Explicit user recovery, never an automatic loop or a black-pixel heuristic.
    try { video.pause(); video.load(); } catch (err) { failed(); }
  };

  Player.prototype.seekBy = function (delta) {
    var d = this.video.duration;
    var target = this.video.currentTime + delta;
    if (isFinite(d)) target = clamp(target, 0, d);
    this.video.currentTime = Math.max(0, target);
  };

  Player.prototype.stepFrame = function (dir) {
    this.video.pause();
    this.seekBy(dir * FRAME);
  };

  Player.prototype.setSpeed = function (delta) {
    this.speedIdx = clamp(this.speedIdx + delta, 0, SPEEDS.length - 1);
    this.video.playbackRate = SPEEDS[this.speedIdx];
    this.speedEl.value = String(this.video.playbackRate);
  };

  Player.prototype.toggleMute = function () {
    this.video.muted = !this.video.muted;
  };

  Player.prototype.toggleFullscreen = function () {
    if (isFullscreen(this.wrap)) exitFullscreen();
    else requestFullscreen(this.wrap);
  };

  Player.prototype.toggleStats = function () {
    var showing = this.statsPanel.hidden;
    this.statsPanel.hidden = !showing;
    this.statsBtn.setAttribute("aria-pressed", showing ? "true" : "false");
    this.statsBtn.classList.toggle("rp-stats-on", showing);
    if (showing) {
      this.renderStats();
      if (this.metrics) this.metrics.startLiveUpdates();
    } else if (this.metrics) {
      this.metrics.stopLiveUpdates();
    }
  };

  Player.prototype.renderStats = function () {
    if (this.statsPanel.hidden || !this.metrics) return;
    this.statsPanel.textContent = this.metrics.statsText();
  };

  Player.prototype.markIn = function () {
    this.loopIn = this.video.currentTime;
    if (this.loopOut !== null && this.loopIn > this.loopOut) this.loopOut = null;
    this.render();
  };

  Player.prototype.markOut = function () {
    this.loopOut = this.video.currentTime;
    if (this.loopIn !== null && this.loopOut < this.loopIn) this.loopIn = null;
    this.render();
  };

  Player.prototype.toggleLoop = function () {
    if (this.loopIn === null || this.loopOut === null) return;
    this.looping = !this.looping;
    this.render();
  };

  Player.prototype.seekTo = function (seconds, playAfter) {
    var d = this.video.duration;
    var target = isFinite(d) ? clamp(seconds, 0, d) : Math.max(0, seconds);
    this.video.currentTime = target;
    if (playAfter) this.video.play().catch(function () {});
  };

  Player.prototype.scrubToClientX = function (clientX) {
    var rect = this.scrub.getBoundingClientRect();
    var frac = rect.width ? clamp((clientX - rect.left) / rect.width, 0, 1) : 0;
    var d = this.video.duration;
    if (isFinite(d)) this.video.currentTime = frac * d;
  };

  Player.prototype.wire = function () {
    var self = this;
    var video = this.video;

    ["mousedown", "focus", "keydown"].forEach(function (evt) {
      self.wrap.addEventListener(evt, function () { self.setActive(); });
    });

    video.addEventListener("click", function () { self.togglePlay(); });
    video.addEventListener("dblclick", function () { self.toggleFullscreen(); });
    this.hint.addEventListener("click", function (e) { e.stopPropagation(); self.togglePlay(); });

    this.speedEl.addEventListener("change", function () {
      var rate = Number(self.speedEl.value);
      if (SPEEDS.indexOf(rate) === -1) return;
      self.speedIdx = SPEEDS.indexOf(rate);
      video.playbackRate = rate;
      self.render();
    });
    video.addEventListener("ratechange", function () { self.render(); });
    video.addEventListener("play", function () { self.render(); });
    video.addEventListener("pause", function () { self.render(); });
    video.addEventListener("loadedmetadata", function () { self.render(); });
    video.addEventListener("timeupdate", function () {
      if (self.looping && self.loopOut !== null && video.currentTime >= self.loopOut) {
        video.currentTime = self.loopIn || 0;
      }
      self.render();
    });
    video.addEventListener("progress", function () { self.render(); });
    video.addEventListener("volumechange", function () { self.render(); });
    video.addEventListener("ratechange", function () { self.render(); });

    this.playBtn.addEventListener("click", function () { self.togglePlay(); });
    this.backBtn.addEventListener("click", function () { self.setActive(); self.seekBy(-SMALL_SEEK); });
    this.fwdBtn.addEventListener("click", function () { self.setActive(); self.seekBy(SMALL_SEEK); });
    this.muteBtn.addEventListener("click", function () { self.toggleMute(); });
    this.fsBtn.addEventListener("click", function () { self.toggleFullscreen(); });
    this.loopBtn.addEventListener("click", function () { self.toggleLoop(); });
    this.statsBtn.addEventListener("click", function () { self.toggleStats(); });
    this.reloadBtn.addEventListener("click", function () { self.reloadVideo(); });

    var dragging = false;
    function seekFromEvent(e) {
      var x = e.touches && e.touches[0] ? e.touches[0].clientX : e.clientX;
      self.scrubToClientX(x);
    }
    this.scrub.addEventListener("mousedown", function (e) {
      dragging = true;
      seekFromEvent(e);
      self.setActive();
    });
    window.addEventListener("mousemove", function (e) {
      if (dragging) seekFromEvent(e);
    });
    window.addEventListener("mouseup", function () { dragging = false; });
    this.scrub.addEventListener("touchstart", function (e) { seekFromEvent(e); self.setActive(); }, { passive: true });
    this.scrub.addEventListener("touchmove", function (e) { seekFromEvent(e); }, { passive: true });
  };

  Player.prototype.render = function () {
    var video = this.video;
    var d = video.duration;
    var hasDuration = isFinite(d) && d > 0;

    this.playBtn.textContent = video.paused ? "▶" : "∥";
    this.playBtn.setAttribute("aria-label", video.paused ? "Play" : "Pause");
    this.wrap.classList.toggle("rp-paused", video.paused);
    this.muteBtn.textContent = video.muted ? "🔇" : "🔊";
    this.timeEl.textContent = fmtTime(video.currentTime) + " / " + fmtTime(hasDuration ? d : 0);
    this.speedEl.value = String(video.playbackRate);
    var currentSpeedIdx = SPEEDS.indexOf(video.playbackRate);
    if (currentSpeedIdx !== -1) this.speedIdx = currentSpeedIdx;
    this.loopBtn.classList.toggle("rp-loop-on", this.looping);
    this.loopBtn.disabled = this.loopIn === null || this.loopOut === null;

    if (hasDuration) {
      var pct = clamp((video.currentTime / d) * 100, 0, 100);
      this.progress.style.width = pct + "%";
      // Positions go to CSS as --rp-pos: the stylesheet keeps the whole dot (and each marker) on the bar.
      this.playhead.style.setProperty("--rp-pos", pct + "%");

      if (video.buffered && video.buffered.length) {
        var end = video.buffered.end(video.buffered.length - 1);
        this.buffered.style.width = clamp((end / d) * 100, 0, 100) + "%";
      }

      if (this.loopIn !== null) {
        this.markerIn.hidden = false;
        this.markerIn.style.setProperty("--rp-pos", clamp((this.loopIn / d) * 100, 0, 100) + "%");
      } else {
        this.markerIn.hidden = true;
      }
      if (this.loopOut !== null) {
        this.markerOut.hidden = false;
        this.markerOut.style.setProperty("--rp-pos", clamp((this.loopOut / d) * 100, 0, 100) + "%");
      } else {
        this.markerOut.hidden = true;
      }
      if (this.loopIn !== null && this.loopOut !== null) {
        this.loopRangeEl.hidden = false;
        var left = clamp((this.loopIn / d) * 100, 0, 100);
        var right = clamp((this.loopOut / d) * 100, 0, 100);
        this.loopRangeEl.style.left = left + "%";
        this.loopRangeEl.style.width = Math.max(0, right - left) + "%";
      } else {
        this.loopRangeEl.hidden = true;
      }
    }
  };

  // -------------------------------------------------------------------- //
  // Playback metrics — capture media element events, derive the numbers a
  // reviewer means by "inconsistent streaming" (rebuffers, stalls, seek and
  // start latency, throughput), and ship them to the server as their own
  // log. Best-effort throughout: any failure here degrades to no metrics,
  // never to broken playback. See serve_site.py's /api/metrics handler for
  // the server side, and metrics.jsonl for the log itself.
  // -------------------------------------------------------------------- //

  var METRICS_ENDPOINT = "/api/metrics";
  var METRICS_FLUSH_INTERVAL_MS = 20000;
  var METRICS_FLUSH_AT_COUNT = 40;
  // Kept comfortably under sendBeacon's ~64KB per-call limit even with
  // buffered-range arrays inflating individual event records.
  var METRICS_CHUNK_SIZE = 80;
  // The exact capture list from the brief. "play" is deliberately not in it
  // (only "playing" is) — it is still watched internally, unlogged, purely
  // as the start marker for time-to-first-frame.
  var METRICS_EVENT_TYPES = [
    "loadstart", "loadedmetadata", "loadeddata", "canplay", "canplaythrough",
    "playing", "waiting", "stalled", "suspend", "seeking", "seeked",
    "pause", "ended", "error", "emptied", "ratechange"
  ];
  var metricsInstances = []; // every PlayerMetrics on the page, for the shared flush triggers
  var flushWiringDone = false;

  function nowMs() { return Date.now(); }

  function round2(n) {
    return isFinite(n) ? Math.round(n * 100) / 100 : 0;
  }

  function randomId() {
    return nowMs().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function bufferedSnapshot(video) {
    var ranges = [];
    var ahead = 0;
    try {
      var b = video.buffered;
      for (var i = 0; i < b.length; i++) {
        var start = b.start(i), end = b.end(i);
        ranges.push([round2(start), round2(end)]);
        if (video.currentTime >= start && video.currentTime <= end) ahead = end - video.currentTime;
      }
    } catch (err) {
      // buffered can throw on a detached or not-yet-ready media element
    }
    return { ranges: ranges, ahead: round2(ahead) };
  }

  function sendMetrics(payload) {
    try {
      var body = JSON.stringify(payload);
      var sent = false;
      if (navigator.sendBeacon) {
        var blob = new Blob([body], { type: "application/json" });
        sent = navigator.sendBeacon(METRICS_ENDPOINT, blob);
      }
      if (!sent) {
        fetch(METRICS_ENDPOINT, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: body,
          keepalive: true
        }).catch(function () {});
      }
    } catch (err) {
      // delivery is best-effort; losing a batch of metrics is never a page error
    }
  }

  function ensureGlobalFlushWiring() {
    if (flushWiringDone) return;
    flushWiringDone = true;
    if (window.performance && performance.setResourceTimingBufferSize) {
      try { performance.setResourceTimingBufferSize(500); } catch (err) {}
    }
    function flushAll(reason) {
      metricsInstances.forEach(function (m) { m.flush(reason); });
    }
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) flushAll("visibilitychange");
    });
    window.addEventListener("pagehide", function () { flushAll("pagehide"); });
    setInterval(function () { flushAll("interval"); }, METRICS_FLUSH_INTERVAL_MS);
    // Test/verification hook only: forces delivery instead of faking
    // document.hidden or waiting out the interval timer. Never called by
    // the player itself.
    window.__rpFlushMetrics = function () { flushAll("manual"); };
  }

  function PlayerMetrics(player) {
    this.player = player;
    this.video = player.video;
    this.sessionId = randomId();
    this.queue = [];
    this.rebufferCount = 0;
    this.rebufferMs = 0;
    this.stallCount = 0;
    this.hasStartedPlayback = false;
    this.waitingStartedAt = null;
    this.playPendingAt = null;
    this.firstPlayLatencyMs = null;
    this.lastPlayLatencyMs = null;
    this.seekPendingAt = null;
    this.lastSeekLatencyMs = null;
    this.throughputKbps = null;
    this.resourceEntriesSeen = 0;
    this.liveTimer = null;
    this.wire();
    metricsInstances.push(this);
    ensureGlobalFlushWiring();
  }

  PlayerMetrics.prototype.wire = function () {
    var self = this;
    var video = this.video;
    METRICS_EVENT_TYPES.forEach(function (type) {
      video.addEventListener(type, function () { self.onEvent(type); });
    });
    // Unlogged: only seeds the time-to-first-frame marker (see METRICS_EVENT_TYPES comment).
    video.addEventListener("play", function () {
      if (self.playPendingAt === null) self.playPendingAt = nowMs();
    });
    // Not in the requested list either; drives the throughput sampler off
    // the same download progress the buffered-ranges bar already uses.
    video.addEventListener("progress", function () { self.sampleThroughput(); });
  };

  PlayerMetrics.prototype.onEvent = function (type) {
    var video = this.video;
    var now = nowMs();
    var buf = bufferedSnapshot(video);
    var record = {
      type: type,
      t: now,
      currentTime: round2(video.currentTime),
      duration: isFinite(video.duration) ? round2(video.duration) : null,
      readyState: video.readyState,
      networkState: video.networkState,
      paused: video.paused,
      playbackRate: video.playbackRate,
      buffered: buf.ranges,
      bufferedAhead: buf.ahead
    };
    if (type === "error" && video.error) {
      record.errorCode = video.error.code;
      record.errorMessage = String(video.error.message || "").slice(0, 200);
    }

    switch (type) {
      case "playing":
        if (this.waitingStartedAt !== null) {
          this.rebufferMs += now - this.waitingStartedAt;
          this.waitingStartedAt = null;
        }
        if (this.playPendingAt !== null) {
          var playLatency = now - this.playPendingAt;
          this.lastPlayLatencyMs = playLatency;
          if (this.firstPlayLatencyMs === null) this.firstPlayLatencyMs = playLatency;
          this.playPendingAt = null;
        }
        if (this.seekPendingAt !== null) {
          this.lastSeekLatencyMs = now - this.seekPendingAt;
          this.seekPendingAt = null;
        }
        this.hasStartedPlayback = true;
        break;
      case "waiting":
        // Only counts once playback has actually started: the pre-roll wait
        // before the first "playing" is start latency (firstPlayLatencyMs),
        // not a rebuffer — otherwise every session's startup would also be
        // logged as rebuffer #1.
        if (this.waitingStartedAt === null && this.hasStartedPlayback) {
          this.rebufferCount++;
          this.waitingStartedAt = now;
        }
        break;
      case "stalled":
        this.stallCount++;
        break;
      case "seeking":
        this.seekPendingAt = now;
        break;
      case "pause":
        this.seekPendingAt = null; // a pause abandons any pending resume-after-seek measurement
        break;
    }

    this.push(record);
    if (this.player.statsPanel && !this.player.statsPanel.hidden) this.player.renderStats();
  };

  PlayerMetrics.prototype.push = function (record) {
    this.queue.push(record);
    if (this.queue.length >= METRICS_FLUSH_AT_COUNT) this.flush("size");
  };

  PlayerMetrics.prototype.sampleThroughput = function () {
    if (!window.performance || !performance.getEntriesByType) return;
    try {
      var src = this.video.currentSrc;
      if (!src) return;
      var entries = performance.getEntriesByType("resource");
      for (var i = this.resourceEntriesSeen; i < entries.length; i++) {
        var entry = entries[i];
        if (entry.name !== src) continue;
        var bytes = entry.transferSize || entry.encodedBodySize || 0;
        var ms = entry.responseEnd - entry.requestStart;
        if (bytes > 0 && ms > 0) {
          this.throughputKbps = round2((bytes * 8) / ms);
          this.push({
            type: "throughput_sample", t: nowMs(), bytes: bytes, ms: round2(ms),
            kbps: this.throughputKbps
          });
        }
      }
      this.resourceEntriesSeen = entries.length;
    } catch (err) {
      // Resource Timing is unavailable or cleared; throughput just stays unknown
    }
  };

  PlayerMetrics.prototype.summary = function () {
    return {
      rebufferCount: this.rebufferCount,
      rebufferMs: Math.round(this.rebufferMs),
      stallCount: this.stallCount,
      firstPlayLatencyMs: this.firstPlayLatencyMs,
      lastPlayLatencyMs: this.lastPlayLatencyMs,
      lastSeekLatencyMs: this.lastSeekLatencyMs,
      throughputKbps: this.throughputKbps,
      bufferedAhead: bufferedSnapshot(this.video).ahead
    };
  };

  PlayerMetrics.prototype.statsText = function () {
    var s = this.summary();
    var throughput = s.throughputKbps === null ? "unknown" :
      (s.throughputKbps >= 1000 ? (s.throughputKbps / 1000).toFixed(1) + " Mbps" : Math.round(s.throughputKbps) + " kbps");
    var text = "Rebuffers " + s.rebufferCount + " (" + (s.rebufferMs / 1000).toFixed(1) + "s buffering) · " +
      "stalls " + s.stallCount + " · buffered ahead " + s.bufferedAhead.toFixed(1) + "s · " +
      "throughput ~" + throughput;
    if (s.lastSeekLatencyMs !== null) text += " · last seek " + Math.round(s.lastSeekLatencyMs) + "ms";
    if (s.firstPlayLatencyMs !== null) text += " · start " + Math.round(s.firstPlayLatencyMs) + "ms";
    return text;
  };

  PlayerMetrics.prototype.startLiveUpdates = function () {
    var self = this;
    this.stopLiveUpdates();
    this.liveTimer = setInterval(function () { self.player.renderStats(); }, 1000);
  };

  PlayerMetrics.prototype.stopLiveUpdates = function () {
    if (this.liveTimer) { clearInterval(this.liveTimer); this.liveTimer = null; }
  };

  PlayerMetrics.prototype.flush = function (reason) {
    if (!this.queue.length) return;
    var batch = this.queue;
    this.queue = [];
    for (var i = 0; i < batch.length; i += METRICS_CHUNK_SIZE) {
      var chunk = batch.slice(i, i + METRICS_CHUNK_SIZE);
      var payload = {
        session_id: this.sessionId,
        page: (window.location && window.location.pathname) || "",
        video_id: this.video.id || "",
        reason: reason || "",
        sent_at: nowMs(),
        events: chunk
      };
      if (i === 0) payload.summary = this.summary();
      sendMetrics(payload);
    }
  };

  function nearestPlayerAbove(node) {
    // Prefer the review-player whose wrapper precedes `node` in document
    // order (the last one before it); fall back to the first player on the
    // page. Keeps a timestamp link relevant when several sites/videos share
    // one page.
    if (players.length === 0) return null;
    if (players.length === 1) return players[0];
    // node.compareDocumentPosition(other) returns PRECEDING when `other`
    // comes before `node` in the document. Take the last player wrap that
    // precedes the clicked link.
    var best = null;
    for (var i = 0; i < players.length; i++) {
      var rel = node.compareDocumentPosition(players[i].wrap);
      if (rel & Node.DOCUMENT_POSITION_PRECEDING) best = players[i];
    }
    return best || players[0];
  }

  var TIMESTAMP_RE = /\[(\d{1,3}):([0-5]\d)(?:\.(\d{1,2}))?\]/g;
  // Non-global twin for membership tests. A /g/ regex carries lastIndex across
  // .test() calls, which made the tree walker accept only every other text node.
  var TIMESTAMP_TEST_RE = /\[(\d{1,3}):([0-5]\d)(?:\.(\d{1,2}))?\]/;
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, PRE: 1, CODE: 1 };

  function linkifyTimestamps(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var p = node.parentNode;
        while (p && p !== root) {
          if (SKIP_TAGS[p.tagName] || p.classList && p.classList.contains("review-player")) {
            return NodeFilter.FILTER_REJECT;
          }
          p = p.parentNode;
        }
        return TIMESTAMP_TEST_RE.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });

    var targets = [];
    var n;
    while ((n = walker.nextNode())) targets.push(n);

    targets.forEach(function (textNode) {
      var text = textNode.nodeValue;
      TIMESTAMP_RE.lastIndex = 0;
      var frag = document.createDocumentFragment();
      var last = 0;
      var m;
      while ((m = TIMESTAMP_RE.exec(text))) {
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var minutes = parseInt(m[1], 10);
        var seconds = parseInt(m[2], 10);
        var frac = m[3] ? parseFloat("0." + m[3]) : 0;
        var totalSeconds = minutes * 60 + seconds + frac;
        var a = el("a", "rp-timestamp", { href: "#", "data-seconds": String(totalSeconds) });
        a.textContent = m[0];
        a.addEventListener("click", function (seconds) {
          return function (e) {
            e.preventDefault();
            var target = nearestPlayerAbove(e.currentTarget) ;
            if (!target) return;
            target.setActive();
            target.seekTo(seconds, true);
            target.wrap.scrollIntoView({ behavior: "smooth", block: "center" });
          };
        }(totalSeconds));
        frag.appendChild(a);
        last = m.index + m[0].length;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      textNode.parentNode.replaceChild(frag, textNode);
    });
  }

  document.addEventListener("keydown", function (event) {
    if (isTypingTarget(document.activeElement)) return;
    if (!active) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    var key = event.key;
    var handled = true;
    switch (key) {
      case " ":
        active.togglePlay();
        break;
      case "ArrowRight":
        active.seekBy(event.shiftKey ? TINY_SEEK : SMALL_SEEK);
        break;
      case "ArrowLeft":
        active.seekBy(event.shiftKey ? -TINY_SEEK : -SMALL_SEEK);
        break;
      case ",":
        active.stepFrame(-1);
        break;
      case ".":
        active.stepFrame(1);
        break;
      case "[":
        active.setSpeed(-1);
        break;
      case "]":
        active.setSpeed(1);
        break;
      case "m":
      case "M":
        active.toggleMute();
        break;
      case "f":
      case "F":
        active.toggleFullscreen();
        break;
      case "i":
      case "I":
        active.markIn();
        break;
      case "o":
      case "O":
        active.markOut();
        break;
      case "l":
      case "L":
        active.toggleLoop();
        break;
      default:
        handled = false;
    }
    if (handled) event.preventDefault();
  });

  function initAll() {
    var videos = Array.prototype.slice.call(document.querySelectorAll("video"));
    videos.forEach(function (video) {
      if (video.closest && video.closest("[data-review-player]")) return;
      try {
        var player = new Player(video);
        players.push(player);
      } catch (err) {
        // Leave this <video controls> exactly as authored; one bad video
        // must not stop the others or break the page.
        if (window.console && console.warn) console.warn("review-player: enhancement failed", err);
      }
    });
    if (players.length) {
      players[0].setActive();
      linkifyTimestamps(document.body);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
