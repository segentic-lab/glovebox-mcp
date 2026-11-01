# Roadmap

What is planned, in order, and why. Dates are when the item was decided, not promised delivery.
Everything shipped is in [CHANGELOG.md](CHANGELOG.md).

## 0.3.0 — honesty round two + agent ergonomics (2026-11-01, this release)

Done in this release: every confirmed lie or hang fixed (off-screen clicks, option-like key tokens,
missing window manager, `open_file` success on a dead handler, unanchored `pkill`, no subprocess
timeouts, proxy leaking into CDP, raw `No module named 'util'` in the default vision mode), the
vision backends moved into `vision.py` with a GPU preflight and an `auto` mode that reports its
reasoning, and four new tools (`clipboard`, `wait_for_stable`, `window_info`, `app_log`).

## 0.4.x — MCP Python SDK 2.x

The server is pinned to `mcp<2` (0.2.1) because 2.0 renamed `FastMCP` → `MCPServer` and moved
`ToolError`/`Image`. Scope of the migration (small, but it changes runtime semantics):

- `MCPServer("glovebox", version=__version__)` replaces the `_mcp_server.version` hack;
  `mcp.server.mcpserver.{exceptions,utilities.types}` import paths.
- **Sync handlers run on a worker thread in 2.x.** Tool calls may then overlap; `INSTANCES`,
  `_LAST`, `_GEOM` and the OmniParser loader already take `_LOCK`/their own lock — audit every
  mutation before switching.
- Smoke tests read `_mcp_server`; move them to the public `list_tools()` / initialize path.
- Keep Python 3.10 (2.3.0 still supports it) and re-run the full suite under both 1.30 and 2.x
  before dropping the pin.

## 0.4.x — shared vision service (one GPU load for the whole fleet)

Today OmniParser is loaded **per server process**, and every agent runs its own server process
over stdio. One agent driving three instances loads the models once; three agents in `local`
mode load them three times (roughly 1.5–2.5 GB VRAM each, plus a CUDA context apiece). The
0.3.0 preflight makes the third agent fail honestly instead of crashing, but it does not make
it work.

Plan: a small `vision-service` process owns the models and answers parse requests over a local
Unix socket (`$XDG_RUNTIME_DIR/glovebox-vision.sock`); the server processes become thin clients.

- **Protocol**: one request = PNG frame + `box_threshold` + instance id; one reply = the same
  JSON `parse_screen` returns today plus the annotated PNG. Length-prefixed, no HTTP.
- **Lifecycle**: first server that needs `local` starts the service if the socket is dead
  (flock, like `_launch_lock`); the service exits after N idle minutes (configurable), so a
  forgotten service does not pin the GPU. `status()` reports `vision.service: {running, pid,
  clients, loaded, idle_s}`.
- **Fallback**: if the socket cannot be reached the server falls back to in-process loading with
  the existing preflight — the service is an optimization, never a new failure mode.
- **Queueing**: requests are serialized on the GPU (what happens implicitly today); the reply
  carries `queued_ms` so agents can see contention.
- **Tests**: service start/stop/idle-exit and the protocol round-trip run without a GPU by
  mocking the parser; the real-GPU path stays a manual check (CI has no GPU).

Ordering: this ranks **ahead** of the MCP SDK 2.x migration if multi-agent `local` mode is the
main use; otherwise after it. Both are independent of each other.

## 0.4.x — candidates, not yet committed

- `navigate(url)` / `page_text()` over the CDP port that already exists for uploads — exact
  browser navigation and DOM text without keystrokes or OCR. Risk: scope creep toward a browser
  automation server (that is periscope's job); keep it to the two calls agents ask for most.
- A `close_all` / `close_instance(all=True)` once multi-agent fleets are routinely left behind.
- `parse_screen` element *types* for `basic` (buttons vs. text) via simple geometry heuristics.
- Per-instance screen recording (`ffmpeg -f x11grab`) for dogfooding evidence in issues.
- `scroll` by pixel distance instead of wheel notches where the toolkit supports it.

## Housekeeping (needs the maintainer, not code)

- **Glama claim** — the listing at glama.ai/mcp/servers/segentic-lab/glovebox-mcp exists and is
  unclaimed; `glama.json` is in place.
- **License** — MIT here, AGPL-3.0 in the sibling servers (periscope, lens). Decide before
  outside contributions arrive.
- **Siblings on mcp 2.x** — periscope/docslens use the low-level `mcp.server.Server`; 2.x renamed
  `McpError` and switched wire field names to snake_case. They need the same pin-then-migrate.
