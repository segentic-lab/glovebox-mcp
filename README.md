# glovebox-mcp

A **sandboxed computer-use MCP server** — let an AI agent drive a real browser and desktop apps
(mouse, keyboard, screenshots, clipboard, vision grounding), confined to a **nested X11 window** so
its input and vision never touch your real screen or the windows of your other apps.

Like a lab **glovebox**: the agent reaches in and manipulates real applications, sealed off from
your desktop. Bring the sandbox up, log into whatever sites or apps you want to automate *inside
that window*, and the agent operates only there — you can watch it live and close it instantly.

> Speaks the [Model Context Protocol](https://modelcontextprotocol.io), so it works with MCP clients
> like Claude Code. Your host can run Wayland; the sandbox gives the agent a real X server to drive.

![glovebox-mcp — an agent filling a sign-up form inside the sandbox](assets/demo.gif)

<sub>An agent driving a real browser in the sandbox — gliding the cursor, inserting a unicode name (`Nadja Kovačič`), typing, and submitting. All confined to a nested X11 window.</sub>

## Why a nested X11 sandbox?

- Most desktop automation (xdotool, PyAutoGUI) is **X11-only**, but many modern desktops run **Wayland**.
- **Xephyr** provides a real X server *inside a single window* (`DISPLAY :1`). Everything the agent
  does — clicks, typing, screenshots — is **confined to that window**, not your real desktop.
- You stay in control: watch it live, `pkill Xephyr` to close everything.

The isolation is **display-level** (see [Safety](#safety)): the apps themselves run as your user with
your filesystem. For OS-level isolation run glovebox inside a VM or container.

## Requirements

- **Linux** — the sandbox nests a real X server (Xephyr), so it works even on **Wayland** hosts
  (via Xwayland). Not macOS/Windows. Developed on Ubuntu; any modern Linux with the packages below.
- **Python 3.10+** and [`uv`](https://docs.astral.sh/uv/) (used for the virtualenv).
- **System packages** — `xserver-xephyr` (Xephyr), `openbox`, `scrot`, `x11-utils`, `xdotool`,
  `wmctrl`, `xclip` (+ `tesseract-ocr` for `basic`). On Debian/Ubuntu the installer **auto-installs**
  them via `apt` (sudo); on Fedora/Arch it prints the matching `dnf`/`pacman` command. The MCP server
  itself is distro-agnostic — any Linux with these tools works. `openbox` is not optional in
  practice: without a window manager Xephyr ignores synthetic pointer motion, so clicks would land in
  the screen center (`launch_app` warns when it is missing).
- A **browser** in the sandbox (Chromium, Chrome, Brave, Edge, Vivaldi or Opera).
- **NVIDIA GPU (≥6 GB VRAM)** — only for the `local` vision mode. The server checks this itself
  (`status()` → `vision.gpu`) and `auto` falls back to `basic`/`none` when it is not met.

## Install

Pick a vision backend and run its one-liner (clone → install). Each one installs the system packages
(auto via `apt` on Debian/Ubuntu) and the Python deps for that mode, and writes a ready-to-paste
`mcp-config.json` with your paths.

**`none`** — no local models; your agent reads screenshots itself (lightest, instant):

```bash
git clone https://github.com/segentic-lab/glovebox-mcp && cd glovebox-mcp && ./install.sh none
```

**`basic`** — Tesseract OCR grounding (`parse_screen` → text lines + coordinates, CPU-only):

```bash
git clone https://github.com/segentic-lab/glovebox-mcp && cd glovebox-mcp && ./install.sh basic
```

**`local`** — OmniParser on an NVIDIA GPU (`parse_screen` → text **+ icons**, pixel-precise; ~4 GB weights, ≥6 GB VRAM):

```bash
git clone https://github.com/segentic-lab/glovebox-mcp && cd glovebox-mcp && ./install.sh local
```

Your choice is written to `.vision-mode` (override per run with the `GLOVEBOX_VISION` env var).
Without either, the server runs in **`auto`** and picks the best backend that is actually usable.
`python vision.py check` prints the same diagnosis `status()` returns (GPU, weights, tesseract).

### Works with any MCP client / harness

Claude Code, Cursor, Codex, or your own agent — it's a standard MCP server, not tied to any one host.
Two compatibility notes: **`basic`/`local`** return element **coordinates as text**, so they work even
with text-only agents; **`none`** relies on the client passing the tool's **screenshots** to a
multimodal model (fine for Claude Code, Cursor, and other image-capable MCP clients). In any mode,
`clipboard()` gets exact text out of an app without vision at all.

## Quickstart

1. **Start the sandbox** (leave it running):
   ```bash
   ./start-display.sh              # 1440×900 Xephyr window with a browser
   ./start-display.sh 1920x1080    # …or pass a screen size (or set $RES)
   ```
   Log into any sites or apps you want to automate **in that window**.
2. **Register the server** with your MCP client. `install.sh` already wrote **`mcp-config.json`** with
   your real install path — copy its `glovebox` block into your client's MCP config:
   ```jsonc
   { "mcpServers": { "glovebox": {
       "command": "/abs/path/to/glovebox-mcp/.venv/bin/python",   // filled in by install.sh
       "args":    ["/abs/path/to/glovebox-mcp/server.py"],
       "env":     { "DISPLAY": ":1" }
   } } }
   ```
   Restart the client so it loads the server.
3. Ask the agent to screenshot / click / type — it operates **only** on the `:1` window.

> **Driving it with an AI agent?** Paste [`AGENTS.md`](AGENTS.md) into the agent's system prompt — it
> teaches the observe → act → verify loop, grounding, the upload/unicode gotchas, and when to stop.

## Tools (23)

| Tool | What |
|---|---|
| `status()` | Server + sandbox status in one read-only call: version, the vision diagnosis (requested vs. effective backend and *why*, GPU name/VRAM, OmniParser code/weights, tesseract languages), live instances, and which system deps are present. Run it first — and paste it into bug reports. |
| `screenshot(instance, region?, scale?, format?)` | `[json, image]`. `region="x,y,w,h"` zooms into a rectangle, `scale=0.5` shrinks, `format="jpeg"` compresses — cut payload or read small text. |
| `parse_screen(instance, granularity?)` | Vision grounding → JSON of detected elements (id, type, label, interactive, pixel-center; capped at 300 per call, flagged via `"truncated"`) + a numbered Set-of-Mark image. `basic` merges OCR words into **lines** (`"Sign in"` = one target; `granularity="word"` for single words). |
| `click_element(id)` | Click an element from the last `parse_screen` (no coordinate guessing). |
| `click(x,y)` · `double_click` · `move_mouse` · `scroll` · `drag(…, steps?)` | Pointer ops. Coordinates outside the display are **rejected** (xdotool would silently clamp them). `drag` emits intermediate motion events so canvases and sliders register it. |
| `type_text(text)` | **Unicode-safe** typing (ASCII via xdotool; anything with č/š/ž… is inserted via the clipboard, because xdotool's synthetic unicode is silently dropped by some GTK apps). |
| `press_keys("ctrl+a" / "Return" / "ctrl+a Delete")` | Keys, combos, and space-separated **sequences** (xdotool syntax). Invalid names error — nothing is pressed. |
| `clipboard(action="get"\|"set", text?)` | Read the instance's clipboard (select + `ctrl+c`, then read **exact** text — no OCR) or set it (then `ctrl+v`). Per-instance. |
| `wait_for_stable(instance, timeout_ms?, threshold?)` | Wait until the screen **stops changing** (page loaded, app drew) instead of guessing a delay. Reports `stable`/`timed_out` honestly. |
| `window_info(instance)` | Active window title + geometry and the visible windows — confirm navigation or that an app opened, without an image. |
| `upload_file(filepath, selector?, page_url_contains?)` | Attach a local file to a page's `<input type=file>` via the **Chrome DevTools Protocol**. The nested X11 file picker is invisible to automation **and hangs the renderer**, so use this for all uploads — never click an upload button expecting a dialog. Works on Chromium-family browsers started by `launch_app`/`start-display.sh` (per-instance `--remote-debugging-port`, `9222+N`). **Browser file inputs only** — for native apps see `open_file`. |
| `open_file(filepath, app?)` | Open a local file in a **native** app on the instance's display (e.g. `app="gimp"`) or via `xdg-open`. Fails honestly if the handler exits immediately (no app registered for that type). |
| `list_files()` | The instance's staging folder `files/<N>/` + entries with size, mtime and a `partial` flag for in-progress downloads. |
| `app_log(instance, lines?)` | Tail of the launched app's stderr — read it when an app crashed or drew nothing. |
| `launch_app(command, name?, size?)` · `list_instances()` · `close_instance(n)` | Multi-instance control (see below). |
| `wait_ms(ms)` · `get_screen_size()` | Timing / sandbox size + effective vision backend. |

Every control tool takes **`instance=N`** and optional **`observe`** / **`settle_ms`** (see below).
In `local` mode OmniParser is **lazy-loaded** on first `parse_screen` (~6 s once, then ~2 s/parse),
after a GPU preflight (VRAM present and free).

### Structured, honest responses

Every tool returns JSON: `{"ok": true, "action": "click", "instance": 1, "detail": "clicked (10,20) button 1", …}`.
Failures come back with the MCP `isError` flag set and the same JSON shape embedded in the error
text — `{"ok": false, "error": "…", "fix": "…"}` — where **`fix` names the call that unblocks you**
(e.g. a click on a dead instance says to run `list_instances()` / `launch_app()`; an unknown element
id says to re-run `parse_screen()`; a GPU that is too small says which mode to switch to). Silent
no-ops are treated as failures too: an invalid keysym, a zero scroll, an off-screen click, closing an
instance that isn't running, an app that exits on launch, or non-ASCII typing without `xclip` all
error instead of pretending success. Clamped waits are flagged (`clamped` / `settle_clamped`).
Image-returning calls (`screenshot()`, `observe="screenshot"|"parse"`) return `[json, image]`.
No subprocess can hang a call: xdotool/xdpyinfo/xclip run under timeouts and a frozen display is
reported as such.

### Vision backend (selectable, self-diagnosing)

`GLOVEBOX_VISION` env var, or the `.vision-mode` file, or default **`auto`**:

| Mode | `parse_screen` | Needs | When |
|---|---|---|---|
| `auto` | the best **ready** backend below (`local` → `basic` → `none`); `status()` says which and why | — | default |
| `none` | disabled (returns a note) — use `screenshot()` + reason, `clipboard()` for exact text | nothing (mcp, mss, xdotool) | lightest; let the agent's own vision do grounding |
| `basic` | Tesseract OCR → text **lines** + coords (+ confidence) | `tesseract-ocr` + `pytesseract` | no GPU; text-only grounding |
| `local` | OmniParser → text **+ icons** + coords | torch + CUDA + OmniParser weights + NVIDIA GPU ≥ 6 GB | best grounding |

An explicitly requested `basic`/`local` that is not usable **fails `parse_screen` with the fix named**
(OmniParser not installed, no/too-small GPU, GPU out of memory, missing tesseract language data) —
it never silently falls back; only `auto` does, and it says so. Tesseract languages default to
`slv+eng` filtered to what is installed (`GLOVEBOX_TESS_LANG` overrides). Switch anytime with
`./install.sh <mode>` (installs only what that mode needs).

### Multi-instance (a fleet of app windows)

Every control tool takes **`instance=N`** (default 1 = the `start-display.sh` sandbox). Spin up more —
each its own Xephyr display/window on the host desktop:

- `launch_app(command, name?, size?)` → starts the next free `:N` running **any** GUI app
  (`chromium`, `gimp`, `inkscape`, `xterm`, …). Chromium-family browsers auto-get X11 flags, a
  per-instance profile, a remote-debugging port, and downloads routed to `files/<N>/`; other GTK apps
  get D-Bus isolation. Returns the instance id, size and any warnings.
- `list_instances()` · `close_instance(n)` · `app_log(n)`.

Because each display has its **own cursor**, multiple agents can drive different instances **in parallel** —
one window each. Each agent's server process allocates display numbers under a **cross-process lock**,
so simultaneous launches don't collide. The only shared resource is the GPU for `local`-mode
`parse_screen` (it just queues). The host display for new windows is `GLOVEBOX_HOST_DISPLAY`
(default `:0`); `XAUTHORITY` is auto-discovered. Instances **outlive the server process** by default
(another agent or session can pick them up via `list_instances`); set `GLOVEBOX_CLEANUP_ON_EXIT=1` to
close the ones a server launched when it exits.

### One-call action + observe

`click` · `click_element` · `type_text` · `press_keys` · `scroll` · `drag` · `double_click` ·
`upload_file` · `wait_for_stable` take **`observe`** (`none` default · `screenshot` · `parse`) and
most take **`settle_ms`**. With `observe="screenshot"` the action returns its result **and** the
resulting screen in a single call — no separate screenshot round-trip. Use `settle_ms` when you know
the delay, `wait_for_stable(observe="screenshot")` when you don't. Default `none` keeps routine steps
cheap; opt into `screenshot`/`parse` on the steps that change the page (navigations, submits).

### Files & uploads

Each instance gets a staging folder **`files/<N>/`** inside the install dir — a stable place to drop
files for that instance (readable by native apps and, since it's under `$HOME`, by snap Chromium too).
`list_files(instance)` shows the folder and its contents, with sizes and a `partial` flag for
in-progress downloads.

- **Browser** `<input type=file>` → **`upload_file(path, instance)`** (via CDP; `page_url_contains`
  picks the tab when several are open). The nested file picker is invisible to automation and hangs
  snap Chromium, so never click an upload button expecting a dialog.
- **Native apps** (GIMP, Inkscape, editors) → **`open_file(path, instance, app="gimp")`**, or just
  drive the app's own **Open dialog** — unlike the browser's, it's a real visible window you can type a
  path into (`Ctrl+L` in a GTK file chooser).
- **Saving / downloads** → apps run as your user, so they can save anywhere you can write. `launch_app`
  browser instances are pre-configured to **download and "save as" into `files/<N>/`**; point native
  apps' Save dialogs there too, then `list_files(instance)` to see the results.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GLOVEBOX_VISION` | `auto` (or `.vision-mode`) | `auto` · `none` · `basic` · `local` |
| `GLOVEBOX_HOST_DISPLAY` | `:0` | host display where new Xephyr windows appear |
| `GLOVEBOX_CLEANUP_ON_EXIT` | off | `1` → close the instances this server launched when it exits |
| `GLOVEBOX_TESS_LANG` | `slv+eng` ∩ installed | tesseract language string for `basic` |
| `DISPLAY` | `:1` | instance 1's display (set by `mcp-config.json`) |

## Maintenance (`local` mode)

The OmniParser lifecycle lives in **`vision.py`** (the server owns it; `install.sh local` just calls
it): `python vision.py install-omniparser` clones [OmniParser](https://github.com/microsoft/OmniParser),
downloads the v2 weights into `OmniParser/weights/`, and makes PaddleOCR optional (this uses easyocr).
`python vision.py check` reports readiness. **`transformers` is pinned to 4.49.0** — newer releases
break Florence-2's remote config. If you upgrade OmniParser manually, re-run `install-omniparser` to
re-apply the patch.

## Stop

```bash
pkill Xephyr      # closes the sandbox (browser + WM + display)
```

## Safety

- The agent's input and vision are **scoped to the sandbox display** — it does not see or control your real desktop.
- The server process and the apps it launches run **as your user** (shell/file access, like any MCP
  server); only the **GUI control** is sandboxed to the Xephyr window. Files the agent saves, opens or
  uploads are your real files. For OS-level isolation from your files, run it inside a VM or container.
- You can watch everything live and close it instantly with `pkill Xephyr`.
- Automate responsibly — only sites and services you are authorized to use.

## Files

- `server.py` — the MCP server (all 23 tools).
- `vision.py` — vision backends: selection rules, GPU preflight, tesseract grouping, OmniParser lifecycle (`check` / `install-omniparser` CLI).
- `install.sh` — mode-aware installer (`none` / `basic` / `local`).
- `start-display.sh` — launches the Xephyr sandbox (display + window manager + browser).
- `AGENTS.md` — drop-in tool-usage instructions for the AI agent (paste into its system prompt).
- `ROADMAP.md` — what is planned next (MCP SDK 2.x migration, …).
- `mcp-config.json` — a ready-to-paste MCP client config snippet.

## Credits

`local` vision mode uses Microsoft's [OmniParser](https://github.com/microsoft/OmniParser)
(cloned and weights downloaded by `vision.py`, under its own license). Screen capture uses
[mss](https://github.com/BoboTiG/python-mss); input is driven with
[xdotool](https://github.com/jordansissel/xdotool). Not affiliated with Microsoft.

## Contributing

Shipped **as-is** under MIT. Issues and PRs are welcome, but this is maintained by one person —
**no support or response time is guaranteed.** If it's useful to you, a ⭐ helps.

## License

MIT — see [LICENSE](LICENSE).
