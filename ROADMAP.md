# Roadmap

What is planned, in order, and why. Dates are when the item was decided, not promised delivery.
Everything shipped is in [CHANGELOG.md](CHANGELOG.md).

## 0.3.0 — honesty round two + agent ergonomics (2026-11-01, this release)

Done in this release: every confirmed lie or hang fixed (off-screen clicks, option-like key tokens,
missing window manager, `open_file` success on a dead handler, unanchored `pkill`, no subprocess
timeouts, proxy leaking into CDP, raw `No module named 'util'` in the default vision mode), the
vision backends moved into `vision.py` with a GPU preflight and an `auto` mode that reports its
reasoning, and four new tools (`clipboard`, `wait_for_stable`, `window_info`, `app_log`).

## 0.4.0 — MCP Python SDK 2.x (done 2026-11-01, unreleased)

Migrated: `MCPServer("glovebox", version=…)`, 2.x import paths, snake_case wire names in the
test client, dependency floor `mcp[cli]>=2.0,<3`. Verified: the full suite passes under 2.3.0,
including the error paths (`ToolError` → `isError`) and `[json, image]` returns. The one
semantic change — sync tools run on a worker thread, so calls can overlap — is covered by a
concurrency e2e and the existing locks. Python 3.10 stays supported.

## 0.4.0 — shared vision service (done 2026-11-01, unreleased)

Implemented as planned below (`vision_service.py`, 10 GPU-less tests via a fake parser). Still a
manual check: the real-GPU path on a multi-agent run — `status().vision.omniparser.service.served`
should climb while `vram_free_mb` stays flat as agents are added.

Original plan — before 0.4.0 OmniParser was loaded **per server process**, and every agent runs its own server process
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

Ordering: landed right after the MCP SDK 2.x migration, both in 0.4.0.

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
