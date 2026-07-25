// Minimal DOM/browser stub so the panel's real script can be executed and
// asserted against under Node, without a browser.
//
// The script under test is extracted from webui.py at run time and injected
// here, so these tests exercise the code that actually ships rather than a
// transcription of it.

'use strict';

let failures = 0;
function check(label, cond) {
  if (cond) { console.log('PASS ' + label); }
  else { console.log('FAIL ' + label); failures++; }
}
function eq(label, got, want) {
  check(label + ' (got ' + JSON.stringify(got) + ')',
        JSON.stringify(got) === JSON.stringify(want));
}

// --------------------------------------------------------------------------
// DOM stubs
// --------------------------------------------------------------------------
function makeEl(id) {
  const classes = new Set();
  return {
    id, textContent: '', value: '0', max: '12', disabled: false,
    src: null, title: '', style: {}, _handlers: {},
    className: '',
    classList: {
      add: c => classes.add(c),
      remove: c => classes.delete(c),
      contains: c => classes.has(c),
      toggle: (c, on) => { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); } else if (on) classes.add(c); else classes.delete(c); },
      _all: classes,
    },
    removeAttribute(a) { if (a === 'src') this.src = null; },
    addEventListener(ev, fn) { (this._handlers[ev] ||= []).push(fn); },
    fire(ev, self) { (this._handlers[ev] || []).forEach(fn => fn.call(self || this)); },
  };
}

const els = {};
const IDS = ['dot', 'devname', 'ver', 'build', 'banner', 'title', 'sub', 'art',
             'state', 'elapsed', 'vol', 'volval', 'volmax', 'mute', 'up',
             'down', 'session', 'active', 'bytes', 'ip', 'foot', 'eq',
             'prev', 'playpause', 'next'];
IDS.forEach(id => { els[id] = makeEl(id); });

global.document = {
  getElementById: id => els[id],
  addEventListener(ev, fn) { (this._h ||= {}); (this._h[ev] ||= []).push(fn); },
  hidden: false,
  _fire(ev) { ((this._h || {})[ev] || []).forEach(fn => fn()); },
};
global.window = {
  addEventListener(ev, fn) { (this._h ||= {}); (this._h[ev] ||= []).push(fn); },
  _fire(ev) { ((this._h || {})[ev] || []).forEach(fn => fn()); },
};
global.location = { search: '', pathname: '/' };
global.history = { replaceState() {} };
global.sessionStorage = {
  _d: {}, getItem(k) { return this._d[k] || null; }, setItem(k, v) { this._d[k] = v; },
};

// Controllable timers so tests never actually wait.
let intervals = 0;
global.setInterval = () => { intervals++; return intervals; };
global.clearInterval = () => { intervals--; };
const timeouts = [];
global.setTimeout = (fn, ms) => { timeouts.push({ fn, ms }); return timeouts.length; };
global.clearTimeout = () => {};
global.runTimeouts = () => { const t = timeouts.splice(0); t.forEach(x => x.fn()); };
global.intervalCount = () => intervals;

// fetch: records calls, resolves with whatever the test queued.
const calls = [];
let responder = () => ({ ok: true, status: 200, json: async () => ({}) });
global.fetch = (path, opts) => {
  calls.push({ path, opts: opts || {} });
  return Promise.resolve(responder(path, opts));
};
global.fetchCalls = calls;
global.setResponder = fn => { responder = fn; };

// --------------------------------------------------------------------------
// Load the real script (path passed as argv[2])
// --------------------------------------------------------------------------
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
// eval is deliberate and the whole point of this harness: it executes the
// panel's own script, extracted from webui.py by the calling test, so these
// assertions run against the code that actually ships rather than a copy that
// can drift. The input is a file this repository generates from its own
// source - never user input, never fetched - and it runs in a throwaway Node
// process with no filesystem or network stubs beyond those defined above.
eval(script);

// The IIFE ran on load and issued its first poll.
const flush = () => new Promise(r => setImmediate(r));

function payload(over = {}) {
  const base = {
    airplay_name: 'Test Speaker', version: '1.1', revision: 'abc1234',
    session_active: true,
    now_playing: { title: 'Song', artist: 'Band', album: 'Record' },
    artwork: { available: false, version: 0 },
    transport: { available: true },
    soundbar: { ip: '192.0.2.10', model: 'Renderer', state: 'PLAYING',
                volume: 7, muted: false, elapsed: '0:01:00', max_volume: 12 },
    stream: { url: '', connections: 1, active: 1, bytes: 1048576 },
    last_error: '',
  };
  return Object.assign({}, base, over);
}

function respondWith(data) {
  setResponder(() => ({ ok: true, status: 200, json: async () => data }));
}

(async () => {
  // ---- rendering -------------------------------------------------------
  respondWith(payload());
  calls.length = 0;
  document._fire('visibilitychange');           // triggers a poll
  await flush(); await flush();

  eq('title rendered', els.title.textContent, 'Song');
  eq('artist and album joined', els.sub.textContent, 'Band — Record');
  eq('version prefixed with v', els.ver.textContent, 'v1.1');
  eq('build shows revision', els.build.textContent, 'abc1234');
  check('equaliser on while playing', els.eq.classList.contains('on'));
  check('transport enabled when available', els.prev.disabled === false);
  eq('bytes humanised', els.bytes.textContent, '1.0 MB');
  eq('volume max from server', els.vol.max, 12);
  eq('volume shown', els.volval.textContent, 7);

  // ---- idle ------------------------------------------------------------
  respondWith(payload({
    session_active: false,
    now_playing: { title: '', artist: '', album: '' },
    transport: { available: false },
  }));
  document._fire('visibilitychange');
  await flush(); await flush();
  eq('idle title', els.title.textContent, 'Not playing');
  check('equaliser off when idle', !els.eq.classList.contains('on'));
  check('transport disabled without sender', els.prev.disabled === true);

  // ---- streaming without metadata --------------------------------------
  respondWith(payload({ now_playing: { title: '  ', artist: '', album: '' } }));
  document._fire('visibilitychange');
  await flush(); await flush();
  eq('whitespace-only title ignored', els.title.textContent, 'AirPlay streaming');

  // ---- volume: server value is authoritative ---------------------------
  calls.length = 0;
  setResponder(p => ({
    ok: true, status: 200,
    json: async () => (String(p).indexOf('/volume/') === 0
      ? { ok: true, volume: 12, requested: 99, capped: true }
      : payload()),
  }));
  els.vol.value = '99';
  els.vol.fire('change', els.vol);
  await flush(); await flush();
  const volCall = calls.find(c => String(c.path).indexOf('/volume/') === 0);
  check('volume POSTed', !!volCall);
  check('request clamped before sending', volCall && volCall.path === '/volume/12');
  eq('capped value adopted from response', els.volval.textContent, 12);

  // ---- one request in flight, later values coalesced --------------------
  calls.length = 0;
  let release;
  setResponder(() => ({
    ok: true, status: 200,
    json: () => new Promise(r => { release = () => r({ ok: true, volume: 5 }); }),
  }));
  els.vol.value = '5'; els.vol.fire('change', els.vol);
  await flush();
  els.vol.value = '6'; els.vol.fire('change', els.vol);
  els.vol.value = '7'; els.vol.fire('change', els.vol);
  await flush();
  const inflight = calls.filter(c => String(c.path).indexOf('/volume/') === 0);
  eq('only one volume request in flight', inflight.length, 1);

  // ---- polling lifecycle ------------------------------------------------
  const before = intervalCount();
  document.hidden = true;
  document._fire('visibilitychange');
  check('polling stops when hidden', intervalCount() < before);
  document.hidden = false;
  document._fire('visibilitychange');
  await flush();
  check('polling resumes when visible', intervalCount() === before);

  // ---- unauthorised -----------------------------------------------------
  setResponder(() => ({ ok: false, status: 401, json: async () => ({}) }));
  document._fire('visibilitychange');
  await flush(); await flush();
  check('401 explains a token is needed',
        /token/i.test(els.banner.textContent));

  console.log(failures === 0 ? 'ALL OK' : failures + ' FAILED');
  process.exit(failures === 0 ? 0 : 1);
})();
