# Isaac Sim agent protocol — design

Status: implemented in this repo (server + CLI + tests; see CODE.md for state).
Deviations from the original design are marked **[impl]** inline.

Goal: let coding agents (pi, Claude Code, others) drive a running Isaac Sim — execute
code, control the sim, query state, and pull screenshots/data — over a protocol we
control. pi connects through a native pi extension (lovely-ide pattern); every other
agent connects through a thin MCP stdio adapter. All same-host/localhost.

## Prior art (local, read these)

- `/home/xl0/work/work/tm/research/isaacsim/vscode-protocol/README.md` — source-based
  report on NVIDIA's own exec bridge (`isaacsim.code_editor.python_server`, port 8226).
  Steal its top-level-await execution trick; reject its transport (5.1 framing
  executes on arbitrary TCP chunk boundaries, no auth, one request per connection, no
  server push). Its named-context and `fire_and_forget` envelope machinery we
  deliberately do not replicate — see D6. Isaac source
  snapshots referenced there; full checkout at
  `/home/xl0/.cache/checkouts/github.com/isaac-sim/IsaacSim`.
- `/home/xl0/work/projects/pi/pi-lovely-ide` — the discovery/connection pattern to
  fork: lockfile advertisement, token auth at WS upgrade, `hello` handshake,
  JSON-RPC-ish envelopes, Valibot schemas. Canonical doc: `docs/PI_IDE_PROTOCOL.md` in
  that repo. The pi-side scaffolding (`extensions/lovely-ide/connection.ts`, discovery,
  footer) ports almost 1:1.
- pi extension/custom-tool API docs:
  `/home/xl0/work/projects/pi/pi-mono/packages/coding-agent/docs/extensions.md` (and
  `examples/extensions/` beside it).
- Local Isaac Sim 5.1 environment details (conda env, `env.sh` launcher, patched
  Jupyter): "Local Isaac Sim setup" in
  `/home/xl0/work/work/tm/research/CODE.md`.
- `isaacsim.code_editor.jupyter` — embedded Jupyter kernel, already running locally.
  Kept for human notebooks; rejected as agent spine (single blocking exec queue, no
  structured tools, no custom events, flaky launcher).

## Decisions

### D1. Transport: WebSocket + JSON-RPC 2.0, server inside Kit

A Kit extension hosts a `websockets` (pure-Python, asyncio) server on Kit's own asyncio
loop, loopback only. JSON-RPC 2.0 text frames; server pushes notifications.

Rejected:
- REST via `omni.services.core`: no push (polling for long ops), NVIDIA deprioritizing it.
- ZMQ: pyzmq native dep in Isaac's pinned env, Node bindings painful for pi extension,
  no localhost benefit; multipart binary framing solves a problem D4 removes.
- Extending NVIDIA's 8226 server: we don't control it; 5.1's is broken (see report).
- Jupyter protocol: see above.

Dep note: `websockets` is pure Python with no hard pins — low conflict risk with
Isaac's strict metadata (unlike the starlette/uvicorn/anyio stack MCP-over-HTTP would
need).

### D2. MCP lives in an external stdio adapter, not in Kit

~200-line Python process: reads lockfile, dials the WS, exposes the wire surface as MCP
tools. In-Kit MCP rejected: stdio transport impossible (Kit owns the process) so it
would force streamable-HTTP + its dep stack into Isaac's pinned env; MCP has no good
server→push story (we need logs/task-done/telemetry); couples wire format to MCP spec
churn; pi would then need an MCP client instead of a lovely-ide fork. The executor/tool
core stays reusable if in-Kit MCP ever becomes worth it.

### D3. Tool surface: exec only, plus an injected `agent` helper library

The wire protocol carries **no domain-specific methods** — no screenshot method, no
timeline method, no state-query method, no task management. The surface is: `hello`,
`exec`, `ping`, a `cancel` notification, and server-push notifications. Everything
domain-specific is Python executed via `exec`, written against a small curated helper
object (`agent`) injected into the exec namespace.

Rationale:
- Isaac's API is too big to wrap, and every "special" method we sketched turned out to
  be sugar over 1–5 lines of helper-based Python. Test case that settled it: "look at
  an asset before using it" — as wire methods this spawns an endless series
  (`preview_asset`, `preview_variant`, "compare two assets"…); as Python it's one
  helper plus agent-composed variations.
- Fiddly, version-specific mechanics (async viewport capture, deterministic stepping,
  session-layer tricks) still live server-side — but as **helper functions** versioned
  with the Kit extension, not as RPC endpoints. The model composes them instead of
  re-deriving Kit API incantations per call.
- Models use documented Python APIs reliably. The helper docs are injected into the
  model's context by the clients (see "Docs injection" below), so `exec` is not a
  blank-page tool.

### D4. Media/data go over the wire, inline in exec results

The exec result envelope has a `media` array. Helpers attach payloads to it; nothing is
written to disk by the server. The client decides what to do — pi inlines images as
`ImageContent`, the MCP adapter returns MCP image content (non-image mimes: adapter's
choice, e.g. temp file + path in text), a test client saves to disk.

```json
{"status": "ok", "result": null, "stdout": "",
 "media": [{"mimeType": "image/png", "data": "<base64>", "name": "viewport"}]}
```

Verified type shapes (2026-07): pi `AgentToolResult.content: (TextContent |
ImageContent)[]`, `ImageContent = {type: "image", data: <base64>, mimeType}`
(`/home/xl0/work/projects/pi/pi-lovely-ide/node_modules/@earendil-works/pi-ai/dist/types.d.ts:239`);
MCP image content is also base64+mimeType. Base64 end-to-end is zero-conversion for
both clients. pi tool results carry text and images only — no other media types.

Sizing: 1080p PNG ≈ 2–4 MB → ≈ 3–5 MB base64; trivial on localhost WS. Cap server
message size (default 64 MB, reject over). Capture helpers take width/height to
downscale at the source. Binary WS frames (blob-id in JSON result, bytes in a binary
frame) are a v2 option if base64 overhead ever matters — do not build in v1.

Escape hatch: `exec` is arbitrary Python; an agent that wants a 10 GB USD export or a
video writes it to disk itself. That's the agent's choice, outside the protocol.

### D5. Discovery/auth: neutral lockfile + mandatory token, loopback only

`~/.isaac-agent/<port>.lock` (neutral dir — pi extension and MCP adapter both scan it):

```json
{
  "protocol": "isaac-agent",
  "version": 1,
  "port": 8765,
  "token": "<random per launch>",
  "pid": 12345,
  "isaac_version": "5.1.0",
  "started_at": "2026-07-21T12:00:00Z"
}
```

Written on extension startup, removed on shutdown; stale locks with dead PID deleted
opportunistically (lovely-ide behavior). Bind 127.0.0.1 always. Token required — the
8226 security findings say auth-off-by-default is not acceptable even locally. Token
sent as `X-Isaac-Agent-Authorization` header at WS upgrade; reject before handshake.
**[impl]** Header-only (a `?token=` URL variant existed briefly and was removed): the
browser-API global WebSocket cannot set headers, so the JS clients import undici's
WebSocket, which can. pi ships undici anyway; the CLI takes it as its one dependency.

Bind/staleness rules (learned from the OmniHub incident, 2026-07-21):

- Server: bind/listen first, write the lockfile only after success — advertise only
  reality. On bind failure, one clear carb error and the extension disables itself;
  never a retry loop. Random port default makes collisions unlikely; explicit-port
  configs get the loud failure.
- Clients: a connection failure means the lock is stale even when the PID is alive —
  live Kit ≠ live server (hot-reload can kill just the extension). Reconnect with
  capped exponential backoff, not a retry storm.
- Anti-pattern this design avoids: OmniHub derives its discovery-file name from an
  install-path hash and binds a fixed shared port — moving the install orphans the
  running instance and its successor panics on `AddrInUse`. Here everything needed to
  connect (port, token) lives explicitly in the lockfile; nothing is derived.

### D6. Execution model (the part that matters most)

All USD/physics/timeline ops must run on Kit's main loop. The WS server already lives
on that loop; exec bodies run there, awaiting
`omni.kit.app.get_app().next_update_async()` when frames must pass.

- **One persistent global namespace**: all `exec` requests share a single namespace
  dict that lives for the Kit process — state survives client reconnects by
  construction (the background-task pattern depends on this). NVIDIA's named contexts
  are
  deliberately dropped: single-agent workflow, and an optional `context` field can be
  reintroduced backward-compatibly if isolation is ever needed.
- **Serialization**: `exec` requests run one at a time (global FIFO across
  connections); they interleave with detached tasks at await points.
- **Top-level await** supported (compile with `PyCF_ALLOW_TOP_LEVEL_AWAIT`, same as
  NVIDIA's executor — see snapshot referenced in the vscode-protocol report).
- **Synchronous exec only — no protocol-level background tasks.** Every `exec` request
  stays pending until the code finishes; an 8 s scripted flight is just a request that
  takes 8 s. Rationale:
  - Coding-agent loops are turn-synchronous: the model calls a tool and waits.
    Protocol background tasks would force cross-turn polling ("is it done yet") —
    strictly worse than one blocking call for the primary consumer.
  - Isaac's native long-running pattern needs no tasks: the sim itself runs on Kit's
    loop — `agent.play()` returns instantly, continuous behavior is a physics callback
    (registration is instant). "Start the flight, then poke around" = register
    callback + play, then further execs against the live sim.
  - Genuinely long jobs (hour-scale Replicator dataset generation) are the namespace's
    job, not the protocol's: `t = asyncio.ensure_future(long_coro())` in an exec, Task
    bound in the namespace (bind it — asyncio holds only weak refs, an unbound task
    can be GC'd mid-flight); later execs check `t.done()`/`t.cancel()`, the coroutine
    emits progress via `agent.emit`. Results live in the namespace; media attaches to
    whichever exec fetches them. `agent.watch(t, label, notify=True)` optionally
    attaches a done callback for a terminal-event wakeup; it is not a scheduler.
- **Cancellation, not server-side timeouts**: the server runs an exec until it
  finishes or is canceled; there is no server timer. Timeout is client policy — MCP
  hosts and pi already have tool-timeout machinery, and only the client knows whether
  a given exec should take 2 s or 5 min. Timeout expiry and a user's Esc are the same
  wire message: `cancel {id}`. The executor genuinely cancels the driven task at its
  next await point — unlike NVIDIA's watchdog, which only suppresses the late reply
  (see the vscode-protocol report) — and the exec resolves with a
  `CancelledError`-shaped error result.
- **Disconnect implies cancel**: when a connection closes, its pending execs are
  canceled. This is the dead-client backstop (a crashed adapter can't send `cancel`;
  without this rule a runaway exec would hold the global FIFO forever) and it is
  coherent on its own: a foreground exec's result is undeliverable once its
  connection is gone, so foreground work is request-scoped by design — work meant to
  outlive the connection is exactly what `ensure_future` is for. Side effect:
  restarting a wedged client frees the FIFO.
- **Honest limits** (document in user-facing docs too): synchronous code blocks Kit —
  no preemption, same as NVIDIA's server; `cancel` and `Task.cancel()` take effect
  only at await points; nothing kills runaway sync code.

## Wire protocol v1

JSON-RPC 2.0 over WS text frames. Client→server: requests. Server→client: responses +
notifications. Protocol name `isaac-agent`, version `1` (in lockfile and `hello`).
Wire field names camelCase.

### Handshake

`hello` (first request, required):
params `{protocolVersion, client: {name, version, pid?}, subscriptions: ["log",
"event", "timeline"]}` →
result `{protocolVersion, server: {isaacVersion, kitVersion, extensionVersion},
stage: {path}, helperDocs: "<markdown>"}`.
`helperDocs` is the injectable documentation for the `agent` helper library (see
below). Non-hello requests before hello → error. Unknown method → `-32601`. Unknown
notifications ignored.

### Methods

- `exec` `{code}`
  → `{status: "ok", result?: <repr/JSON>, stdout, stderr?, media?: [...]}`
  | `{status: "error", stdout, stderr?, ename, evalue, traceback: [...], media?: [...]}`.
  Stays pending until the code finishes or the request is canceled (D6). (NVIDIA's
  `context`/`args`/`timeout` envelope fields are dropped: one global namespace,
  code-writing agents embed values in code, timeout is client policy.)
  **[impl]** `result` uses notebook semantics: the value of the last *top-level
  expression* (AST-split, eval-compiled), null when the code ends in a statement.
  Strictly more useful to models than NVIDIA's eval-first/exec-fallback — multi-line
  code still returns its final expression. Top-level `await` works in both body and
  trailing expression. Non-JSON values are `repr()`'d.
- `ping` → `{}`.

That is the entire method surface.

### Notifications (client→server)

- `cancel {id}` — cancel the pending exec with that JSON-RPC id (same connection).
  Effective at the exec's next await point; the exec resolves with a
  `CancelledError`-shaped error result. Sent by clients on their timeout expiry or
  user abort (Esc). Closing the connection cancels all of its pending execs (D6).

### Notifications (server→client, per `hello` subscriptions)

- `log {severity, source, message, t}` — Carbonite log subscription, severity-filtered
  and rate-limited server-side (flood control is mandatory, Isaac is chatty).
- `timeline.changed {playing, simTime}` — for client UX (pi footer).
- `event {name, payload, t}` — from `agent.emit()`.
  `agent.watch` uses the same event subscription with `name: "task.done"` and
  an additional top-level `notify` boolean. Payload is `{label, status}` where
  status is `ok`, `error`, or `cancelled`; errors also include `ename`, `evalue`,
  and `traceback`. Results remain on the native Task, not in the event.

Multiple concurrent clients allowed (pi + Claude Code + observer). Notifications
broadcast to subscribers except watch completions, which target only the registering
connection. Exec runs are globally serialized and all clients share the
one namespace (an observer client inspecting an agent's live state is a feature).
The exec connection is captured in a ContextVar; later execs cannot change a watch's
destination. Disconnected owners lose delivery (no replay or session rerouting).
One registration per Task per connection is enforced with weak references. A
reconnected client may explicitly rewatch a retained Task, including a completed one.
Pi's current socket/generation gate converts opted-in terminal events to a custom
follow-up message with `triggerTurn: true`; ordinary telemetry remains buffered.
MCP only buffers terminal events.

## The `agent` helper library

Injected into the exec namespace. Design rules: small curated surface; helpers return
plain Python data (ndarrays, dicts); **capture and ship are separate acts** — grab
data, process in-sim, attach only what's worth sending; sync where possible, async only
where frames must pass; every helper has a docstring (so `help(agent)` works
interactively) and an entry in `helperDocs`.

v1 surface:

- `await agent.viewport(width=None, height=None, camera=None) -> ndarray` — RGBA frame.
  Waits for a rendered frame correctly (async capture-to-buffer mechanics encapsulated
  here — version-specific, do not make models write this). With `camera`, renders via a
  hidden temporary viewport bound to that camera, so the user's viewport/camera is
  never touched; this also covers sensor-camera capture with no extra API.
  **[impl]** camera path = replicator render product + rgb annotator; the annotator
  only fills after `rep.orchestrator.step_async(delta_time=0.0, pause_timeline=False)`
  (waiting frames via `next_update_async` is not sufficient). Active-viewport path =
  `capture_viewport_to_buffer` + PyCapsule pointer copy; width/height resize via PIL
  while preserving aspect ratio (two dimensions define a bounding box). Offscreen
  dimensions remain exact render resolution. Timeout diagnostics read viewport,
  Replicator, rendering settings and timeline state without automatic recovery.
- `agent.image(x, name=None)` — accepts ndarray / PIL image / matplotlib figure / PNG
  bytes; encodes to PNG, appends to the current request's `media`.
- `agent.attach(data: bytes, mime: str, name=None)` — raw attach for anything else
  (depth as npz, point clouds, JSON dumps). Both attach helpers write to the pending
  request's media sink; the sink closes when the request completes, so a leaked
  background coroutine calling them raises instead of writing into a later result.
- `agent.emit(name, payload)` — `event` notification to subscribed clients. The
  telemetry/lesson-gate channel: physics callbacks can emit pose at N Hz, gates emit
  pass/fail.
- `agent.watch(task, label, notify=False)` — terminal event for a native Task,
  returned unchanged. Does not retain it strongly or schedule work. Tracebacks
  remain on the Task and are included in failure events; notification delivery is
  connection-scoped as above.
- `agent.logs(n=50, min_severity="warning") -> list[dict]` — recent Carbonite log
  lines from a server-side ring buffer. Pull complement to the push `log`
  subscription, which only helps if the client subscribed before the interesting
  event; "show me the first real error since startup" is a one-call diagnostic.
- `agent.play()` / `agent.pause()` / `agent.stop()`; `await agent.step(n)` — advance
  exactly n update steps then pause (deterministic gates). **[impl]** each op calls
  `timeline.commit()`: Kit frame-queues timeline commands, so without commit a
  stop→play sequence inside one exec collapses into "stopped" and `is_playing()`
  reads stale state within the same exec.
- `agent.state(paths) -> dict` — per prim `{pose: {pos, quat_wxyz}, lin_vel?,
  ang_vel?}` (velocities when rigid body). **[impl]** Composed USD, not native PhysX:
  stronger session-layer transforms can mask simulation writes to the root layer.
  Author physics fixtures in the simulation edit target.
  `agent.status() -> dict` — fps, timeline clock (`simTime`), playing, stage path.
  Manual physics advancement is not necessarily reflected in that clock.
- `await agent.preview_asset(url, image=True) -> dict` — inspect an asset before use,
  tiered: (1) existing Omniverse thumbnail (`.thumbs/256x256/` beside the asset) via
  `omni.client`; (2) metadata from an independent `Usd.Stage.Open(url)` — default prim,
  prim tree summary, bounds via `UsdGeomBBoxCache`, variants, physics APIs present —
  zero effect on the open stage; (3) if `image` and no thumbnail: reference into the
  **session layer** under a unique `/AgentPreview_<uuid>` path, render
  through a preview camera framed from bounds, capture, tear down. **[impl]**
  Replicator can temporarily author root-layer overrides; cleanup removes only the
  reserved namespace from local layers, preserving preexisting content.
  Refuses tier 3 while the timeline is playing (rigid bodies would drop/collide).
  Attaches the image via `agent.image`, returns the metadata dict.

Anything beyond this is agent-composed Python on top of Isaac/USD APIs — resist growing
the helper surface until a pattern recurs.

## Docs injection

`helperDocs` (markdown, from `hello`) is the single source of truth for how to drive
the sim, versioned with the Kit extension — clients never hardcode helper docs:

- MCP adapter: embeds `helperDocs` in the `isaac_exec` tool description.
- pi extension: embeds it in its registered tool description (or system-prompt
  addition, whichever fits pi better at implementation time).

**[impl]** Client-specific descriptions also document exactly-one `code`/`path`
inputs. Pi reads UTF-8 files relative to session cwd; MCP uses adapter cwd. Contents
are sent as ordinary `exec {code}` with no server-side file access or script
environment changes. Pi stores the submitted source in result details for expanded
display. Both clients preserve oversized text in local files; Python errors retain
partial stdout/media and are marked as failed tool results.
Event reads consume oldest-first up to `max` (default 100), keeping the remainder
unless `flush: true` explicitly discards it. Both clients report remaining/flushed
counts and separately report capacity eviction from their 500-entry buffers.

Content: the helper surface above with signatures and one-line semantics, the
persistent-namespace model, the honest limits (blocking, cancellation), and 2–3 short
recipes (screenshot;
fly-and-measure via callback + play; the long-job pattern — `asyncio.ensure_future` +
Task bound in the namespace + `emit` progress, for anything beyond ~a minute, which
also dodges MCP-host tool timeouts), shared-view framing and Kit-frame yielding.
Keep contracts and recipes concise; workflow policy belongs in the separate usage
skill, not additional tool prompt guidelines.

## Components to build

All new code lives in this repo. **[impl]** In addition to the three planned
components there is `cli/` — `@xl0/isaac-cli`, a Node CLI with undici as its one dependency
(exec with client-side timeout→cancel, repl with Ctrl-C cancel, status, screenshot,
logs, watch, ping, docs). It is both the human/scripting client and the reference
implementation of discovery/auth/cancel client behavior.

### 1. Kit extension (the server)

Extension `xl0.lovely.isaac` (protocol stays neutral `isaac-agent`; lovely branding
lives on the implementations): `extension.toml`, `server.py` (WS + JSON-RPC + lockfile),
`executor.py` (namespace/top-level-await/cancel — crib from NVIDIA's `executor.py`,
snapshot path in the vscode-protocol report), `helpers.py` (the `agent` object),
`docs.py` or `HELPERS.md` (source of `helperDocs`). **[impl]** No Carbonite settings:
port and token are always random per launch (the lockfile is the only distribution
channel — a fixed port would recreate the OmniHub failure mode) and log-push policy is
hardcoded (warning+, 30/s; extend `hello` subscriptions per-connection if ever needed). Load
via `--ext-folder <impl-dir>/exts --enable xl0.lovely.isaac`; add to the launcher in
`/home/xl0/work/work/tm/research/isaacsim/env.sh`. **[impl]** That launcher now enables
the extension. This app does not hot-reload files; use `tools/reload_ext.sh`.

Verify with a small pytest WS client before any agent integration: hello returns
helperDocs, exec round-trip, namespace persistence across a reconnect, an
`asyncio.ensure_future` task checked/canceled from a later exec, `cancel` and a
dropped connection each abort a pending exec at an await point,
`agent.image(await agent.viewport())` yields a decodable PNG in `media`, bad token
rejected. Run the gate once unsandboxed and once via
`run-claude-sandboxed.sh -- <cmd>` (the sandbox scripts bind `~/.isaac-agent`); the
two environments must behave identically.

### 2. MCP stdio adapter

Small Python package (uses `mcp` SDK), runs outside Isaac's env. Discovers lockfile,
dials WS. Tools: `isaac_exec` (description = intro + `helperDocs`; returns text +
images from `media`) and `isaac_events` (drain buffered notifications — MCP clients
handle server notifications poorly, so buffer and surface them here and appended to the
next tool result). The adapter owns timeout policy: it enforces its own configurable
limit and forwards host-side aborts, sending `cancel {id}` on either. Register in
Claude Code with `claude mcp add`.

### 3. pi extension

Fork lovely-ide scaffolding from `/home/xl0/work/projects/pi/pi-lovely-ide`
(discovery, `connection.ts`, footer, Valibot schemas — swap protocol constants/dir).
Registers `isaac_exec` (+ events tool) via `ctx.registerTool`; the execute callback's
`AbortSignal` (user Esc) maps directly to `cancel {id}`; result `content`
mixes `TextContent` (stdout/result/traceback) and `ImageContent` (from `media`) so
screenshots land directly in model context. Tool description carries `helperDocs` from
hello. Footer `● Isaac` + playing/sim-time from `timeline.changed`; `/isaac` selector
for endpoint/settings. pi extension API reference:
`/home/xl0/work/projects/pi/pi-mono/packages/coding-agent/docs/extensions.md`.

## Build order

1. Kit extension core: executor + cancel + WS/JSON-RPC + lockfile + helpers
   (`viewport`/`image`/`attach`/`emit`/`logs`/timeline/`state`/`status`) + pytest
   client.
2. MCP adapter → Claude Code drives Isaac; dogfood immediately (use it to build 3+).
3. pi extension.
4. `agent.preview_asset` (tier 1+2 first, tier 3 after hidden-viewport capture is
   solid), telemetry rate tuning.
5. Later, by demonstrated need: binary frames, in-Kit MCP-over-HTTP.

## Isaac 6.0.1 compatibility **[impl]**

Verified against a fresh pip env (`isaacsim[all,extscache]==6.0.1.0`, Python 3.12,
conda env `isaacsim6`): full gate passes unmodified. Notes:
- NVIDIA's Python-3.12 "Cannot enter into task" concern (their `_drive_coroutine`
  workaround) did not materialize with Task-based execs on Kit 108.
- Replicator on 6 authors preview overs into the **root layer** during
  render-product capture; teardown removes only the unique reserved namespace from
  every local layer, not just the session layer.
- `rep.orchestrator.step_async` canceled mid-step leaves the orchestrator in
  `STEPPED` and timeline auto-update off. Successful captures also disable eco mode.
  The helper now awaits stop/restoration on every exit, including repeated cancel.
  Async rendering restores five updates later, so cleanup waits through that delay.
  An already-active user orchestrator is refused, not stopped or adopted.
- 6 does not auto-enable the 8226 vscode bridge; the dev-reload script falls back
  to toggling the extension through our own server via a detached task.
- One-off launch flake seen: fatal `TSC ran backwards` at startup (machine under
  load); retry succeeded.

## Open questions

- Incremental stdout for long-running execs (an `exec.output` notification stream) vs
  stdout-at-completion. v1: at completion; revisit when a long exec actually hurts.
- Video capture: pi tool results can't carry video (text/images only), so video is
  exec-writes-a-file territory. Out of v1.
- `helperDocs` token budget: one blob in the tool description vs a short summary +
  `agent.docs()` for full text on demand. Start with one blob; measure.
- Reuse `b*` lesson fixtures as protocol smoke tests (e.g. b0_1 flight driven entirely
  over the protocol) — good end-to-end gate once the pi/MCP clients exist.
