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
  const el = {
    id, textContent: '', value: '0', max: '12', disabled: false,
    src: null, title: '', style: {}, _handlers: {},
    type: '', step: '', open: false, children: [],
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...nodes) { this.children = nodes; },
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
  // className and classList are two views of one thing in a browser. Keeping
  // them as separate properties here let an element be styled by one and
  // asserted through the other, which is a way for a stub to disagree with
  // reality and pass anyway.
  Object.defineProperty(el, 'className', {
    get: () => [...classes].join(' '),
    set(value) {
      classes.clear();
      String(value).split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
    },
  });
  return el;
}

const els = {};
const IDS = ['dot', 'devname', 'ver', 'build', 'banner', 'title', 'sub', 'art',
             'state', 'elapsed', 'vol', 'volval', 'volmax', 'mute', 'up',
             'down', 'session', 'active', 'bytes', 'ip', 'foot', 'eq',
             'prev', 'playpause', 'next', 'power', 'autooff',
             'setwrap', 'setform', 'setsave', 'setnote', 'setrestart',
             'dorestart'];
IDS.forEach(id => { els[id] = makeEl(id); });

// The settings form is generated from /settings, so the stub has to be able to
// make elements as well as look them up. Flattening the tree is enough for the
// assertions: what matters is the inputs' values and where they are sent.
function flatten(el, out = []) {
  (el.children || []).forEach(child => { out.push(child); flatten(child, out); });
  return out;
}
global.settingInputs = () => flatten(els.setform).filter(e => e.tagName === 'INPUT');
global.settingLabels = () => flatten(els.setform).filter(e => e.tagName === 'LABEL');
global.settingNotes = () => flatten(els.setform).filter(
  e => e.classList.contains('why'));

global.document = {
  createElement(tag) {
    const el = makeEl('');
    el.tagName = tag.toUpperCase();
    return el;
  },
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
    power: { auto_off_minutes: 30, off: false, seconds_until_off: null,
             last_result: '' },
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

  // ---- power state -----------------------------------------------------
  // The bridge decides whether a countdown is running; the panel must render
  // exactly what it is sent rather than reimplementing the arming rules.
  respondWith(payload({
    session_active: false,
    power: { auto_off_minutes: 30, off: false, seconds_until_off: 1080,
             last_result: '' },
  }));
  document._fire('visibilitychange');
  await flush(); await flush();
  eq('auto-off countdown shown', els.autooff.textContent, 'in 18 min');
  eq('power button offers off', els.power.textContent, 'Turn off');

  respondWith(payload({
    session_active: false,
    power: { auto_off_minutes: 30, off: true, seconds_until_off: null,
             last_result: 'powered off (idle for 30 min) via wam' },
    soundbar: { state: 'off', volume: null, muted: false, max_volume: 12 },
  }));
  document._fire('visibilitychange');
  await flush(); await flush();
  eq('power button offers the way back', els.power.textContent, 'Turn on');
  eq('powered off stated plainly', els.autooff.textContent, 'powered off');
  check('no error banner for a speaker we switched off',
        !/not responding/.test(els.banner.textContent));

  respondWith(payload({
    power: { auto_off_minutes: 0, off: false, seconds_until_off: null,
             last_result: '' },
  }));
  document._fire('visibilitychange');
  await flush(); await flush();
  eq('auto-off disabled reads plainly', els.autooff.textContent, 'disabled');

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

  // ---- power button ------------------------------------------------------
  calls.length = 0;
  respondWith(payload());
  document._fire('visibilitychange');
  await flush(); await flush();
  setResponder(p => ({
    ok: true, status: 200,
    json: async () => (String(p).indexOf('/power/') === 0
      ? { ok: false, detail: 'cannot power this renderer off' }
      : payload()),
  }));
  calls.length = 0;
  els.power.fire('click', els.power);
  await flush(); await flush();
  const powerCall = calls.find(c => String(c.path).indexOf('/power/') === 0);
  eq('power off POSTed', powerCall && powerCall.path, '/power/off');
  // A power method that quietly did nothing looks just like one that worked.
  check('failed power command explained',
        /cannot power/.test(els.banner.textContent));

  // ---- settings form -----------------------------------------------------
  const SETTINGS = {
    settings: [
      { env: 'AIRPLAY_NAME', help: 'name shown in the AirPlay menu',
        value: 'Kitchen', running: 'Soundbar', kind: 'str', live: false,
        pending: true },
      { env: 'AUTO_OFF', help: 'minutes of silence before powering off',
        value: 30, running: 30, kind: 'float', live: true, pending: false },
    ],
    restart_pending: true,
    writable: true,
    config_file: '/etc/airplay-soundbar/bridge.env',
  };
  setResponder(p => ({
    ok: true, status: 200,
    json: async () => (String(p).indexOf('/settings') === 0 ? SETTINGS : payload()),
  }));
  calls.length = 0;
  els.setwrap.open = true;
  els.setwrap.fire('toggle', els.setwrap);
  await flush(); await flush();

  eq('settings form built from the bridge', settingInputs().length, 2);
  eq('editable setting labelled by its variable',
     settingLabels().map(e => e.textContent), ['AIRPLAY_NAME', 'AUTO_OFF']);
  eq('saved value shown, not the running one',
     settingInputs().map(e => e.value), ['Kitchen', 30]);
  eq('numeric setting gets a numeric input',
     settingInputs().map(e => e.type), ['text', 'number']);
  // The mismatch is the point: a saved value that is not in effect yet.
  check('pending setting says a restart is needed',
        /restart to apply/.test(settingNotes()[0].textContent));
  check('pending setting names the running value',
        /Soundbar/.test(settingNotes()[0].textContent));
  check('settled setting shows its help instead',
        /minutes of silence/.test(settingNotes()[1].textContent));

  // Saving posts every field, and the restart prompt only appears when
  // something actually needs one.
  calls.length = 0;
  setResponder((p, opts) => ({
    ok: true, status: 200,
    json: async () => (String(p) === '/settings' && opts && opts.method === 'POST'
      ? { ok: true, applied: { AIRPLAY_NAME: 'Kitchen' },
          restart_required: ['AIRPLAY_NAME'] }
      : SETTINGS),
  }));
  settingInputs()[0].value = 'Kitchen';
  els.setsave.fire('click', els.setsave);
  await flush(); await flush(); await flush();

  const saveCall = calls.find(c => c.opts && c.opts.method === 'POST');
  check('settings POSTed as JSON', !!saveCall && !!saveCall.opts.body);
  eq('every field sent, keyed by variable',
     Object.keys(JSON.parse(saveCall.opts.body)).sort(),
     ['AIRPLAY_NAME', 'AUTO_OFF']);
  check('save reports what needs a restart',
        /restart/i.test(els.setnote.textContent));
  check('restart offered, not performed',
        !els.setrestart.classList.contains('hide'));
  check('restart not requested until asked',
        !calls.some(c => String(c.path) === '/restart'));

  els.dorestart.fire('click', els.dorestart);
  await flush(); await flush();
  check('restart requested on demand',
        calls.some(c => String(c.path) === '/restart'));

  // A host that cannot save must say so before the form is filled in, not
  // after Save fails - this is a property of the host, not of the input.
  const READONLY = Object.assign({}, SETTINGS, {
    writable: false, config_file: '/etc/airplay-soundbar/bridge.env',
  });
  setResponder(p => ({
    ok: true, status: 200,
    json: async () => (String(p).indexOf('/settings') === 0 ? READONLY : payload()),
  }));
  els.setwrap.fire('toggle', els.setwrap);
  await flush(); await flush();
  check('read-only host disables saving', els.setsave.disabled === true);
  check('read-only host disables the fields',
        settingInputs().every(e => e.disabled === true));
  check('read-only host names the file it cannot write',
        /bridge\.env/.test(els.setnote.textContent));

  // ...and a writable one leaves the form usable.
  setResponder(p => ({
    ok: true, status: 200,
    json: async () => (String(p).indexOf('/settings') === 0 ? SETTINGS : payload()),
  }));
  els.setwrap.fire('toggle', els.setwrap);
  await flush(); await flush();
  check('writable host leaves saving enabled', els.setsave.disabled === false);

  // A rejected value must name the field rather than failing vaguely.
  setResponder((p, opts) => ({
    ok: false, status: 400,
    json: async () => (opts && opts.method === 'POST'
      ? { ok: false, errors: { STREAM_PORT: 'port must be between 1 and 65535' } }
      : SETTINGS),
  }));
  els.setsave.fire('click', els.setsave);
  await flush(); await flush(); await flush();
  check('rejected value names the field',
        /STREAM_PORT/.test(els.setnote.textContent));
  check('rejected value gives the reason',
        /between 1 and 65535/.test(els.setnote.textContent));

  // ---- unauthorised -----------------------------------------------------
  setResponder(() => ({ ok: false, status: 401, json: async () => ({}) }));
  document._fire('visibilitychange');
  await flush(); await flush();
  check('401 explains a token is needed',
        /token/i.test(els.banner.textContent));

  console.log(failures === 0 ? 'ALL OK' : failures + ' FAILED');
  process.exit(failures === 0 ? 0 : 1);
})();
