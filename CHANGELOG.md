# Changelog

All notable changes to glovebox-mcp are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
defined once in [`_version.py`](_version.py) (pyproject reads it dynamically) and
reported to MCP clients during the initialize handshake.

## [Unreleased]

### Added
- **Taskbar for a person working in the window.** `launch_app(..., panel=true)`, or
  `GLOVEBOX_PANEL=1` for every launch and `start-display.sh`, adds a 32 px bar on the bottom edge:
  an Applications menu built from the machine's `.desktop` entries, Run…, a button per open
  window, a clock. With it `command` may be empty (an instance with just the desktop). The launch
  result has `panel`, `status()` has `panel_default` and `deps.gtk3`. Needs GTK 3 for the system
  python (`gir1.2-gtk-3.0`, added to `install.sh` and the container image); without it the launch
  warns and starts without the bar.
- **Right-click on the empty desktop** of every instance lists the installed apps by group and
  the open windows (it replaces the distribution's openbox menu; keys and theme are untouched).
- `server.py run N [--wait-window S] -- COMMAND` starts a program inside instance N with the
  handling of `start_app`; the taskbar, the desktop menu and link handling use it.

### Changed
- **Whatever is opened from inside an instance stays inside it.** Menu and taskbar launches go
  through the launcher. A link clicked in an app resolves the default browser to an overriding
  `.desktop` entry (first in the instance's `XDG_DATA_DIRS`) and opens in a browser on the
  instance — as a new tab when one already runs there — instead of in the browser on the host
  desktop. The old openbox menu started programs with the host's Wayland socket in reach.
- Firefox, LibreWolf and Thunderbird get a per-instance profile (`profiles/<name>-<N>`), so a
  copy running on the host no longer answers "already running" or takes the launch.
- Snap programs are no longer wrapped in a private session bus: snapd refused to start them
  there ("is not a snap cgroup").
- The window manager and what it starts no longer inherit `WAYLAND_DISPLAY`.

### Known limits
- Single-instance programs that keep their lock outside glovebox's reach (Electron apps,
  Telegram, KeePassXC) still hand over to a copy running on the host; the taskbar reports it.
- Snap Firefox did not connect to a nested display in testing (`cannot open display`).

## [0.7.1] - 2026-11-24

### Added
- **Interactive `./install.sh`.** Run on a terminal without a mode, the installer asks where
  the sandbox should run (this desktop or a Docker container), which vision backend or
  container app set, and whether to install the Claude Code skill. It shows what it detected
  (OS, GPU, Docker), refuses the options this machine cannot run and says why, prints the plan
  as the equivalent non-interactive command, and installs only after a final confirmation.
  Every question has a default; end of input takes the defaults, so it cannot hang a script.
- `./install.sh --plan` prints what would be installed and stops; `-y`/`--yes` never asks.

### Changed
- `./install.sh` with no arguments on a terminal now asks instead of silently running `auto`.
  Named modes, `--yes` and non-terminal runs (pipes, CI) are unchanged.

## [0.7.0] - 2026-11-23

### Added
- **Accessibility grounding.** `parse_screen`, `find_elements` and `wait_for_text` now ask the
  app before they look at pixels (`source="auto"`; `"a11y"` and `"vision"` force one side), and
  the reply's `"source"` says who answered:
  - `browser-dom` — the active tab of a Chromium-family browser, read over DevTools by an
    in-page walk of the DOM: roles, accessible names, states, values, `<select>` options, open
    shadow roots and same-origin iframes. A long Wikipedia article is listed in ~0.2 s
    (Chrome's own accessibility tree took ~4 s to build and ~7 s to measure on the same page,
    which is why it is not used).
  - `at-spi` — native GTK, Qt and LibreOffice apps through the display's own accessibility bus
    (`a11y_tree.py`, system python).
  Elements carry `role`, `states`, `value`; with vision mode `none` this is grounding where
  there was none. Apps exposing fewer than 3 elements fall back to vision with the reason in
  `a11y_unavailable`.
- **`act(element, action, value)`** (31 tools) — press a control, set a value, focus or scroll
  an element through the app's accessibility interface: no pointer, no keyboard, no
  coordinates. Returns `before`/`after` states and `verified` for `set_value`.
- Accessibility ids are handles, not remembered pixels: `click_element`, `fill(element=)` and
  `act` look the element up again first. A moved element is hit where it is (`moved_from`),
  one outside the viewport is scrolled in (`scrolled`), one that is gone fails with
  `stale: true` and nothing done.
- `find_elements` finds elements scrolled out of view in browsers (`"onscreen": false`).
- `observe="elements"` on every action tool: the element list alone, no image.
- `observe="changes"`: only what the action changed — elements that appeared (with usable
  ids), disappeared, or changed state/value, a focus move, a page navigation — from the app's
  accessibility information before and after. Fails before acting for apps that expose none.
- `screenshot(save_as="name.png")` writes the image into the staging folder and returns JSON
  only.
- MCP tool annotations on all tools (`readOnlyHint` on the twelve that only look, wait or
  list; `destructiveHint: false` on launch/start/put_file/move_mouse/open_file).

- **Container mode** — the same server in a Docker container, for macOS/Windows hosts and for
  real isolation from the user's files. The container runs a virtual desktop (Xvfb + openbox)
  with a web viewer (noVNC on `127.0.0.1:6080`); instances are Xephyr windows on it exactly as
  on a real desktop, so multi-instance, the keymap copy and the clipboard bridge are unchanged.
  The container is long-lived and each agent session attaches with
  `docker exec -i glovebox glovebox-server`, so instances outlive sessions and agents share
  one desktop. New: `Dockerfile`, `docker-compose.yml`, `container.py` (stdlib-only launcher:
  `up`/`down`/`status`/`build`), `./install.sh container`, `GLOVEBOX_MODE=container` (an
  existing install hands its sessions to the container), `status().container`. The image is
  built and driven on amd64 and arm64 in CI (`tests/container_smoke.py`, 16 checks).
- **Container app sets** — `--build-arg APPS=browser|basic|full` (`./install.sh container
  --apps full`, `GLOVEBOX_APPS=full python3 container.py build`): Chromium; plus an editor, file
  manager, PDF and image viewers, calculator, archiver and terminal; plus LibreOffice, GIMP,
  Inkscape, Firefox ESR and VLC. `EXTRA_APT` adds any Debian package on top;
  `python3 container.py install PKG…` adds packages to the running container.
  `status().container.apps` lists the set and what is installed.
- **Container vision from the host's GPU** (opt-in, Linux hosts) —
  `GLOVEBOX_CONTAINER_VISION=host` / `./install.sh container --vision host` mounts, read-only,
  one directory holding a hard link to the host's vision-service socket; the container's
  `parse_screen` then goes through that service (`"via": "host-service"`), so the image carries
  no models and native and container agents share one model load. The service publishes the
  link itself when the directory exists (`vision_service.publish_share`), `container.py`
  starts it at session start, and when it is down the container falls back to OCR with the
  reason and the fix. New CLI: `vision_service.py start`.
- `.github/workflows/docker-publish.yml` — pushes the image to GHCR; manual trigger only.

### Changed
- `find_elements`/`wait_for_text` no longer need a vision backend when the app exposes
  accessibility; `status().tips` and the AT-SPI note say so.

### Fixed
- `launch_app` on an unwritable staging folder raised a raw `PermissionError`; it now fails
  structured, with the fix (found by the container CI job, whose runner is not uid 1000 —
  the container now gives its sandbox user the id of the mounted data folder's owner).

### Known limits
- Container: Chromium runs with `--no-sandbox` under Docker's default seccomp profile
  (reported by `status()`); no image carries GPU vision models (the host's service can be
  shared instead, on Linux); Firefox in the `full` set is read by OCR, not accessibility.
- Accessibility: cross-origin iframes are counted (`frames_skipped`), not read; the browser's
  own toolbar is not listed; Tk/Xlib apps expose nothing.

## [0.6.2] - 2026-11-20

Reported by an agent that lost its MCP connection three times in one session while
reading ad-platform data with `browser(action="eval")`.

### Fixed
- **A tool result containing half an emoji no longer kills the connection.** A string
  with an unpaired UTF-16 surrogate (a JS string cut with `slice()`, a clipboard
  fragment, a window title) cannot be encoded as UTF-8; serialising the reply crashed
  the server's stdout writer, the call never returned and every later call failed with
  "Connection closed" until `/mcp`. All tool replies now replace such characters with
  U+FFFD and say so in `text_repaired`. (The size of the result was not the cause:
  3 MB results returned in 1 s before and after.)
- `browser(action="eval")` no longer gives up after a fixed 15 s with a fix line about
  the browser not being Chromium. The timeout is now a parameter, bounds the whole
  reply rather than each socket read, and the failure says the script keeps running
  and how to poll for its result.

### Added
- `browser(timeout_s=…)` — 1–600 s for an eval or text call (default 30). A timeout
  fails with `timed_out: true`; the connection stays usable.
- `browser(action="eval")` caps the result at `max_chars` (default 100000, max
  1000000) characters of JSON: larger results return `truncated: true`, `length` and
  the cut `result_json` instead of `result`.
- `browser(action="info")` returns `viewport` (`dpr`, `left`, `top`, `css_width`,
  `css_height`), the transform from the page's CSS pixels to click/screenshot pixels.

### Changed
- `browser(max_chars)` defaults to 0 = the per-action default (20000 for `text`,
  100000 for `eval`); eval results above 100000 characters, previously unlimited, now
  need an explicit `max_chars`.

## [0.6.1] - 2026-11-15

Found by driving real apps (a browser form with 35 cases; GTK, Qt and Tk test
forms; Kate, LibreOffice Writer/Calc, GIMP, Inkscape) through the MCP interface.

### Fixed
- **Qt/KDE apps ignored every key press** (agent and person) since 0.4.0. The
  host-keymap copy set only the core keyboard properly; the XTEST keyboard
  (xdotool) and Xephyr's own keyboard were left with a map the X server could
  not serialise, the core keyboard adopted it on the first injected key, and
  xkbcommon clients gave up ("qt.qpa.xkeyboard: failed to compile a keymap").
  The keymap is now uploaded to every keyboard device.
- **Qt/KDE apps never showed a window on a Wayland host**: Qt follows
  `XDG_SESSION_TYPE` / `QT_QPA_PLATFORM`, not `DISPLAY`. Launched apps now get
  `XDG_SESSION_TYPE=x11`, `QT_QPA_PLATFORM=xcb` (plus SDL/Clutter/Mozilla
  equivalents) and Qt's accessibility switches.
- **LibreOffice opened outside the sandbox or not at all**: it is
  single-instance per profile, so a launch was handed to a LibreOffice already
  running on the host or left over from a closed instance. Each instance now
  gets its own profile (`profiles/libreoffice-<N>`).
- **`close_instance` left processes behind** (LibreOffice's `soffice.bin`
  survives its display). Apps start in their own process group and the whole
  group on that display is ended on close.
- `fill` in a browser typed into a field elsewhere on the page when the click
  hit the browser's toolbar or an element that refuses focus; it now fails
  with nothing typed when the focused field is not at the clicked point
  (clicking a `<label for>` still fills its input).
- `fill` refused the document area of LibreOffice Writer: the app reports the
  focus only as its window frame. A window-level focus is now accepted when
  the widget at the clicked point is editable, and reported as unknown
  (`focus_checked: false`) otherwise — never as a miss.
- Qt leaves the focused flag on a combo box's current item; with several
  widgets claiming focus the one under the click is used.
- `fill`'s read-back marker could reach the host clipboard through the bridge;
  the bridge never mirrors it.

## [0.6.0] - 2026-11-14

### Added
- `put_file(name, content, encoding, overwrite)` — write a text or base64
  file (max 10 MB) into the instance's staging folder, so an agent without
  shell access can hand the sandbox a file it produced and then
  `open_file`/`upload_file` it by name. Confined to `files/<N>/` (absolute
  paths, `..` and symlinks out are refused), atomic, refuses to replace an
  existing file without `overwrite=true`. 29 tools. (Glama's completeness
  review listed "no file-write tool" as a gap.)
- `fill(text, x, y | element, submit, verify, allow_password)` — click a
  field, check the focus, replace its content and read the value back in one
  call. 30 tools. (A dogfooding agent repeated click → select-all → paste →
  screenshot about 30 times in one session.)
  - Focus check BEFORE typing: the page's DOM in browsers, the accessibility
    bus (AT-SPI, new helper `a11y_focus.py` under the system python3) in
    native apps. Focus not on an editable field, or on a field whose rectangle
    does not contain the click (a label was clicked, the focus stayed put),
    fails with nothing typed and `focus: {role, name, rect}`.
  - Password fields are refused unless `allow_password=true`.
  - `focus_checked: false` + a warning + `click_effect: "changed"|"none"`
    where nobody can tell the focus (canvas, games, Xlib-only apps, iframes,
    AT-SPI not installed) — a blind fill no longer reads as a clean success.
  - With `element`, the ScreenParser detector warns when the target is a text
    label and names the nearest input (`suggest_element`).
  - Read-back (`verified: true|false|null`): DOM in browsers, select-all +
    copy behind a sentinel elsewhere (clipboard restored).
  - `install.sh` installs `at-spi2-core gir1.2-atspi-2.0 python3-gi`;
    `status().deps` reports `atspi` and `xprop`.
- `hit` on `click` / `double_click` / `click_element` / `fill`: in
  Chromium-family browsers, the element under the point (role, name,
  `checked_before`, `disabled`, `href`), read from the DOM before the click;
  `{"role": "browser-ui"}` outside the page. Absent for native apps.
- `observe_region="x,y,w,h"` on every tool that takes `observe`: the returned
  screenshot is cropped to that rectangle (implies `observe="screenshot"`,
  validated before the action).
- `scroll(direction="up"|"down"|"left"|"right")` next to the signed amount;
  the reply names the `direction` actually scrolled.
- `warnings` on action tools when the pointer is not where the server left
  it — a person or another agent is using the instance. Pointer movement
  only; typing cannot be detected.
- `status()` returns `tips`, and the server's MCP `instructions` name
  `find_elements` / `click_element` / `wait_for_text` / `fill`: clients that
  load tool schemas lazily never showed them to the agent, which then
  clicked by pixel and misclicked.

### Fixed
- A second click (or `move_mouse`, `scroll`, a `drag` step) on the spot where
  the pointer already was hung for ~15 s: `xdotool mousemove --sync` waits
  for a movement that never comes. The move is skipped when the pointer is
  already there.

## [0.5.1] - 2026-11-14

### Changed
- **One-command installer**: `./install.sh` (= `auto`) probes the GPU and
  installs the best backend the machine can run (NVIDIA ≥ 6 GB → `local`
  with OmniParser + ScreenParser, else `basic`); tesseract is installed in
  every mode so the server's run-time fallback has something to fall back
  to; Python deps come from `pyproject.toml`'s extras instead of a second
  list in the script; it writes `.mcp.json` for Claude Code next to
  `mcp-config.json` and installs the Claude Code skill when `~/.claude`
  exists. Flags: `--no-skill`, `--no-screenparser`, `--dev`. `.vision-mode`
  records what was asked for (`auto` by default), not what was chosen.

## [0.5.0] - 2026-11-14

### Added
- **Five tools** (23 → 28): `find_elements(query, type)` (parse + filter in one
  call, ids ready for `click_element`), `wait_for_text(text, appear)` (OCR/parse
  polling with an honest `timed_out`), `browser(action=info|navigate|text|eval)`
  (CDP: URL/tabs, navigation that waits for load, rendered page text, JS
  values), `manage_window(action, window_id)` (activate/close/minimize/
  maximize/restore/move/resize), `start_app(command)` (another app on an
  existing instance).
- `click(count, modifiers)` — double/triple and ctrl/shift/alt-clicks (keys
  released whatever happens); `click_element(modifiers)`;
  `type_text(clear, submit)`; `scroll(x, y, axis)` with `applied`/`clamped`
  reported (the 100-notch cap used to be silent); `window_info` reports the
  pointer position and unnamed windows; `list_instances` rows carry the active
  window `title`; `close_instance(all_launched=true)`; `parse_screen` results
  carry `detector` and `fallback_reason`.
- A Claude Code **skill** (`skills/glovebox/SKILL.md`, `./install.sh skill`)
  that teaches an agent the observe → act → verify loop and the gotchas.
- `_paths.py`: one 0700, ownership-checked per-user temp dir for frames and
  logs (it used to be created with the default mode and no check).

### Changed
- **The host display is refused** — `instance=0` (or whatever
  `GLOVEBOX_HOST_DISPLAY` is) used to drive the real desktop; every tool now
  validates the instance id (1–255, not the host, display answering).
- **A failed observation no longer fails the action** — `click(observe=
  "parse")` whose parse errors returns `ok: true, observed: false,
  observe_error` instead of an isError that made agents click twice.
- **Per-instance serialization**: tools that touch one instance's cursor,
  keyboard or clipboard run under that instance's lock (SDK 2.x overlaps
  calls; a `mousemove` could interleave with another call's `click`).
- `press_keys` validates every token against the X keysym table (libX11)
  BEFORE pressing — "nothing was pressed" is now true also for `'ctrl+a Foo'`.
- `type_text` restores the clipboard after a unicode paste also when it was
  empty (it used to leave the typed text there, and the host bridge mirrored
  it to the desktop), waits 0.3 s for slow readers, never fails on the
  restore; long ASCII text scales the xdotool timeout instead of timing out
  mid-string.
- `launch_app` validates `size` and the command before starting a display,
  logs Xephyr's stderr (quoted on failure, with `poll()` instead of a blind
  10 s wait), closes the display again on any later failure, and reports a
  failed keymap copy / bridge start in `warnings` instead of silently.
- `drag` releases the button even when a motion step fails; `scroll` reports
  the clamp; `clipboard(get)` caps at 100 000 characters (`truncated`) and
  distinguishes "empty" from "xclip failed"; `list_files` fails on an
  unreadable folder instead of returning 0 entries.
- `list_instances` forgets instances whose window was closed by hand (their
  number is reused; stale `up: false` rows are gone) and probes :1–:24.
- The vision backend is re-resolved every 20 s (installing OmniParser or
  freeing the GPU used to need a server restart); `auto` no longer picks
  `local` when the GPU lacks free memory to load the models (unless the
  shared service already holds them).
- Chromium is no longer started with `--remote-allow-origins=*` (any local
  page could drive the browser); the CDP client never sent an Origin header
  anyway.
- Missing binaries (xdotool, xdpyinfo, xclip, wmctrl) are structured errors
  with the package named, not raw exceptions; a recorded pid is only killed
  while it still is that process.
- The vision service: frame limits (64 KiB header, 64 MiB payload), one
  thread cap, in-flight requests counted from accept (an upload in progress
  can no longer be cut off by the idle exit), shutdown drains running parses,
  SIGTERM handled, accept errors survived, only its own socket file removed
  (a newer service's was deleted before), status polls no longer reset the
  idle clock (the GPU was pinned forever on a monitored machine), a
  reachable-but-slow service is reported as busy instead of loading a second
  model copy in the server, malformed replies are errors, annotated frames
  are deleted after they are sent, the start-lock is per socket path.
- ScreenParser merge: a container of a different class keeps its child (a
  Text Input no longer loses its clear icon), identical boxes tie-break by
  confidence, degenerate boxes are dropped before captioning (a 0.4 px box
  used to shift every following caption onto the wrong element), centres are
  clamped into the display, `box_threshold` raises the detector floor.
- `basic` OCR: lines are split at wide gaps (a menu bar "File   Edit   View"
  is three targets, not one whose centre is blank), tesseract runs under a
  30 s timeout.
- **Optional typed detector for `local` mode** (`python vision.py
  install-screenparser`): ScreenParser (docling-project, ICML 2026, YOLO11-L,
  55 UI classes, Apache-2.0, 153 MB) replaces OmniParser's icon-only detector
  inside the same pipeline (easyocr text, Florence-2 captions). `parse_screen`
  elements then carry a real `type` (button, link, tab, input, checkbox,
  radio, switch, select, slider, image, icon, …) and the raw `class`; buttons
  and tabs get their visible text as `label`. Auto-selected once installed,
  `GLOVEBOX_DETECTOR` forces either; `status()` reports
  `vision.omniparser.detector`; the vision service reports which one it
  loaded. Layout containers (window, toolbar, table, …) are dropped before the
  merge so they cannot swallow the text inside them. Measured on the same
  1440x900 page: 110 typed elements vs. 93 untyped, 1.7 s per parse either
  way. Merge rules are unit-tested without a GPU.

### Fixed
- `local` mode failed on a fresh install with `OmniParser import failed: No
  module named 'openai'`: OmniParser's `util/utils.py` imports the OpenAI SDK
  unconditionally for a GPT-captioning mode we never call. The installer now
  makes that import optional (as it already did for PaddleOCR), and the
  patch is anchored so re-running `install-omniparser` cannot wrap the same
  import twice (which produced a syntax error). First real-GPU run of the
  shared vision service on an RTX A5000 Laptop (16 GB): models load in 12 s,
  a 1440x900 parse takes 1.7 s warm, the service holds 2.2 GB VRAM.

## [0.4.0] - 2026-11-14

### Added
- **Host bridge** (`host_bridge.py`): every instance is now usable by a
  person as well as by the agent. `launch_app` (and `start-display.sh`)
  copy the host's keyboard layout into the new display and start one
  detached bridge process per instance that mirrors clipboard TEXT between
  the host and the instance (both directions, ~0.5 s) and keeps the keymap
  equal to the host's (re-copied within 5 s if Xephyr reloads its own map or
  the host layout is switched). The launch result carries
  `keyboard: {method, host_layout, verified}` and `clipboard_sync`;
  `list_instances` rows carry `clipboard_sync`; `status()` reports the host
  layout and the `xkbcomp`/`setxkbmap` deps. `GLOVEBOX_CLIPBOARD_SYNC=0`
  turns the bridge off, `GLOVEBOX_KEYBOARD=si` forces a layout. The bridge
  exits by itself when the display goes down; tests cover the sync policy
  without X and the real thing on :99. New system package: `x11-xkb-utils`.
- **Shared vision service** (`vision_service.py`): in `local` mode OmniParser
  is loaded by ONE process per machine and served over a Unix socket, instead
  of every agent's server loading YOLO + Florence-2 + easyocr into the GPU
  again (1.5–2.5 GB and a CUDA context each). The first `parse_screen` starts
  it under a cross-process lock; parses are serialized on the GPU and the
  result carries `"via": "service"`, `queued_ms` and `parse_ms`; it exits
  after `GLOVEBOX_VISION_IDLE_MIN` (15) idle minutes. Unreachable or
  un-startable → the server parses in-process as before (`"via":
  "in-process"`, re-tried after 60 s). Parser errors (GPU too small, out of
  memory) cross the wire as the same structured `{error, fix}`. `status()`
  reports it under `vision.omniparser.service`; `GLOVEBOX_VISION_SERVICE=0`
  disables it; `python vision_service.py status|stop`. 10 tests cover framing,
  lifecycle (spawn once, reuse, idle-exit, shutdown), error propagation and
  the server's client/fallback paths without a GPU (`--fake` parser).

### Fixed
- **AltGr characters could not be typed by hand into a sandbox window**
  (`@ { } [ ] | ~ €` on Slovenian/Croatian/German/… layouts). Xephyr copies
  the host's keysyms but files the AltGr key (keycode 108) under plain
  `Alt`, so AltGr+V reached the app as Alt+v. Reproduced with a key logger
  inside the instance, fixed by copying the host's complete keymap
  (`xkbcomp HOST INSTANCE`); the e2e test asserts the bare-Xephyr bug and
  its absence after launch. The agent's `type_text` was never affected
  (xdotool addresses keysyms).
- **Copy/paste between the desktop and a sandbox window did nothing** — an
  X selection lives in one X server. Now bridged (text) by the per-instance
  bridge; on Wayland desktops the compositor relays it on to native apps
  (verified with KDE's klipper).
- `type_text` leaves the clipboard as it found it after its unicode paste,
  so agent text does not end up on the person's host clipboard through the
  bridge.

### Changed
- **MCP Python SDK 2.x** (`mcp[cli]>=2.0,<3`; 1.x is no longer supported).
  `FastMCP` → `MCPServer("glovebox", version=…)`, which also retires the
  private `_mcp_server.version` hack — the handshake version is now a public
  constructor argument. `ToolError`/`Image` import from
  `mcp.server.mcpserver.*`. The server also sends `instructions` in the
  handshake (call `status()` first, every tool returns JSON with `ok`/`fix`).
- **Tool calls may overlap.** In 2.x sync tool functions run on a worker
  thread, so one server process can execute two calls at once (1.x ran them
  one after another on the event loop). Mutable module state is guarded
  (`_LOCK` around instance allocation/close, the OmniParser loader's own
  lock, atomic dict ops elsewhere); an e2e test now runs calls concurrently.
  Driving ONE instance from two concurrent calls is still a bad idea (shared
  cursor) — that is the agent's contract, unchanged.
- Test client code uses 2.x wire names (`is_error`, `server_info`,
  `mime_type`). Nothing changes for agents: the JSON payloads, `isError`
  flag on the wire and `[json, image]` returns are identical.

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
- `basic` OCR converts the frame to grayscale + autocontrast and upscales it
  2× before tesseract (`GLOVEBOX_OCR_SCALE`, 1–4): measured on real UI text,
  the raw frame at 1× found nothing and the preprocessed 2× frame read every
  word at 95%+. A second pass at 1× runs too and the richer result wins
  (tesseract 5.3.4 in CI returned nothing for upscaled frames that 5.5.0
  reads). Coordinates are mapped back to screen pixels.
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
