# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AirPlay-to-UPnP/DLNA bridge. `shairport-sync` (an external process) terminates
the AirPlay session and writes raw PCM to stdout; `bridge/bridge.py` fans that out
as an endless HTTP WAV stream and points a UPnP renderer at it. It also serves a
web control panel. Runs on a Raspberry Pi, any Linux box, or macOS.

**Python 3.11+, standard library only — no pip packages, ever.** The target is a
Raspberry Pi where `pip install` is friction. If something appears to need a
package, raise it rather than adding it. Tests are `unittest`, not pytest.

## Commands

```bash
./run-tests.sh                 # whole suite
./run-tests.sh test_bridge     # one module, verbose (maps to tests.test_bridge)

python3 -W error::ResourceWarning -m unittest discover -s tests -t .  # leak check
shellcheck bridge/install.sh deploy.sh run-tests.sh tools/*.sh
./tools/verify-platforms.sh    # resolves package names in Debian/Fedora/Arch containers (Docker)

python3 tools/demo-panel.py    # real panel + real API routes, invented data (--idle)
python3 tools/diagnose.py      # end-to-end health check against a live renderer
python3 tools/level.py --wait  # measure the live stream in dBFS

python3 bridge/config.py               # every setting and its default
python3 bridge/config.py --env-names   # names install.sh/deploy.sh forward

./deploy.sh user@host           # rsync bridge/ to a host and run install.sh there
sudo ./bridge/install.sh        # install locally (no sudo on macOS — Homebrew refuses)
```

CI (`.github/workflows/run_tests.yml`) runs the suite on Linux (3.11, 3.13) and
macOS, plus shellcheck, container package resolution, and a **hygiene job that
fails on any literal `192.168.x.x` or `10.x.x.x` in `.py`/`.sh`/`.md`/`.yml`** —
use the RFC 5737 range `192.0.2.x` in examples and tests.

No test needs real hardware: UPnP runs against a fake renderer, metadata against
a temp FIFO, DACP against captured `avahi-browse`/`dns-sd` output. The panel's JS
tests skip if Node is absent.

## Architecture

Everything under `bridge/` is installed **flat into one directory** on the host,
so the modules import each other by bare name (`import api`, `from config import
Config`); `bridge.py` inserts its own directory on `sys.path`, and tests insert
`bridge/`. Keep that flat-import style.

- `bridge.py` — orchestration only: spawns shairport-sync, pumps its stdout into
  the broadcaster, runs the session-reconciliation loop, assembles `/status`.
- `streamer.py` — `PcmBroadcaster` (fan-out ring buffer, one `_Client` per
  attached renderer) and `LiveWavServer` (hand-rolled HTTP/1.1, endless WAV).
- `soundbar.py` — UPnP AVTransport/RenderingControl over SOAP, SSDP discovery,
  optional Samsung WAM extras. Pure library, no bridge state.
- `metadata.py` — parses shairport-sync's metadata FIFO: track text, cover art,
  and the DACP credentials.
- `dacp.py` — transport control sent back to the *AirPlay sender* (not the
  speaker), resolved via the host's mDNS CLI (`avahi-browse` / `dns-sd`).
- `api.py` — the status/control HTTP server; `webui.py` — the panel as one
  `PAGE` string (CSS ~17-150, JS ~209-480).
- `install.sh` — platform detection, deps, generated config, systemd unit or
  launchd plist. `deploy.sh` is just a remote wrapper around it.

Key control flows that span files:

**Session liveness is inferred, not signalled.** shairport-sync emits nothing
between AirPlay sessions, so `PcmBroadcaster.seconds_since_audio` is the only
signal; `_session_loop` engages the renderer while audio flows and releases it
after `idle_stop_seconds`. Releasing means both UPnP `Stop` *and*
`LiveWavServer.disconnect_clients()` — a `Stop` alone leaves the renderer holding
the socket, still being fed silence, and unclaimable by anything else.

**`ReengagePolicy`** exists so the bridge does not fight a user who deliberately
switched the speaker to TV/Bluetooth: retry a few times, then stand down until
the next session.

## Invariants worth knowing before editing

**One settings table.** `SETTINGS` in `config.py` generates the argparse
interface, the env var read for each option, the `bridge.env` `install.sh`
writes, the list `deploy.sh` forwards over ssh, the launchd plist, and the web
panel's settings form. Adding an option is one line there plus the matching
explicit `Config` dataclass field — never a sixth place. `test_config.py` asserts
the table and the dataclass stay in step (names, defaults, uniqueness).

**`editable` and `live` on a `Setting` are security and honesty flags.** Only
`editable=True` options are reachable from `POST /settings`, checked server-side:
the volume cap is enforced there precisely so no client can raise it, the power
commands run a shell as the service user on a LAN-exposed unauthenticated panel,
and `STATUS_*` can make the panel unreachable from itself. `live=True` means the
session loop re-reads it, so it applies without a restart; everything else is
saved but keeps running on the old value, and `describe_editable` reports `value`
(saved) and `running` separately so the panel can say which is which rather than
implying a change took effect. Settings persist by writing `bridge.env`, the same
file `install.sh` carries forward — so a panel edit survives a re-deploy, while an
explicit `VAR=x ./deploy.sh` still wins.

**The service sandbox and the config write path are coupled.** The systemd unit
sets `ProtectSystem=full`, which mounts `/etc` read-only, so writing `bridge.env`
only works because the unit also carries
`ReadWritePaths=-/etc/airplay-soundbar`. Anything that changes where config is
written (`CONFIG_DIR`) needs a matching entry, or every save fails with `EROFS`
having looked fine all the way to the last step. `/settings` reports `writable`
so the panel can grey the form out instead.

**The audio format contract spans three files.** `RATE/CHANNELS/BITS` in
`bridge.py`, the WAV header in `streamer.py`, and the `stdout` stanza
`install.sh` generates must agree. Under AirPlay 2 shairport-sync's stdout
backend defaults to `S32_LE @ 48000`, which produces full-scale noise while every
status reads healthy — hence `_probe_input_format`, which correlates the first
half-second of each session and logs `INPUT FORMAT MISMATCH`.

**Never drop a partial frame.** Every trim/discard in `_Client.push` rounds to
whole frames; a partial drop shifts the stereo interleave and swaps left/right
for the rest of the session. Drift correction is deliberately proportional and
capped (`TRIM_DIVISOR`, `MAX_TRIM_SECONDS`) so no single splice is audible; the
soft threshold must stay below the hard one or the trim branch is unreachable.

**Volume cap is enforced server-side** in `api.py` — the one path every client
goes through. Clients read `max_volume` from `/status` rather than hardcoding it,
and a capped request still returns `ok` with `capped: true`. Mutating endpoints
call `bridge.invalidate_soundbar_cache()`, without which a client that sets
volume and immediately polls sees the stale cached value and its slider snaps
back.

**Power-off and power-on must use the same method.** `AUTO_OFF` switches the
renderer off via Samsung WAM if it answers, otherwise via `POWER_OFF_COMMAND`;
`Bridge._off_method` records which, and only that method's inverse can wake it —
a smart plug that cut the power leaves nothing on the network to answer WAM.
The policy in `AutoOffPolicy` never fires before the process has held a session
(`seconds_since_audio` is infinite until then, which would otherwise power off a
speaker someone is watching TV through after every restart) and allows one
attempt per idle period.

**The test tone must travel the real path, or it proves nothing.**
`play_test_tone` feeds `PcmBroadcaster` and is paced in real time by
`_write_paced`, exactly as `_pump` feeds live audio — a renderer accepts
`SetAVTransportURI`, answers `Play` and reports `PLAYING` while emitting
nothing, so only audio that has actually been through the broadcaster and WAV
server is evidence. Writing it in one go would also overshoot `_Client`'s
two-second backlog (which a two-second tone reaches exactly) and be trimmed
away. It refuses during a session because both feed the same broadcaster and
would interleave into noise; it waits for a client to attach, because a tone
played before the renderer fetches lands nowhere. The tone moves
`seconds_since_audio` exactly as AirPlay audio does, so `session_active` goes
true for one — `test_tone.playing` is what stops `/status` claiming a session
that never existed.

**Auto-off checks the speaker's input, not just its transport state.**
`Bridge._renderer_in_use()` asks AVTransport *and* WAM `GetFunc`, because a
soundbar playing a film through HDMI-ARC reports exactly what an idle one does
over UPnP — only `GetFunc` distinguishes them, and only `wifi` is ours. It fails
open for a renderer that has never answered `GetFunc` (otherwise the feature
would work on Samsung hardware alone) and fails closed for one that used to and
has stopped, tracked by `_input_readable`. A manual `POST /power/off` skips the
check entirely: an explicit press is an instruction, not a guess.

**`install.sh` is idempotent and carries settings forward.** It regenerates
config on every run, so it reads the previous `bridge.env` for anything this run
did not explicitly set — otherwise a plain `./deploy.sh` would silently revert
the user's configuration.

## Working on the web panel

The panel lives as one string in `bridge/webui.py`. Preview it with
`tools/demo-panel.py` (that is also how `docs/web-panel.png` is produced —
regenerate it if the layout changes). Self-contained by requirement: no CDN, no
build step, no network access on the host.

Its JavaScript is genuinely executed, not string-matched: `test_webui_js.py`
extracts the `<script>` block and runs it under Node against the stub DOM in
`tests/webui_harness.js`. The harness prints `PASS <label>` / `FAIL <label>`
lines and the Python test asserts on labels, so **a new behaviour needs a case in
the harness and an assertion in the test module** — adding only one silently
tests nothing.

## Conventions

This project has had a run of bugs that were *silent* — the system reported
healthy while sounding wrong (S32 decoded as S16, `read()` vs `read1()` on a
FIFO, a partial-frame drop, a TV winning discovery). So: prefer changes that fail
loudly, and add a regression test for anything a test could have caught. `/status`
exists partly so faults are visible rather than inferred.

Comments explain *why*, especially where behaviour looks odd but is deliberate —
most of the strange-looking code here works around a measured hardware quirk, and
the comment is what stops it being "simplified" back into a bug. Match the
surrounding style.

Licensed **PolyForm Noncommercial 1.0.0** (source-available, not OSI open
source). `shairport-sync` and `nqptp` are GPL programs this drives as separate
processes — they are never vendored or modified here.
