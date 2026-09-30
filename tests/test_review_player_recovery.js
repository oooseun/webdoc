// Run: node --test webdoc/tests/test_review_player_recovery.js
// Deterministic media-event tests; actual browser playback is a separate check.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture(options = {}) {
  const timers = new Map();
  let nextTimer = 0;
  const context = vm.createContext({
    document: { readyState: 'loading', addEventListener() {} },
    setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
  });
  const source = fs.readFileSync(path.join(__dirname, '../assets/review-player.js'), 'utf8');
  // Expose only in this VM; production keeps its private IIFE.
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, 'globalThis.TestPlayer = Player; })();'), context);
  class Video extends EventTarget {
    constructor() {
      super();
      Object.assign(this, { currentTime: 479.805177, duration: 778.34,
        playbackRate: 1.5, volume: 0.4, muted: true, paused: true,
        readyState: 4, seeking: false, loads: 0, plays: 0 }, options);
    }
    load() { this.loads++; this.currentTime = 0; this.readyState = 0; this.paused = true; }
    pause() { this.paused = true; }
    play() { this.plays++; this.paused = false; return Promise.resolve(); }
    emit(name) { this.dispatchEvent(new Event(name)); }
    ready() { this.readyState = 1; this.emit('loadedmetadata'); this.readyState = 4; this.emit('canplay'); }
  }
  const video = new Video();
  const player = Object.create(context.TestPlayer.prototype);
  Object.assign(player, { video, reloadBtn: { disabled: false }, recoveryStatus: { textContent: '' }, render() {} });
  return { player, video, timers };
}

test('paused reload retains media element, time, audio settings and rate', () => {
  const { player, video, timers } = fixture();
  let externalListener = 0;
  video.addEventListener('canplay', () => externalListener++);
  player.reloadVideo();
  player.reloadVideo(); // rapid second click must not start another load
  assert.equal(video.loads, 1);
  assert.equal(player.reloadBtn.disabled, true);
  video.ready();
  assert.equal(player.video, video);
  assert.equal(externalListener, 1);
  assert.equal(video.currentTime, 479.805177);
  assert.equal(video.paused, true);
  assert.equal(video.plays, 0);
  assert.equal(video.playbackRate, 1.5);
  assert.equal(video.volume, 0.4);
  assert.equal(video.muted, true);
  assert.equal(player.reloadBtn.disabled, false);
  assert.equal(timers.size, 0);
});

test('playing reload resumes only after target is ready; start at zero works', () => {
  const { player, video } = fixture({ currentTime: 0, paused: false });
  player.reloadVideo();
  assert.equal(video.plays, 0);
  video.ready();
  assert.equal(video.currentTime, 0);
  assert.equal(video.plays, 1);
  video.emit('canplay'); // completion handlers must be removed
  assert.equal(video.plays, 1);
});

test('timeout allows retry at original position, without an automatic reload loop', () => {
  const { player, video, timers } = fixture();
  player.reloadVideo();
  [...timers.values()][0]();
  assert.equal(player.reloadBtn.disabled, false);
  assert.equal(timers.size, 0);
  assert.equal(video.loads, 1);
  assert.match(player.recoveryStatus.textContent, /7:59/);
  player.reloadVideo();
  video.ready();
  assert.equal(video.currentTime, 479.805177);
});

test('media error releases the button and does not restore on a late event', () => {
  const { player, video, timers } = fixture();
  player.reloadVideo();
  video.emit('error');
  assert.equal(player.reloadBtn.disabled, false);
  assert.equal(timers.size, 0);
  video.ready();
  assert.equal(video.currentTime, 0);
  assert.equal(video.plays, 0);
});

test('blocked resume leaves a useful play instruction', async () => {
  const { player, video } = fixture({ paused: false });
  video.play = () => Promise.reject(new Error('autoplay blocked'));
  player.reloadVideo();
  video.ready();
  await Promise.resolve();
  assert.match(player.recoveryStatus.textContent, /[Pp]ress play/);
});
