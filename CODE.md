# Codebase state

Implementation of `docs/DESIGN.md`: isaac-agent protocol — coding agents drive a running
Isaac Sim over WebSocket + JSON-RPC 2.0. Protocol decisions and rationale live in DESIGN.md;
this file covers what exists and non-obvious implementation details.

## Layout

- `exts/xl0.lovely.isaac/` — Kit extension (the server). Load: `--ext-folder <repo>/exts --enable xl0.lovely.isaac`.
  - `xl0/lovely/isaac/executor.py` — persistent-namespace executor. Jupyter semantics:
    AST-splits a trailing expression, compiles body `exec` + tail `eval`, both with
    `PyCF_ALLOW_TOP_LEVEL_AWAIT`; coroutines awaited by the caller (cancellable).
  - `xl0/lovely/isaac/server.py` — WS server (websockets 12, legacy API) on Kit's asyncio loop,
    JSON-RPC layer, lockfile, carb-log ring + push, timeline notifications, media base64 packing.
  - `xl0/lovely/isaac/helpers.py` — the `agent` object + MediaSink + PNG encoding.
  - `docs/HELPERS.md` — helperDocs served in `hello`; the user-facing exec contract.
- `cli/` — `@xl0/isaac-cli`, Node >= 22 ESM (`cli/bin/isaac-cli.mjs`), single dep: undici.
  Commands: exec (-f/stdin, --timeout sends cancel, --media-dir, --json), repl (block mode on
  trailing `:`, Ctrl-C cancels), status, screenshot, logs, watch, ping, docs.
- `tests/test_server.py` — 33 live protocol tests + 21 isolated capture/USD cases.
  `tests/test_viewport.py` — 13 isolated sizing/timeout diagnostics cases with real Pillow.
  `tests/test_notifications.py` — 8 isolated watch/exec-routing cases.
  `tests/test_mcp_adapter.py` — 9 MCP stdio tests; needs `mcp/.venv`.
  `tests/pi-extension.test.ts` — 19 Bun tests for file inputs, source snapshots,
  output/errors, bounded waits, cancellation, archival, wakeups, and lifecycle races.
  Pi runtime packages must be resolvable.
  Python dev tooling lives in repo-local `.venv`, installed from the `dev`
  dependency group. Its websockets<14 is separate from the MCP environment.
- `scenarios/` — exec-payload scripts used as live tests/examples (falling_cube, telemetry_bounce).
- `tools/` — dev loop: `kit_exec.py` (talk to NVIDIA 8226 bridge; 5.1 quirk: no half-close,
  small payloads only, use -F for indirect file exec), `launch_isaac.sh`, `reload_ext.sh`
  (toggle ext + purge sys.modules; app config has no hot-reload), `ws_smoke.py`,
  `smoke_async_pi.ts` (opt-in real-WebSocket pi client gate with a host stub that
  records wakeups; short sleeps/synthetic media, no scene/timeline changes),
  `gen_pyrightconfig.py` (emits gitignored machine-local pyrightconfig.json; per-directory
  executionEnvironments because tests/mcp/exts run under different interpreters).
- `typings/pxr/` — committed Any-stub overlay: NVIDIA's generated pxr stubs mistype many
  returns as None (boost stubgen artifacts); untyped beats wrongly-typed. `carb.logging`
  has no on-disk module (registered at native init) — inline ignore in server.py.

## Server implementation details (non-obvious)

- Exec flow: reader spawns outer task per request; outer creates inner task
  (`_run_exec`) registered in `conn.pending[id]`; `cancel {id}` / disconnect cancel the
  inner; outer catches CancelledError -> `CancelledError`-shaped error result. Global
  FIFO = `asyncio.Lock` acquired inside inner (cancel while queued works).
- stdout/stderr: permanent `_StreamRouter` proxies installed on sys.stdout/stderr while
  server runs; route via task-scoped ContextVar to per-request StringIO, else original
  stream. Buffers closed after result build -> late writes from leaked user tasks fall
  through to the console. Same pattern for media (`MediaSink` closes per request; late
  `agent.attach` raises).
- Timeline ops are frame-queued by Kit — `tl.commit()` after play/pause/stop/step is
  mandatory, else e.g. stop();play() in one exec collapses and callbacks silently don't run.
  `is_playing()`/`get_current_time()` read in the same exec as play() are stale-until-commit.
- Viewport capture: `capture_viewport_to_buffer` callback hands a PyCapsule;
  `PyCapsule_GetPointer` + ctypes copy. Camera capture: replicator render product + rgb
  annotator; data arrives only after `rep.orchestrator.step_async(delta_time=0.0,
  pause_timeline=False)` (plain `next_update_async` loops never fill the annotator).
  Active screenshot dimensions preserve aspect ratio, with two dimensions defining
  a bounding box; offscreen dimensions remain explicit render resolution. Active
  capture timeout includes camera, viewport update state, Replicator status, render
  settings, timeline playing state and FPS. Diagnostics never import/enable
  Replicator or attempt automatic recovery.
- Camera capture refuses non-STOPPED orchestrators, disables capture-on-play while
  attaching, then destroys owned resources and awaits stop_async. Replicator restores
  async rendering five updates later; cleanup waits six and restores original
  timeline auto-update/play-every-frame and capture-on-play. Repeated cancellation
  cannot abandon cleanup. Preview namespaces are UUID-based and collision-checked
  across local layers; teardown removes only the reserved namespace.
  Stop and deferred-render waits share a 10 s cleanup deadline. A local snapshot of
  Replicator's SETTINGS_TO_SAVE restores render settings even if native stop times
  out; synchronous restoration and ownership release still run. Timeout reports
  that Replicator may need manual recovery, rather than wedging the exec FIFO.
- `state()` deliberately reads composed USD, not native PhysX. Live verification:
  a stronger session-authored transform hides a root-written simulated pose while
  velocity still updates. Physics fixtures must use the simulation edit target.
- Auth: token from lockfile via `X-Isaac-Agent-Authorization` header only, rejected
  pre-handshake in process_request. JS clients use undici's WebSocket (the browser-API
  global cannot set headers).
- `agent.watch(task, label, notify=False)` delegates to a server-side native Task
  done callback, not a scheduler. The exec connection is captured via ContextVar.
  Completion is an `event` named `task.done`, targeted only to that connection;
  `notify=True` marks it for pi wakeup. Failures reuse the exec error envelope and
  leave the native Task's exception/traceback inspectable. WeakSets enforce one
  watch per Task per connection without retaining tasks. No offline replay; a new
  connection can explicitly rewatch the retained Task.
- carb log listener: `carb.logging.acquire_logging().add_logger(cb)`, cb(source, level,
  file, line, msg), levels -2..2 = verbose..fatal; may fire off-thread -> ring deque append
  direct, push via call_soon_threadsafe + 1 s token-bucket (`dropped` count attached).
- NVIDIA 6.0.1 python_server drives coroutines manually (`_drive_coroutine`) to dodge a
  Python 3.12 "Cannot enter into task" re-entrancy issue. We use plain Tasks (needed for
  cancel); 5.1 is Python 3.11 — revisit if latest-Isaac testing hits that error.

## Clients

- `extensions/lovely-isaac/index.ts` — pi extension (single file; undici WebSocket
  from pi's own runtime, header auth). Tools isaac_exec/isaac_result/isaac_events; re-registered with
  live helperDocs after hello (registerTool replaces by name); AbortSignal → cancel;
  footer `● Isaac <ver> ▶ t`; `/isaac` command; reconnect with capped backoff.
  Exactly one of `code`/`path`; paths are read client-side relative to session cwd.
  File contents execute unchanged and are snapshotted in result details for expanded
  syntax-highlighted display. No cwd, __file__, __main__ or sys.path changes.
  Headers show `code=` / `path=` / `max=`. Text previews cap at 2000 lines/50 KiB
  with full-output files; image display remains native. A tool_result hook marks
  Python errors without discarding partial stdout/media.
  Exec waits default to 1000 ms after submission (`waitMs=0` detaches immediately).
  A session-local `i_<8 hex>` run ID (collision-checked) tracks the existing RPC;
  `isaac_result` waits/reads or
  sends cancellation only on the original socket. Explicit cancellation suppresses
  wakeup and never fabricates an acknowledgment. Initial-wait abort returns the ID
  after requesting cancellation; result-wait abort only stops waiting.
  Detached completion wakes the owning live pi instance unless `notify=false`.
  Original responses/media and source snapshots are archived in private temporary
  JSON files, releasing large image strings from RAM. Failed writes retain the
  response for a retry on retrieval. Registry IDs do not survive client reload;
  transport failure is an unknown outcome, never automatic resubmission.
  There are no server changes, new native tasks, or changes to the exec FIFO.
  Both clients consume events oldest-first, up to `max` (default 100). The remainder
  stays buffered unless `flush: true`; overflow eviction at 500 entries is reported
  separately. Pi details include returned count, remaining count, and flushed count.
  Shutdown fences socket callbacks and in-flight handshakes by generation before
  clearing the captured context; late replies cannot register tools after reload.
  SDK `AgentSession.dispose()` can skip `session_shutdown`, so async paths also
  probe context liveness. The known stale-context error triggers connection teardown
  rather than escaping a WebSocket callback and crashing pi. No SDK-host coupling.
  Exec completion and opted-in watch events use `sendMessage` with steering delivery
  and triggerTurn: notify during active runs at the next steering point or wake idle pi;
  oversized tracebacks use the same full-output files. Events remain buffered too.
  Workflow policy belongs in the separate usage skill, not extra prompt guidelines.
  Run: `pi -e <repo>/extensions/lovely-isaac/index.ts`. Tested with
  openai-codex/gpt-5.6-sol (autonomous restitution experiment passed).
- `mcp/` — python package `isaac-agent-mcp` (mcp≥1.28 low-level Server + websockets≥14
  `additional_headers` API). Registered project-scope in `.mcp.json`. Adapter owns
  timeout (default 120 s, `timeout_s` per call; host abort → cancel + shielded wait).
  Code/path inputs mirror pi, but relative paths use adapter cwd. Python errors set
  MCP isError; large text is preserved in temp files. Non-image media → temp files
  under /tmp/isaac-agent-media. Watch completions are buffered, without host wakeups.
  MCP still waits synchronously; bounded-wait/result-tool parity is a follow-up.

## Review hardening (2026-07-21 workflow review, 22 confirmed findings fixed)

Non-obvious ones: cancel arriving in the same websockets read burst as its exec is
pre-registered synchronously (sentinel in conn.pending; reader never yields between
buffered frames); _handle_exec has a BaseException catch-all so every exec gets a
response (RecursionError in json.dumps, broken __repr__/__str__); _serialize uses
allow_nan=False (NaN/Inf would emit invalid JSON that Node clients drop silently);
non-dict params / unhashable or null ids answered with errors instead of killing the
reader; camera capture uses force_new render products, refuses user orchestrator
jobs, and restores settings even on cancel; step() pauses
in finally; CLI rejects rpc calls on closed sockets, times out connect/hello, exits
watch on server death, double-SIGINT force-quits.

## Environment

- Isaac Sim 5.1.0 GUI, conda env `~/miniforge3/envs/isaacsim` (Python 3.11, websockets 12).
  Launched via `tools/launch_isaac.sh` (foreground; includes Jupyter).
  The external research `isaacsim/env.sh` also enables our server, plus a 60 Hz
  main-loop cap and RTX eco mode.
- Isaac 6.0.1 in conda env `isaacsim6` (Python 3.12, pip `isaacsim[all,extscache]`),
  launch: `tools/launch_isaac6.sh` — wraps the shared launcher, selects
  `~/miniforge3/envs/isaacsim6`, adds the 60 Hz cap and RTX eco mode, and forwards
  extra arguments. Full gate previously passed there; version notes in DESIGN.md ("Isaac 6.0.1
  compatibility"). No 8226 bridge → reload_ext.sh falls back to a detached-task toggle
  through our own server.
- Last full gate: 60 Python tests pass on Isaac 6.0.1 / Kit 110.1.2 (9.65 s), plus
  8 Bun tests. Native capture cancellation/settings restoration and capture while
  playing verified separately. Isolated helper cases also pass with Isaac 5.1 USD.
  Idle CPU remains high after eco restoration; reducing the async-render cap from
  120 to 60 Hz did not measurably help, so that setting was left unchanged.
  Current changes pass 42 isolated Python cases and 19 Bun tests; Ruff and TypeScript
  checks pass. After user reload, live smoke checks verified 960×720 bounds produce
  960×540 output from the shared 1280×720 viewport; success/cancel/error task events,
  retained results/tracebacks, and pi completion wakeup delivery also passed without
  changing the scene/camera. Full live gate and forced-timeout/disposal checks were
  not rerun in the user's working simulator. Helpers document shared-view framing,
  Kit-frame yielding and the timeline-clock vs manual-physics distinction.
  The bounded-wait pi client also passed its real-WebSocket smoke gate after the
  user's simulator restart: ~1001 ms detach, output/image retrieval, cancellation,
  partial-error output, and FIFO behavior. After pi reload, direct tool calls also
  verified bounded waiting, result retrieval, cancellation, and actual in-session
  completion wakeup delivery. No Isaac/server reload was needed.
- NVIDIA vscode bridge on 8226 (5.1 GUI) = fallback control channel when our extension
  is down.
- This session's sandbox binds `~/.isaac-agent` writable; Isaac launched from within it
  dies with the session (bwrap --die-with-parent). Relaunch: `tools/launch_isaac.sh`.
