# Changelog

All notable changes to glovebox-mcp are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
defined once in [`_version.py`](_version.py) (pyproject reads it dynamically) and
reported to MCP clients during the initialize handshake.

## [0.3.0] - 2026-11-01

Honesty round two, OmniParser owned by the server, and four tools agents kept
needing. 23 tools now (was 19). Found by reading `server.py` end to end and
driving every suspected path live over stdio against a throwaway Xephyr.

### Added
- **`clipboard(action="get"|"set")`** — exact text out of (or into) an app
  without OCR; per-instance (each Xephyr has its own selection).
- **`wait_for_stable(instance, timeout_ms, threshold, observe)`** — waits until
  two consecutive frames differ by less than `threshold`; reports
  `stable`/`timed_out` honestly instead of guessing a `settle_ms`.
- **`window_info(instance)`** — active window title/geometry + visible windows:
  confirm navigation without an image.
- **`app_log(instance, lines)`** — tail of the launched app's stderr.
- `screenshot(region, scale, format, quality)` — crop/zoom, downscale, JPEG;
  returns `[json, image]` like `observe` does (so "every tool returns JSON" is
  finally true).
- `press_keys` accepts space-separated **sequences** (`"ctrl+a Delete"`).
- `drag(steps=10)` emits intermediate motion events (canvases, sliders).
- `parse_screen(granularity="line"|"word")` — `basic` mode merges OCR words on
  one line into a single target (`"Sign in"`), with a `confidence` per element.
- `list_files` entries carry `bytes`, `modified`, `is_dir` and `partial`
  (in-progress `.crdownload`/`.part`), newest first.
- `upload_file(page_url_contains=…)` picks the tab when several are open;
  relative paths resolve against `files/<N>/`.
- `launch_app` returns `width`/`height`/`warnings`; recognizes the whole
  Chromium family (google-chrome(-stable), brave(-browser), microsoft-edge,
  vivaldi, opera) for CDP flags + per-instance profiles; display numbers are
  allocated under a **cross-process file lock** so parallel agents can't both
  take `:2`.
- `GLOVEBOX_CLEANUP_ON_EXIT=1` closes the instances a server process launched
  when it exits (default off: instances outlive the server on purpose).
- `list_instances`/`status` rows carry `launched_here`.
- **`vision.py`**: backend selection, GPU preflight (`nvidia-smi`: present, VRAM
  total/free, ≥ 6 GB), tesseract language detection, OmniParser lazy loader with
  structured errors (not installed, no/too-small GPU, CUDA invisible to torch,
  out of memory), and the OmniParser install (`python vision.py
  install-omniparser`) moved out of `install.sh`. `python vision.py check`
  prints the diagnosis. `status()` returns it under `vision` (requested vs.
  effective backend, reason, gpu, omniparser, tesseract) and lists `openbox`,
  `xdpyinfo`, `dbus_run_session`, `nvidia_smi` in `deps`.
- 32 tests (was 11): smoke tests for the vision rules, GPU thresholds, OCR
  grouping, `--` argument safety, bounds checks and the anchored kill pattern;
  e2e asserts the **pointer actually moved**, clipboard round-trip, screenshot
  region/scale/format, `wait_for_stable`, `window_info`, `app_log`,
  `list_files` entries, `basic` line grouping, strict-`local` error, and
  cleanup-on-exit. CI installs tesseract so the `basic` path runs.
- `ROADMAP.md` (MCP SDK 2.x migration plan, candidates, housekeeping).

### Changed
- **Default vision mode is `auto`** (was `local`): the best backend that is
  actually usable, with the reason in `status()`. An explicitly requested
  `basic`/`local` that is not usable makes `parse_screen` fail with the fix
  named — it never silently falls back.
- Tesseract languages default to `slv+eng` **filtered to what is installed**
  (`GLOVEBOX_TESS_LANG` overrides; `LC_TESS_LANG` still honored). A missing
  language used to be a hard tesseract error.
- Annotated frames and app logs live in `$TMPDIR/glovebox-<uid>/` (no
  cross-user collisions in `/tmp`); `parse_screen` returns the path.
- `settle_ms` above 5000 is clamped **and flagged** (`settle_clamped`), like
  `wait_ms` already was.
- Documentation no longer claims file isolation: the sandbox is display-level,
  apps run as your user (README, AGENTS.md, start-display.sh header).
- `scroll` sends one xdotool call with `--repeat` (capped at 100 notches).

### Fixed
- **Default-mode `parse_screen` returned a bare `No module named 'util'`** on
  any machine without OmniParser (the default was `local`). Now a structured
  error with the fix, or an `auto` fallback that says so.
- **Off-screen coordinates reported success** — xdotool clamps `(9999,9999)`
  to the edge and negatives to 0. Coordinates are now validated against the
  cached display geometry; nothing is clicked.
- **Option-like arguments** — `press_keys("-h")` made xdotool print its help
  and exit 0 ("pressed -h"); negative coordinates produced a usage dump. All
  xdotool calls now pass user values after `--`, and `press_keys` rejects
  tokens starting with `-` up front; `Invalid key sequence` is detected like
  `No such key name`.
- **Pointer motion ignored without a window manager**: Xephyr drops xdotool
  motion until a WM runs, so every click landed in the screen center while
  reporting success. `launch_app` now returns a warning when `openbox` is
  missing, `status().deps` lists it, and the e2e suite asserts the pointer
  position after `click`/`drag`.
- `open_file` reported success when the handler exited immediately (xdg-open
  with no application for the type, a missing app). It now polls like
  `launch_app` and quotes the stderr tail; the xdg-open path gets the same
  X11/D-Bus environment as `app=` (its docstring promised that; it wasn't
  true), so a GTK handler can't activate a window on the host instead.
- `_kill_xephyr` used `pkill -f "Xephyr :N "` **unanchored** — it also matched
  any shell or editor whose command line contained that string (it killed the
  audit's own shell). Anchored to `^Xephyr :N `.
- **No subprocess had a timeout**: a frozen Xephyr hung `xdpyinfo`/`xdotool`
  and therefore the tool call forever. `xdpyinfo` 5 s, `xdotool` 20 s, `xclip`
  5 s, `pkill` 5 s — a timeout is reported as a frozen display with the fix.
- `upload_file`'s CDP HTTP call honored `http_proxy` from the environment and
  timed out through the proxy; it now uses a no-proxy opener.
- `launch_app` left a Xephyr `Popen` unreaped when the display never came up.
- `clipboard`/`type_text` `xclip -i` no longer blocks on the daemonized
  selection owner's inherited pipe.
- `mss.MSS` is used when available (`mss.mss` is deprecated in mss 10).
- Stale `'claude'` vision-mode comments in pyproject/requirements; `install.sh`
  now checks for `uv` before using it.

## [0.2.1] - 2026-11-01

### Fixed
- **Fresh installs were broken since 2026-08-26.** The `mcp` dependency was
  unpinned; the MCP Python SDK 2.0 removed `mcp.server.fastmcp` (FastMCP was
  renamed `MCPServer`), so `pip install`/`install.sh` pulled 2.x and the server
  died at import with `No module named 'mcp.server.fastmcp'`. Pinned to
  `mcp[cli]>=1.9,<2` in pyproject, requirements.txt and install.sh. The 2.x
  migration is a separate, planned release.

## [0.2.0] - 2026-07-06

The honesty release: structured responses, a status tool, tests, CI, and a
release pipeline. No tool was added to or removed from the control surface
except the new read-only `status()` — 19 tools now.

### Changed
- **Structured response contract (breaking for string-parsers).** Every tool
  returns JSON `{"ok", "action", "instance", …}`. Failures set the MCP
  `isError` flag with `{"ok": false, "error", "fix"}` embedded — `fix` names
  the call that unblocks the agent (re-run `parse_screen`, check
  `list_instances`, install `xclip`, …).
- `parse_screen` elements are capped at 300 per call and `list_files` at 200
  entries, both flagged with `"truncated"`; `wait_ms` reports
  `requested_ms`/`waited_ms`/`clamped` instead of clamping silently.
- The initialize handshake now reports the real project version (it used to
  report the MCP SDK's own version).
- All 19 tool descriptions rewritten agent-first: what it does / what it
  returns / when to use it / caveats.
- `observe` mode is validated **before** the action runs — a typo like
  `"screenshoot"` errors instead of acting and silently skipping the
  observation. `observe="parse"` now embeds the parse result in the action's
  JSON (one text part + the annotated image).

### Fixed
- **Actions on a dead instance no longer report success.** xdotool's exit code
  was ignored, so clicking/typing/scrolling on a never-started display came
  back as `"[:9] clicked (10,10)"`. All of these are now errors with the fix
  named.
- `close_instance` of a non-running instance said "closed"; it now errors, and
  a real close **verifies the display actually went down** before reporting
  success.
- `launch_app` of a nonexistent or instantly-dying command looked like a
  successful launch (found live: `xterm` missing on the host produced a
  "running" instance with a black screen). The app's stderr is captured and
  quoted in the error. Failed launches also wait for their Xephyr to fully
  die, so the next `launch_app` can't land on a corpse display.
- `press_keys` with an invalid keysym was a silent no-op (xdotool exits 0 and
  "ignores" it); it now errors with the accepted syntax.
- `type_text` with non-ASCII text and no `xclip` fell back to the xdotool path
  that silently drops characters in some GTK toolkits; it now errors and names
  the fix instead of losing data. The response reports which input `method`
  was used (`xdotool` | `clipboard`).
- `scroll(0)` was a silent no-op; it now errors.
- `launch_app` no longer crashes when `openbox` is not installed (the window
  manager is a nicety, not a requirement).

### Added
- **`status()`** — read-only: version, vision backend, host display, live
  instances, and which system deps are present (xdotool, xclip, Xephyr,
  wmctrl, tesseract, OmniParser weights). Run it first; paste it into bug
  reports.
- Tests (11) + CI: a no-display smoke suite (tool registration, description
  quality, handshake version, contract shape) and a display-gated e2e suite
  that drives the real server over stdio against an X server on `:99`,
  including the unicode clipboard route and the full `launch_app` lifecycle.
  CI runs both on Python 3.10/3.12/3.13 under Xvfb.
- Release pipeline: `_version.py` single source, `server.json`, `glama.json`,
  registry publish workflow (version synced from the release tag), issue
  templates.

## [0.1.0] - 2026-07-01

Initial public code: 18 tools driving Xephyr-sandboxed apps (multi-instance,
one display per instance), selectable vision grounding
(`none`/`basic`/`local`), CDP-based browser uploads, unicode-safe typing,
`observe`/`settle_ms` one-call act-and-verify, mode-aware installer, and the
`start-display.sh` sandbox. Never tagged or released.
