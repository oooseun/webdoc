// Run: node --test webdoc/tests/test_review_player_metrics.js
// Deterministic tests for PlayerMetrics (review-player.js): captured event
// shape, derived rebuffer/seek/start-latency counters, throughput sampling
// off Resource Timing, and the sendBeacon/fetch delivery + chunking path.
// No real browser or video involved (see references/headless-verify.md for
// the real-browser half, run against the staging server).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture(videoOptions = {}, opts = {}) {
  const timers = new Map();
  let nextTimer = 0;
  const sentBeacons = [];
  const fetchCalls = [];
  const resourceEntries = []; // shared, mutable: tests push into this to feed sampleThroughput
  const navigatorMock = {};
  if (!opts.noBeacon) {
    navigatorMock.sendBeacon = (url, blob) => {
      sentBeacons.push({ url, body: JSON.parse(blob.parts[0]) });
      return true;
    };
  }
  const context = vm.createContext({
    document: {
      hidden: false,
      // Not "complete": the file's own bottom-of-script initAll() only runs
      // when readyState !== "loading" (see review-player.js's final lines).
      // Leaving it at "loading" makes that branch a no-op addEventListener
      // call instead of a real DOMContentLoaded wait, so PlayerMetrics can be
      // constructed directly without a real document.querySelectorAll etc.
      readyState: 'loading',
      addEventListener() {},
    },
    setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 0; }, // no real timers in a unit test; interval-triggered flush is out of scope here
    clearInterval() {},
    navigator: navigatorMock,
    fetch(url, init) {
      fetchCalls.push({ url, opts: init });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
    },
    performance: {
      getEntriesByType() { return resourceEntries; },
      setResourceTimingBufferSize() {},
    },
    location: { pathname: '/test-page' },
    addEventListener() {}, // window.addEventListener
  });
  context.window = context; // window === globalThis, as in a real page
  context.Blob = class Blob {
    constructor(parts, opts) { this.parts = parts; this.type = opts && opts.type; }
  };

  const source = fs.readFileSync(path.join(__dirname, '../assets/review-player.js'), 'utf8');
  vm.runInContext(
    source.replace(/\}\)\(\);\s*$/, 'globalThis.TestPlayerMetrics = PlayerMetrics; })();'),
    context
  );
  // Fake Date.now inside the vm's own realm: patching the outer process's
  // Date has no effect on code executing in a separate vm context.
  context.__fakeNow = 0;
  vm.runInContext('Date.now = function () { return __fakeNow; };', context);

  class Video extends EventTarget {
    constructor() {
      super();
      Object.assign(this, {
        currentTime: 0, duration: 734.8, playbackRate: 1, volume: 1, muted: false,
        paused: true, readyState: 4, networkState: 1,
        currentSrc: 'http://127.0.0.1/assets/clip.mp4', error: null,
        id: 'reveal-video',
      }, videoOptions);
      this._buffered = [];
    }
    get buffered() {
      const ranges = this._buffered;
      return { length: ranges.length, start: (i) => ranges[i][0], end: (i) => ranges[i][1] };
    }
    emit(type) { this.dispatchEvent(new Event(type)); }
  }

  const video = new Video();
  const statsPanel = { hidden: true, textContent: '' };
  const player = { video, statsPanel, renderStats() {} };
  const metrics = new context.TestPlayerMetrics(player);
  return {
    metrics, video, player, timers, sentBeacons, fetchCalls, resourceEntries,
    setNow(t) { context.__fakeNow = t; },
  };
}

test('loadedmetadata is captured with the required fields and queued', () => {
  const { metrics, video } = fixture();
  video.emit('loadedmetadata');
  assert.equal(metrics.queue.length, 1);
  const e = metrics.queue[0];
  assert.equal(e.type, 'loadedmetadata');
  assert.equal(e.duration, 734.8);
  assert.equal(e.readyState, 4);
  assert.equal(e.networkState, 1);
  assert.equal(typeof e.t, 'number');
  assert.equal(e.buffered.length, 0);
});

test('"play" is not logged as its own event (not in the requested capture list)', () => {
  const { metrics, video } = fixture();
  video.emit('play');
  assert.equal(metrics.queue.length, 0, 'play seeds start-latency only, never appears as a raw event');
});

test('a rebuffer mid-playback is counted and timed from waiting to playing', () => {
  const { metrics, video, setNow } = fixture({ paused: false });
  setNow(1000);
  video.emit('play');
  setNow(1050);
  video.emit('playing'); // playback actually starts
  setNow(2000);
  video.emit('waiting'); // a real interruption
  setNow(2400);
  video.emit('playing'); // recovered
  const s = metrics.summary();
  assert.equal(s.rebufferCount, 1);
  assert.equal(s.rebufferMs, 400);
  assert.equal(s.firstPlayLatencyMs, 50, 'first play->playing gap');
});

test('waiting before the first "playing" is start latency, not a rebuffer', () => {
  const { metrics, video, setNow } = fixture();
  setNow(500);
  video.emit('play');
  setNow(700);
  video.emit('waiting'); // pre-roll buffering, not an interruption of playback
  setNow(1200);
  video.emit('playing');
  const s = metrics.summary();
  assert.equal(s.rebufferCount, 0, 'no rebuffer: playback had not started yet');
  assert.equal(s.firstPlayLatencyMs, 700, 'start latency measured from play, unaffected by the pre-roll wait');
});

test('two waiting events with no playing between them count as one ongoing rebuffer', () => {
  const { metrics, video, setNow } = fixture({ paused: false });
  setNow(0);
  video.emit('play');
  video.emit('playing');
  setNow(100);
  video.emit('waiting');
  setNow(150);
  video.emit('waiting'); // still stalled; must not start a second timer or double-count
  setNow(300);
  video.emit('playing');
  const s = metrics.summary();
  assert.equal(s.rebufferCount, 1);
  assert.equal(s.rebufferMs, 200, 'timed from the FIRST waiting, not the second');
});

test('stalled increments stallCount independently of the rebuffer timer', () => {
  const { metrics, video } = fixture();
  video.emit('stalled');
  video.emit('stalled');
  assert.equal(metrics.summary().stallCount, 2);
  assert.equal(metrics.summary().rebufferCount, 0, 'stalled alone is not a rebuffer');
});

test('seek latency is measured from seeking to the next playing', () => {
  const { metrics, video, setNow } = fixture({ paused: false });
  setNow(5000);
  video.currentTime = 120;
  video.emit('seeking');
  setNow(5180);
  video.emit('seeked'); // currentTime settles; playback has not resumed yet
  assert.equal(metrics.summary().lastSeekLatencyMs, null, 'seeked alone does not resolve the latency');
  setNow(5300);
  video.emit('playing'); // resumes -> this is what "seek latency" means per the brief
  assert.equal(metrics.summary().lastSeekLatencyMs, 300);
});

test('a seek while paused (scrubbing) has no seek-to-playing latency, but the raw events still queue', () => {
  const { metrics, video, setNow } = fixture({ paused: true });
  setNow(0);
  video.emit('seeking');
  setNow(50);
  video.emit('seeked');
  video.emit('pause');
  assert.equal(metrics.summary().lastSeekLatencyMs, null);
  assert.equal(metrics.queue.filter((e) => e.type === 'seeking' || e.type === 'seeked').length, 2);
});

test('a MediaError on the "error" event is captured with its code', () => {
  const { metrics, video } = fixture({ error: { code: 3, message: 'MEDIA_ERR_DECODE' } });
  video.emit('error');
  const e = metrics.queue[metrics.queue.length - 1];
  assert.equal(e.type, 'error');
  assert.equal(e.errorCode, 3);
  assert.equal(e.errorMessage, 'MEDIA_ERR_DECODE');
});

test('buffered ranges and buffered-ahead are captured relative to currentTime', () => {
  const { metrics, video } = fixture({ currentTime: 45 });
  video._buffered = [[0, 50], [60, 90]];
  video.emit('progress');
  // progress drives the throughput sampler, not a logged event; use loadeddata instead
  video.emit('loadeddata');
  const e = metrics.queue[metrics.queue.length - 1];
  // e.buffered is an array built inside the vm context's own realm, so it
  // has a different Array prototype than an outer-realm array literal;
  // assert.deepEqual would report "same structure but not reference-equal".
  // Compare via JSON, which serializes to a plain string irrespective of realm.
  assert.equal(JSON.stringify(e.buffered), JSON.stringify([[0, 50], [60, 90]]));
  assert.equal(e.bufferedAhead, 5, '50 - 45');
});

test('sampleThroughput reads new Resource Timing entries for the current video src and updates the estimate', () => {
  const { metrics, video, resourceEntries } = fixture();
  resourceEntries.push(
    { name: 'http://127.0.0.1/assets/other.jpg', transferSize: 999, requestStart: 0, responseEnd: 1 },
    { name: video.currentSrc, transferSize: 125000, requestStart: 0, responseEnd: 100 } // 125000B*8/100ms = 10000 kbps
  );
  video.emit('progress'); // not a logged event itself; it only drives the throughput sampler
  assert.equal(metrics.throughputKbps, 10000);
  const sample = metrics.queue.find((e) => e.type === 'throughput_sample');
  assert.ok(sample, 'throughput sample queued');
  assert.equal(sample.kbps, 10000);
});

test('sampleThroughput ignores entries for other resources and does not resample the same entry twice', () => {
  const { metrics, video, resourceEntries } = fixture();
  resourceEntries.push({ name: 'http://127.0.0.1/assets/thumb.jpg', transferSize: 999, requestStart: 0, responseEnd: 1 });
  video.emit('progress');
  assert.equal(metrics.throughputKbps, null, 'no entry named after the video src yet');
  assert.equal(metrics.queue.filter((e) => e.type === 'throughput_sample').length, 0);
  resourceEntries.push({ name: video.currentSrc, transferSize: 50000, requestStart: 0, responseEnd: 50 });
  video.emit('progress');
  assert.equal(metrics.throughputKbps, 8000, '50000 * 8 / 50ms');
  assert.equal(metrics.queue.filter((e) => e.type === 'throughput_sample').length, 1, 'only the new entry was sampled');
});

test('statsText renders a compact, non-empty readout even with zero activity', () => {
  const { metrics } = fixture();
  const text = metrics.statsText();
  assert.match(text, /Rebuffers 0/);
  assert.match(text, /stalls 0/);
  assert.match(text, /buffered ahead/);
  assert.match(text, /throughput/);
});

test('flush sends one sendBeacon batch with events plus a summary, and empties the queue', () => {
  const { metrics, video, sentBeacons, fetchCalls } = fixture();
  video.emit('loadstart');
  video.emit('loadedmetadata');
  assert.equal(metrics.queue.length, 2);
  metrics.flush('pagehide');
  assert.equal(metrics.queue.length, 0);
  assert.equal(sentBeacons.length, 1);
  assert.equal(sentBeacons[0].url, '/api/metrics');
  assert.equal(sentBeacons[0].body.reason, 'pagehide');
  assert.equal(sentBeacons[0].body.video_id, 'reveal-video');
  assert.equal(sentBeacons[0].body.events.length, 2);
  assert.ok(sentBeacons[0].body.summary, 'first (only) chunk carries the summary');
  assert.equal(fetchCalls.length, 0, 'sendBeacon succeeded, so the fetch fallback must not fire');
});

test('flush is a no-op when there is nothing queued', () => {
  const { metrics, sentBeacons } = fixture();
  metrics.flush('interval');
  assert.equal(sentBeacons.length, 0);
});

test('flush falls back to fetch(keepalive) when sendBeacon is unavailable', () => {
  const { metrics, video, fetchCalls, sentBeacons } = fixture({}, { noBeacon: true });
  video.emit('loadstart');
  metrics.flush('pagehide');
  assert.equal(sentBeacons.length, 0, 'no sendBeacon in this browser');
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, '/api/metrics');
  assert.equal(fetchCalls[0].opts.method, 'POST');
  assert.equal(fetchCalls[0].opts.keepalive, true, 'keepalive is required so the request survives page unload');
  const body = JSON.parse(fetchCalls[0].opts.body);
  assert.equal(body.reason, 'pagehide');
  assert.equal(body.video_id, video.id);
  assert.equal(body.events.length, 1);
});

test('the queue auto-flushes at METRICS_FLUSH_AT_COUNT, and a final flush delivers the remainder', () => {
  const { metrics, video, sentBeacons } = fixture();
  // 85 events: two automatic flushes at 40 events each (queue.push triggers
  // flush("size") once length reaches METRICS_FLUSH_AT_COUNT), leaving 5
  // behind for the explicit flush below. This also exercises delivery never
  // exceeding METRICS_CHUNK_SIZE (80) per beacon, since 40 < 80.
  for (let i = 0; i < 85; i++) video.emit('ratechange');
  assert.equal(sentBeacons.length, 2, 'two auto-flushes at 40 events each');
  assert.equal(metrics.queue.length, 5, 'the remaining 5 events are still queued');
  metrics.flush('pagehide');
  assert.equal(sentBeacons.length, 3);
  const totalEvents = sentBeacons.reduce((n, b) => n + b.body.events.length, 0);
  assert.equal(totalEvents, 85);
  assert.deepEqual(sentBeacons.map((b) => b.body.events.length), [40, 40, 5]);
});
