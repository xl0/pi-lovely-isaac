# pi-lovely-isaac

Coding agents driving a running Isaac Sim: a Kit extension hosts a loopback
WebSocket + JSON-RPC 2.0 server whose only surface is Python `exec` with an
injected `agent` helper library (screenshots, timeline, state, telemetry, media).
Clients: a Node CLI, a pi extension, and an MCP stdio adapter.

Design and rationale: [docs/DESIGN.md](docs/DESIGN.md). The exec contract models
see: [exts/xl0.lovely.isaac/docs/HELPERS.md](exts/xl0.lovely.isaac/docs/HELPERS.md).

## Run Isaac with the server

```bash
tools/launch_isaac6.sh           # Isaac 6; Jupyter + agent server, 60 Hz cap, RTX eco mode
tools/launch_isaac.sh            # Isaac 5.1; or add to your own launcher:
#   isaacsim --ext-folder <repo>/exts --enable xl0.lovely.isaac
```

Run one launcher, not both. Isaac 6 uses `~/miniforge3/envs/isaacsim6`;
extra arguments are forwarded to Isaac. Both launchers run in the foreground.

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

`isaac_exec` accepts exactly one of:

```json
{"code": "agent.status()"}
{"path": "scenarios/falling_cube.py"}
```

Files are read by pi as UTF-8, relative to the session's working directory.
Contents run in the same persistent namespace with top-level await and
last-expression results; no `__main__`, `__file__`, cwd, or import-path changes.
The expanded tool row shows the submitted source snapshot, not later file edits.
Python failures retain stdout/media and are marked as errors. Text over 2000 lines
or 50 KiB is previewed; the full text is saved to a temporary file.

`isaac_events` consumes the oldest entries first. `max` defaults to 100;
unreturned entries remain buffered unless `flush: true` explicitly discards them:

```json
{"max": 20}
{"max": 20, "flush": true}
```

Results report how many entries remain or were flushed. The 500-entry buffer can
still evict oldest entries on overflow, which is reported separately.

## MCP adapter (Claude Code etc.)

```bash
cd mcp && uv venv && uv pip install -e .
claude mcp add isaac -- <repo>/mcp/.venv/bin/isaac-agent-mcp
```

Tools `isaac_exec` / `isaac_events`, same behavior as the pi extension. The
adapter owns timeout policy (default 120 s, `timeout_s` per call, host aborts
forwarded) and cancels the in-sim exec on expiry.
Its `path` input is relative to the adapter's working directory, not the host
agent's session directory. Use absolute paths when those directories differ.

## Observation safety

Camera capture requires a STOPPED Replicator orchestrator and restores its render
settings/resources, including on cancellation. Asset previews use unique temporary
namespaces and clean only their own specs; rendering can temporarily touch local
USD layers. `agent.state()` reads composed USD, not native PhysX state: author
physics fixtures in the active simulation edit target so stronger session-layer
transforms do not mask the simulated pose.

## Tests

```bash
uv venv .venv
uv pip install --python .venv/bin/python --group dev
.venv/bin/python -m pytest tests/ -v  # live gates need Isaac + mcp/.venv
.venv/bin/ruff check .
```

`tests/test_server.py` is the protocol gate (auth, exec semantics, cancel,
disconnect-cancel, FIFO, media, notifications), plus isolated USD/capture-cleanup
regressions. `tests/test_mcp_adapter.py` gates the MCP adapter over stdio.
`bun test tests/pi-extension.test.ts` checks pi file inputs, source snapshots,
output/error rendering and reload lifecycle; it needs Bun and pi's runtime
packages available in `node_modules`.

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
