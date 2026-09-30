// Run: node --test webdoc/tests/test_review_player_scrub.js
// The scrub bar's playhead and loop markers stay on the bar at either end of the video.
// A review page, 2026-09-25: the page's layout audit, from a 440 px iPhone at the end of the video, reported
// the playhead (a 12 px dot centred on its time) 6 px past the bar's right edge. render() now hands the
// position to CSS as --rp-pos and the stylesheet clamps it; the browser geometry is checked
// in a real browser; these pin the contract between the two files.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ASSETS = path.join(__dirname, '../assets');

function fakeEl() {
  const style = {};
  Object.defineProperty(style, 'setProperty', { value(k, v) { this[k] = v; }, enumerable: false });
  return { style, hidden: false, disabled: false, textContent: '', classList: { toggle() {} }, setAttribute() {} };
}

function player(videoProps, loop = {}) {
  const context = vm.createContext({ document: { readyState: 'loading', addEventListener() {} }, setTimeout() {}, clearTimeout() {} });
  const source = fs.readFileSync(path.join(ASSETS, 'review-player.js'), 'utf8');
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, 'globalThis.TestPlayer = Player; })();'), context);
  const p = Object.create(context.TestPlayer.prototype);
  const els = ['progress', 'playhead', 'buffered', 'markerIn', 'markerOut', 'loopRangeEl', 'playBtn', 'wrap', 'muteBtn', 'timeEl', 'speedEl', 'loopBtn'];
  for (const k of els) p[k] = fakeEl();
  Object.assign(p, { loopIn: null, loopOut: null, looping: false }, loop);
  p.video = Object.assign({ duration: 732.264867, currentTime: 0, paused: true, muted: false, playbackRate: 1, buffered: { length: 0 } }, videoProps);
  return p;
}

test('at the end of the video the playhead position goes to CSS as --rp-pos, not as an inline left', () => {
  const p = player({ currentTime: 732.264867 });
  p.render();
  assert.equal(p.playhead.style['--rp-pos'], '100%');
  assert.equal(p.playhead.style.left, undefined);
  assert.equal(p.progress.style.width, '100%');
});

test('loop markers hand their position to CSS the same way', () => {
  const p = player({ currentTime: 10 }, { loopIn: 0, loopOut: 732.264867 });
  p.render();
  assert.equal(p.markerIn.style['--rp-pos'], '0%');
  assert.equal(p.markerOut.style['--rp-pos'], '100%');
  assert.equal(p.markerOut.style.left, undefined);
});

function rule(css, selector) {
  const m = css.match(new RegExp(selector.replace('.', '\\.') + '\\s*\\{([^}]*)\\}'));
  assert.ok(m, `no ${selector} rule`);
  return m[1];
}

test('the stylesheet keeps the whole playhead dot and each marker on the bar', () => {
  const css = fs.readFileSync(path.join(ASSETS, 'review-player.css'), 'utf8');
  const ph = rule(css, '.rp-playhead');
  const width = Number(ph.match(/(?:^|;|\s)width:\s*(\d+)px/)[1]);
  const half = width / 2;
  assert.match(ph, new RegExp(`left:\\s*clamp\\(${half}px,\\s*var\\(--rp-pos,\\s*0%\\),\\s*calc\\(100% - ${half}px\\)\\)`));
  assert.match(ph, new RegExp(`margin-left:\\s*-${half}px`));
  const mk = rule(css, '.rp-marker');
  const mw = Number(mk.match(/(?:^|;|\s)width:\s*(\d+)px/)[1]);
  assert.match(mk, new RegExp(`left:\\s*min\\(var\\(--rp-pos,\\s*0%\\),\\s*calc\\(100% - ${mw}px\\)\\)`));
});
