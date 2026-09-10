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

Registers `isaac_exec` (helper docs embedded after connect), `isaac_result`
(retrieve existing runs/cancel), and `isaac_events`
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

### Bounded execution waits

`isaac_exec` waits **1000 ms after submission** by default. Fast calls return
their output/images directly; unfinished calls return a run ID. The original
request keeps running—there is no wrapping, resubmission, or second scheduler.

```json
{"code": "await long_experiment()", "waitMs": 0, "label": "experiment"}
```

Then call `isaac_result`:

```json
{"id": "i_6a604999"}
{"id": "i_6a604999", "waitMs": 5000}
{"id": "i_6a604999", "cancel": true, "waitMs": 1000}
```

`waitMs` is bounded to 0–600000 ms. Connection setup and file reading happen before
the wait budget. `notify` on `isaac_exec` defaults to true: detached completion
wakes the originating pi session once with a result reference, not a duplicate
image payload. Inline completions and explicit cancellation do not trigger wakeups.
Set `notify: false` to retrieve quietly.
Exec and `agent.watch` completion notifications use steering delivery: they enter
at the next steering point while busy, rather than waiting for the entire agent
run to finish, and trigger a turn when idle. They do not preempt an in-flight tool.

Aborting the initial exec wait requests cooperative cancellation and returns its
run ID/current state without waiting indefinitely for acknowledgment. Aborting an
`isaac_result` wait does **not** cancel execution. A sent cancellation is not proof
that work stopped; the eventual response is authoritative.

This frees **pi**, not **Kit**. Pending can mean queued or running: synchronous
Python/native calls still block Kit, and other execs cannot overtake its global FIFO.

Run IDs use eight random hex characters, collision-checked within the client
instance, and are lost on reload/restart. Disconnect
marks pending outcomes unknown—never blindly retry code that may have changed the
scene. Completed reads are repeatable. Raw responses, media, and source snapshots
are archived in private temporary files; files are not automatically pruned.
If archiving fails, the response stays in memory and retrieval retries the write.

`isaac_events` consumes the oldest entries first. `max` defaults to 100;
unreturned entries remain buffered unless `flush: true` explicitly discards them:

```json
{"max": 20}
{"max": 20, "flush": true}
```

Results report how many entries remain or were flushed. The 500-entry buffer can
still evict oldest entries on overflow, which is reported separately.

For a native detached Python Task, separate from the client-side run IDs:

```python
t = asyncio.create_task(long_coro())
agent.watch(t, "contact/release", notify=True)
```

This watches the native Task without scheduling or retaining it: keep `t` for
`t.result()` / `t.exception()` / `t.cancel()`. Success, failure (with traceback),
and cancellation produce one event to the registering connection. Without
`notify=True`, completion only enters the event buffer; telemetry never wakes pi.
Wakeups are best-effort and are not replayed after disconnect/reload. Tasks survive
client reconnects while Isaac stays running; reconnect and explicitly watch again
to rearm, or inspect `t` directly.

## MCP adapter (Claude Code etc.)

```bash
cd mcp && uv venv && uv pip install -e .
claude mcp add isaac -- <repo>/mcp/.venv/bin/isaac-agent-mcp
```

Tools `isaac_exec` / `isaac_events`. MCP still waits synchronously; bounded waits
and `isaac_result` are currently pi-only. The adapter owns timeout policy
(default 120 s, `timeout_s` per call, host aborts
forwarded) and cancels the in-sim exec on expiry.
Its `path` input is relative to the adapter's working directory, not the host
agent's session directory. Use absolute paths when those directories differ.
MCP buffers `agent.watch` terminal events but does not wake the host agent.

## Observation safety

Active-viewport screenshots preserve aspect ratio. Two dimensions bound the output
size rather than stretch it; `viewport(width=960)` is sufficient for ordinary sizing.
Offscreen `camera=...` captures retain explicit render-resolution semantics.
Viewport timeout diagnostics observe camera/render/timeline state without attempting
recovery. Shared-view framing and Kit-frame yielding recipes are in
[`HELPERS.md`](exts/xl0.lovely.isaac/docs/HELPERS.md).

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
`tests/test_viewport.py` and `tests/test_notifications.py` are isolated viewport and
connection-scoped task-notification tests; neither connects to a running simulator.
`bun test tests/pi-extension.test.ts` checks pi file inputs, source snapshots,
output/error rendering, bounded waits, cancellation, task wakeups and
reload/SDK-disposal lifecycle; it needs Bun and pi's runtime packages in `node_modules`.
`bun tools/smoke_async_pi.ts` is an opt-in live client gate using short sleeps and
synthetic media. It does not load scenes or change the timeline/viewport.

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
