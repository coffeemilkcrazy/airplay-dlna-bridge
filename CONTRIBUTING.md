# Contributing

Thanks for looking. The most useful contribution is **a report from a renderer
other than a Samsung HW-N850** — that is the only hardware any of this has been
verified against, and everything else is inference.

## Reporting a renderer

Run the diagnostic and paste its output:

```bash
python3 tools/diagnose.py
```

It reports what it discovered, whether the control plane answers, and whether
the device actually fetches and sustains audio. A failure at step 4 with
`HEAD` but no `GET` is the interesting case — it means the renderer accepted the
command and then refused the media, and knowing which devices do that is
genuinely valuable.

Please include the make and model, and whether it worked, partly worked, or not
at all. "Doesn't work" without the diagnostic output is hard to act on.

## Development

No dependencies. Standard library only, on purpose — the target is a Raspberry
Pi where `pip install` is friction and a broken virtualenv is a support burden.
Please keep it that way; if something seems to need a package, say so in the
issue first.

```bash
./run-tests.sh                 # everything
./run-tests.sh test_bridge     # one module, verbose

# resource leaks: unclosed sockets surface as exhaustion days later
python3 -W error::ResourceWarning -m unittest discover -s tests -t .

shellcheck bridge/install.sh deploy.sh run-tests.sh tools/*.sh
./tools/verify-platforms.sh    # package names across apt/dnf/pacman (Docker)
```

CI runs all of the above on Linux and macOS, across Python 3.11 and 3.13.

### Working on the web panel

The panel lives as one string in `bridge/webui.py`. To see it without a
renderer or a Pi:

```bash
python3 tools/demo-panel.py            # playing, with cover art
python3 tools/demo-panel.py --idle     # nothing playing
```

That serves the real page and the real API routes against invented data, which
is also how `docs/web-panel.png` is produced — please regenerate it if you
change the layout.

Its JavaScript is genuinely tested: `tests/test_webui_js.py` extracts the
`<script>` block and executes it under Node against a stub DOM, so the slider,
polling and volume-coalescing logic is covered. Add to that rather than relying
on the eye.

## What tends to matter here

This project has had a run of bugs that were **silent** — the system reported
healthy while sounding wrong. A sample:

- `S32_LE` decoded as `S16_LE`: full-scale noise, while the renderer reported
  `PLAYING` and streamed megabytes
- `read()` instead of `read1()` on a FIFO: blocked forever, so track metadata
  never appeared and nothing errored
- Dropping a partial frame: shifted the stereo interleave, swapping left and
  right for the rest of the session
- A television winning discovery: accepts `Play`, then never fetches

So: prefer changes that fail loudly, and add a regression test for anything a
test could have caught. `/status` exists partly so faults are visible rather
than inferred.

## Style

Match the surrounding code. Comments explain *why*, particularly where
behaviour looks odd but is deliberate — most of the strange-looking code here
is working around a measured hardware quirk, and the comment is what stops it
being "simplified" back into a bug.

## Licence

Contributions are accepted under [PolyForm Noncommercial 1.0.0](LICENSE), the
project's licence. Note it is not an OSI-approved open-source licence:
commercial use is not permitted.
