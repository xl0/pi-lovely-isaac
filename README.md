# pi-lovely-isaac

Coding agents driving a running Isaac Sim: a Kit extension hosts a loopback
WebSocket + JSON-RPC 2.0 server whose only surface is Python `exec` with an
injected `agent` helper library (screenshots, timeline, state, telemetry, media).
Clients: a Node CLI, a pi extension, and an MCP stdio adapter.

Design and rationale: [docs/DESIGN.md](docs/DESIGN.md). The exec contract models
see: [exts/xl0.lovely.isaac/docs/HELPERS.md](exts/xl0.lovely.isaac/docs/HELPERS.md).

## Run Isaac with the server

```bash
tools/launch_isaac.sh            # or add to your own launcher:
#   isaacsim --ext-folder <repo>/exts --enable xl0.lovely.isaac
```

On startup the extension binds `127.0.0.1:<random port>` and writes
`~/.isaac-agent/<port>.lock` (port + per-launch token). All clients discover it
from there. Port and token are always random per launch; there are no settings.

## CLI

```bash
node cli/bin/isaac-cli.mjs status
node cli/bin/isaac-cli.mjs exec 'agent.status()'
node cli/bin/isaac-cli.mjs exec -f scenarios/falling_cube.py
node cli/bin/isaac-cli.mjs exec --timeout 5 'await asyncio.sleep(60)'   # cancels in-sim
node cli/bin/isaac-cli.mjs screenshot out.png --width 640 [--camera /World/Cam]
node cli/bin/isaac-cli.mjs watch          # stream log/timeline/event notifications
node cli/bin/isaac-cli.mjs repl           # interactive; Ctrl-C cancels a running exec
node cli/bin/isaac-cli.mjs docs           # the agent helper docs, from the live server
```

## pi extension

```bash
pi -e <repo>/extensions/lovely-isaac/index.ts
```

Registers `isaac_exec` (helper docs embedded in the tool description after
connect; screenshots return as images into model context) and `isaac_events`
(drains buffered telemetry/log notifications). Footer shows `● Isaac <version>`
plus sim time while playing; `/isaac` command for connect/disconnect/status/docs.

## MCP adapter (Claude Code etc.)

```bash
cd mcp && uv venv && uv pip install -e .
claude mcp add isaac -- <repo>/mcp/.venv/bin/isaac-agent-mcp
```

Tools `isaac_exec` / `isaac_events`, same behavior as the pi extension. The
adapter owns timeout policy (default 120 s, `timeout_s` per call, host aborts
forwarded) and cancels the in-sim exec on expiry.

## Tests

```bash
python3 -m pytest tests/ -v     # needs a live Isaac with the extension enabled
```

`tests/test_server.py` is the protocol gate (auth, exec semantics, cancel,
disconnect-cancel, FIFO, media, notifications). `tests/test_mcp_adapter.py`
gates the MCP adapter end-to-end over stdio.

## Dev setup

LSP/type-checking config is machine-local: `python3 tools/gen_pyrightconfig.py`
writes a gitignored `pyrightconfig.json` with per-directory import paths discovered
from the isaacsim conda env (re-run after env upgrades). `typings/pxr/` is a
committed stub overlay that keeps pxr untyped — NVIDIA's generated stubs mistype
many returns as `None`. Lint/format: `ruff` via `pyproject.toml`.

## Dev loop

Against a running GUI instance: edit files under `exts/`, then
`tools/reload_ext.sh` (the app config has no hot-reload; the script toggles the
extension through NVIDIA's port-8226 exec bridge and purges `sys.modules`).
`tools/kit_exec.py` talks to that bridge directly.
