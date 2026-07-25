"""Self-contained web control panel served by the bridge.

Reachable from any device on the LAN at http://<pi>:8772/ - phone, tablet or
laptop.

Everything is inlined: no CDN, no build step, no external requests. The page
ships as one string so deployment stays "copy the .py files".
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0d10">
<title>Soundbar</title>
<style>
  :root {
    --bg: #f4f5f7; --card: #fff; --fg: #14171a; --muted: #6b7280;
    --line: #e3e6ea; --accent: #2f6fed; --ok: #16a34a; --bad: #dc2626;
    --radius: 14px;
    --shadow: 0 1px 2px rgba(0,0,0,.05), 0 8px 24px rgba(0,0,0,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0b0d10; --card: #14181d; --fg: #e8eaed; --muted: #9aa3ad;
      --line: #252b33;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px 16px 40px;
    background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 460px; margin: 0 auto; }

  header { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
  header h1 { font-size: 17px; font-weight: 600; margin: 0; flex: 1; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; }
  .dot.live { background: var(--ok); box-shadow: 0 0 0 3px rgba(22,163,74,.18); }
  .dot.down { background: var(--bad); box-shadow: 0 0 0 3px rgba(220,38,38,.18); }

  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: var(--radius); box-shadow: var(--shadow);
    padding: 18px; margin-bottom: 14px;
  }

  .np { display: flex; gap: 14px; align-items: center; }
  .art {
    width: 64px; height: 64px; border-radius: 10px; flex: none;
    background: var(--bg) center/cover no-repeat;
    border: 1px solid var(--line);
    display: flex; align-items: center; justify-content: center;
    color: var(--muted); font-size: 24px; line-height: 1;
  }
  /* Placeholder glyph shows only while there is no cover art, so the slot is
     always occupied and the card height never jumps when a track starts. */
  .art::after { content: "\\266A"; opacity: .45; }
  .art.has-art::after { content: none; }
  .np-text { min-width: 0; flex: 1; }
  .np-title {
    font-size: 18px; font-weight: 600; margin: 0 0 2px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .np-sub { color: var(--muted); font-size: 14px; min-height: 21px;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .np-meta { display: flex; align-items: center; gap: 8px; margin-top: 12px; }

  /* Playing indicator. Purely decorative - the state pill carries the actual
     information - so it is aria-hidden and animated in CSS rather than by a
     timer, which keeps it off the main thread and costs nothing when idle. */
  .eq { display: none; align-items: flex-end; gap: 2px; height: 15px; }
  .eq.on { display: inline-flex; }
  .eq i {
    display: block; width: 3px; height: 30%; border-radius: 1px;
    background: var(--ok);
    animation: eq 900ms ease-in-out infinite;
  }
  .eq i:nth-child(2) { animation-duration: 640ms; animation-delay: -220ms; }
  .eq i:nth-child(3) { animation-duration: 1150ms; animation-delay: -480ms; }
  .eq i:nth-child(4) { animation-duration: 780ms; animation-delay: -90ms; }
  @keyframes eq {
    0%, 100% { height: 25%; }
    50%      { height: 100%; }
  }
  @media (prefers-reduced-motion: reduce) {
    /* Still shows that audio is playing, just without the motion. */
    .eq i { animation: none; height: 60%; }
  }
  .pill {
    display: inline-block; padding: 2px 9px; border-radius: 999px;
    background: var(--bg); border: 1px solid var(--line);
    font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
  }

  .row { display: flex; align-items: center; gap: 12px; }
  .vol-val {
    font-size: 26px; font-weight: 600; min-width: 44px;
    font-variant-numeric: tabular-nums; letter-spacing: -.5px;
  }
  input[type=range] {
    -webkit-appearance: none; appearance: none;
    flex: 1; height: 34px; background: transparent; margin: 0; touch-action: none;
  }
  input[type=range]::-webkit-slider-runnable-track {
    height: 6px; border-radius: 3px; background: var(--line);
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none;
    width: 28px; height: 28px; border-radius: 50%;
    background: var(--card); border: 2px solid var(--accent);
    margin-top: -11px; box-shadow: 0 1px 4px rgba(0,0,0,.2);
  }
  input[type=range]::-moz-range-track {
    height: 6px; border-radius: 3px; background: var(--line);
  }
  input[type=range]::-moz-range-thumb {
    width: 24px; height: 24px; border-radius: 50%;
    background: var(--card); border: 2px solid var(--accent);
  }
  input[type=range]:disabled { opacity: .4; }

  .btns { display: flex; gap: 8px; margin-top: 14px; }
  button {
    flex: 1; padding: 12px; font: inherit; font-weight: 500;
    color: var(--fg); background: var(--bg);
    border: 1px solid var(--line); border-radius: 10px; cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  button:active:not(:disabled) { transform: scale(.97); }
  button:disabled { opacity: .35; cursor: default; }
  button.on { background: var(--accent); border-color: var(--accent); color: #fff; }
  .transport button { font-size: 17px; line-height: 1; padding: 13px 12px; }

  dl { display: grid; grid-template-columns: auto 1fr; gap: 8px 16px; margin: 0; }
  dt { color: var(--muted); font-size: 13px; }
  dd { margin: 0; font-size: 13px; text-align: right;
       font-variant-numeric: tabular-nums; overflow: hidden;
       text-overflow: ellipsis; white-space: nowrap; }

  .foot { text-align: center; color: var(--muted); font-size: 12px; margin-top: 6px; }
  .err { background: rgba(220,38,38,.1); border-color: rgba(220,38,38,.35);
         color: var(--bad); }
  .hide { display: none !important; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <span class="dot" id="dot"></span>
    <h1 id="devname">Soundbar</h1>
    <span class="pill" id="ver">&mdash;</span>
  </header>

  <div class="card err hide" id="banner"></div>

  <div class="card">
    <div class="np">
      <div class="art" id="art" aria-hidden="true"></div>
      <div class="np-text">
        <div class="np-title" id="title">Not playing</div>
        <div class="np-sub" id="sub"></div>
      </div>
    </div>
    <div class="np-meta">
      <span class="eq" id="eq" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      <span class="pill" id="state">&mdash;</span>
      <span class="pill" id="elapsed">&mdash;</span>
    </div>
    <div class="btns transport">
      <button id="prev" disabled>&#9198;</button>
      <button id="playpause" disabled>&#9199;</button>
      <button id="next" disabled>&#9197;</button>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <span class="vol-val" id="volval">&mdash;</span>
      <input type="range" id="vol" min="0" max="12" step="1" value="0" disabled>
      <span class="pill" id="volmax">/ &mdash;</span>
    </div>
    <div class="btns">
      <button id="down">&minus;1</button>
      <button id="up">+1</button>
      <button id="mute">Mute</button>
    </div>
  </div>

  <div class="card">
    <dl>
      <dt>AirPlay session</dt><dd id="session">&mdash;</dd>
      <dt>Streaming to</dt><dd id="active">&mdash;</dd>
      <dt>Audio sent</dt><dd id="bytes">&mdash;</dd>
      <dt>Soundbar</dt><dd id="ip">&mdash;</dd>
      <dt>Build</dt><dd id="build">&mdash;</dd>
    </dl>
  </div>

  <div class="foot" id="foot">connecting&hellip;</div>
</div>

<script>
(function () {
  "use strict";

  // A token may be supplied as ?token=... ; keep it for later requests but
  // drop it from the visible URL so it is not left in screenshots or history.
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || sessionStorage.getItem("bridgeToken") || "";
  if (params.get("token")) {
    sessionStorage.setItem("bridgeToken", token);
    history.replaceState(null, "", location.pathname);
  }

  var $ = function (id) { return document.getElementById(id); };
  var failures = 0, lastGood = 0, artVersion = -1;
  var maxVol = 12;   // replaced by the bridge's configured cap on first poll

  // Slider ownership. While the user is touching the slider - and for a short
  // settle window afterwards - polled values must not be written back to it,
  // or the knob visibly jumps backwards to a value already superseded.
  var dragging = false, localVol = null, localAt = 0;
  var SETTLE_MS = 1200;

  function sliderIsOurs() {
    return dragging || (localVol !== null && Date.now() - localAt < SETTLE_MS);
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers,
                                 token ? { "X-Bridge-Token": token } : {});
    return fetch(path, opts);
  }

  function fmtBytes(n) {
    if (!n) return "0 B";
    var u = ["B", "KB", "MB", "GB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }

  function banner(msg) {
    var b = $("banner");
    if (!msg) { b.classList.add("hide"); return; }
    b.textContent = msg;
    b.classList.remove("hide");
  }

  function render(d) {
    var bar = d.soundbar || {}, np = d.now_playing || {}, st = d.stream || {};
    var art = d.artwork || {}, tr = d.transport || {};

    $("devname").textContent = d.airplay_name || "Soundbar";
    // Release version in the header; the git revision goes in the detail list
    // where it is useful for confirming what is actually deployed.
    $("ver").textContent = d.version ? "v" + d.version : "\\u2014";
    $("build").textContent = d.revision || "\\u2014";

    var active = !!d.session_active;
    $("dot").className = "dot " + (active ? "live" : "idle");
    // Same signal as the status dot: audio has flowed recently.
    $("eq").classList.toggle("on", active);

    var title = (np.title || "").trim(), artist = (np.artist || "").trim(),
        album = (np.album || "").trim();
    if (title || artist) {
      $("title").textContent = title || "(unknown title)";
      $("sub").textContent = [artist, album].filter(Boolean).join(" \\u2014 ");
    } else {
      $("title").textContent = active ? "AirPlay streaming" : "Not playing";
      $("sub").textContent = "";
    }

    // Reload artwork only when its version changes - it can be hundreds of kB.
    if (art.available) {
      if (art.version !== artVersion) {
        artVersion = art.version;
        $("art").style.backgroundImage = 'url("/artwork?v=' + artVersion +
          (token ? "&token=" + encodeURIComponent(token) : "") + '")';
      }
      $("art").classList.add("has-art");
    } else if (artVersion !== -1) {
      artVersion = -1;
      $("art").style.backgroundImage = "";
      $("art").classList.remove("has-art");
    }

    // Transport needs the sender's DACP credentials, which only arrive once
    // something has actually played.
    var canControl = !!tr.available;
    ["prev", "playpause", "next"].forEach(function (id) {
      $(id).disabled = !canControl;
      $(id).title = canControl ? ""
        : "Start playing first \\u2014 the sender's remote details are not known yet";
    });

    $("state").textContent = bar.state || "unknown";
    var el = (bar.elapsed || "").trim();
    $("elapsed").textContent = el && el !== "0:00:00" ? el : "\\u2014";

    if (typeof bar.max_volume === "number" && bar.max_volume > 0) {
      maxVol = bar.max_volume;
      $("vol").max = maxVol;
      $("volmax").textContent = "/ " + maxVol;
    }

    var vol = typeof bar.volume === "number" ? bar.volume : null;
    if (vol === null) {
      if (!sliderIsOurs()) {
        $("volval").textContent = "\\u2014";
        $("vol").disabled = true;
      }
    } else {
      $("vol").disabled = false;
      if (localVol !== null && vol === localVol) localVol = null;  // caught up
      if (!sliderIsOurs()) {
        $("vol").value = Math.min(vol, maxVol);
        $("volval").textContent = vol;
      }
    }

    var muted = !!bar.muted;
    $("mute").textContent = muted ? "Unmute" : "Mute";
    $("mute").classList.toggle("on", muted);

    $("session").textContent = active ? "active" : "idle";
    $("active").textContent = st.active
      ? st.active + " renderer" + (st.active > 1 ? "s" : "") : "none";
    $("bytes").textContent = fmtBytes(st.bytes);
    $("ip").textContent = bar.ip || "\\u2014";

    banner(String(bar.state || "").indexOf("error") === 0
      ? "Soundbar not responding \\u2014 is it powered on and on Wi-Fi?" : "");
  }

  function poll() {
    api("/status").then(function (r) {
      if (r.status === 401) {
        banner("Unauthorised \\u2014 append ?token=\\u2026 to the URL.");
        $("dot").className = "dot down";
        $("foot").textContent = "token required";
        return null;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (d) {
      if (!d) return;
      failures = 0; lastGood = Date.now();
      render(d);
      $("foot").textContent = "updated " + new Date().toLocaleTimeString();
    }).catch(function () {
      if (++failures >= 2) {
        $("dot").className = "dot down";
        banner("Cannot reach the bridge on the Pi.");
        $("foot").textContent = lastGood
          ? "last seen " + new Date(lastGood).toLocaleTimeString()
          : "no connection";
      }
    });
  }

  // Polling stops while the page is hidden. Left open in a background tab it
  // would otherwise hit the Pi every 2s indefinitely, for nothing.
  var timer = null;
  function startPolling() {
    if (timer === null) timer = setInterval(poll, 2000);
  }
  function stopPolling() {
    if (timer !== null) { clearInterval(timer); timer = null; }
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stopPolling(); }
    else { poll(); startPolling(); }
  });

  // Only one volume request is in flight at a time. Anything done while it is
  // outstanding is coalesced and sent on completion, so dragging cannot queue
  // a backlog of stale values on a slow network.
  var inFlight = false, queued = null;

  function sendVolume(v) {
    v = Math.max(0, Math.min(maxVol, parseInt(v, 10) || 0));
    localVol = v; localAt = Date.now();
    if (inFlight) { queued = v; return; }
    inFlight = true;
    api("/volume/" + v, { method: "POST" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (body) {
        inFlight = false;
        // Trust the server's applied value - it may have capped ours.
        if (body && typeof body.volume === "number") {
          localVol = body.volume; localAt = Date.now();
          if (!dragging) {
            $("vol").value = body.volume;
            $("volval").textContent = body.volume;
          }
        }
        if (queued !== null && queued !== v) {
          var next = queued; queued = null; sendVolume(next);
        } else { queued = null; }
      });
  }

  var debounce, slider = $("vol");

  // Pointer state is tracked explicitly: 'input' cannot distinguish a drag
  // from a keyboard step, and releasing outside the element does not always
  // fire 'change', which would leave the final position unsent.
  ["pointerdown", "touchstart", "mousedown"].forEach(function (ev) {
    slider.addEventListener(ev, function () { dragging = true; }, { passive: true });
  });
  ["pointerup", "pointercancel", "touchend", "touchcancel", "mouseup"]
    .forEach(function (ev) {
      window.addEventListener(ev, function () {
        if (!dragging) return;
        dragging = false;
        clearTimeout(debounce);
        sendVolume(slider.value);
      }, { passive: true });
    });

  slider.addEventListener("input", function () {
    $("volval").textContent = this.value;
    localVol = parseInt(this.value, 10); localAt = Date.now();
    clearTimeout(debounce);
    var v = this.value;
    debounce = setTimeout(function () { sendVolume(v); }, 120);
  });
  slider.addEventListener("change", function () {
    clearTimeout(debounce);
    sendVolume(this.value);
  });

  function nudge(delta) {
    var cur = localVol !== null ? localVol : parseInt(slider.value, 10);
    if (isNaN(cur)) return;
    var next = Math.max(0, Math.min(maxVol, cur + delta));
    slider.value = next;
    $("volval").textContent = next;
    sendVolume(next);
  }
  $("up").addEventListener("click", function () { nudge(1); });
  $("down").addEventListener("click", function () { nudge(-1); });

  $("mute").addEventListener("click", function () {
    var on = !this.classList.contains("on");
    this.classList.toggle("on", on);           // reflect immediately
    this.textContent = on ? "Unmute" : "Mute";
    api("/mute/" + (on ? "on" : "off"), { method: "POST" })
      .catch(function () {}).then(poll);
  });

  function transport(cmd) {
    api("/transport/" + cmd, { method: "POST" })
      .then(function (r) {
        return r.json().catch(function () { return null; });
      })
      .catch(function () { return null; })
      .then(function (body) {
        if (body && body.ok === false) banner(body.detail || "Command failed");
        setTimeout(poll, 400);                 // give the sender a moment
      });
  }
  $("playpause").addEventListener("click", function () { transport("playpause"); });
  $("next").addEventListener("click", function () { transport("next"); });
  $("prev").addEventListener("click", function () { transport("previous"); });

  poll();
  startPolling();
})();
</script>
</body>
</html>
"""
