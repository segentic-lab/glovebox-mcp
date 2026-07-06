# Changelog

All notable changes to glovebox-mcp are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
defined once in [`_version.py`](_version.py) (pyproject reads it dynamically) and
reported to MCP clients during the initialize handshake.

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
