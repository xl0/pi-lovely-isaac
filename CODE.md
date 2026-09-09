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
- `tests/test_server.py` — protocol gate, 33 tests, system python3 (pytest + websockets 10.4).
  `tests/test_mcp_adapter.py` — MCP adapter gate over stdio, 7 tests.
  Both need a live Isaac with the extension; ~9 s total.
- `scenarios/` — exec-payload scripts used as live tests/examples (falling_cube, telemetry_bounce).
- `tools/` — dev loop: `kit_exec.py` (talk to NVIDIA 8226 bridge; 5.1 quirk: no half-close,
  small payloads only, use -F for indirect file exec), `launch_isaac.sh`, `reload_ext.sh`
  (toggle ext + purge sys.modules; app config has no hot-reload), `ws_smoke.py`,
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
- Auth: token from lockfile via `X-Isaac-Agent-Authorization` header only, rejected
  pre-handshake in process_request. JS clients use undici's WebSocket (the browser-API
  global cannot set headers).
- carb log listener: `carb.logging.acquire_logging().add_logger(cb)`, cb(source, level,
  file, line, msg), levels -2..2 = verbose..fatal; may fire off-thread -> ring deque append
  direct, push via call_soon_threadsafe + 1 s token-bucket (`dropped` count attached).
- NVIDIA 6.0.1 python_server drives coroutines manually (`_drive_coroutine`) to dodge a
  Python 3.12 "Cannot enter into task" re-entrancy issue. We use plain Tasks (needed for
  cancel); 5.1 is Python 3.11 — revisit if latest-Isaac testing hits that error.

## Clients

- `extensions/lovely-isaac/index.ts` — pi extension (single file; undici WebSocket
  from pi's own runtime, header auth). Tools isaac_exec/isaac_events; tool re-registered with
  live helperDocs after hello (registerTool replaces by name); AbortSignal → cancel;
  footer `● Isaac <ver> ▶ t`; `/isaac` command; reconnect with capped backoff.
  Run: `pi -e <repo>/extensions/lovely-isaac/index.ts`. Tested with
  openai-codex/gpt-5.6-sol (autonomous restitution experiment passed).
- `mcp/` — python package `isaac-agent-mcp` (mcp SDK low-level Server + websockets≥13
  `additional_headers` API). Registered project-scope in `.mcp.json`. Adapter owns
  timeout (default 120 s, `timeout_s` per call; host abort → cancel + shielded wait).
  Non-image media → temp files under /tmp/isaac-agent-media.

## Review hardening (2026-07-21 workflow review, 22 confirmed findings fixed)

Non-obvious ones: cancel arriving in the same websockets read burst as its exec is
pre-registered synchronously (sentinel in conn.pending; reader never yields between
buffered frames); _handle_exec has a BaseException catch-all so every exec gets a
response (RecursionError in json.dumps, broken __repr__/__str__); _serialize uses
allow_nan=False (NaN/Inf would emit invalid JSON that Node clients drop silently);
non-dict params / unhashable or null ids answered with errors instead of killing the
reader; camera capture uses force_new render products, resets a STEPPED orchestrator,
and restores timeline auto-update on cancel (step_async disables it); step() pauses
in finally; CLI rejects rpc calls on closed sockets, times out connect/hello, exits
watch on server death, double-SIGINT force-quits.

## Environment

- Isaac Sim 5.1.0 GUI, conda env `~/miniforge3/envs/isaacsim` (Python 3.11, websockets 12).
  Launched via `tools/launch_isaac.sh` (nohup; includes jupyter ext parity with research
  env.sh — that file is outside this repo and read-only for sandboxed sessions).
- Isaac 6.0.1 in conda env `isaacsim6` (Python 3.12, pip `isaacsim[all,extscache]`),
  launch: `~/miniforge3/envs/isaacsim6/bin/isaacsim --ext-folder <repo>/exts --enable
  xl0.lovely.isaac`. Full gate passes there; version notes in DESIGN.md ("Isaac 6.0.1
  compatibility"). No 8226 bridge → reload_ext.sh falls back to a detached-task toggle
  through our own server.
- NVIDIA vscode bridge on 8226 (5.1 GUI) = fallback control channel when our extension
  is down.
- This session's sandbox binds `~/.isaac-agent` writable; Isaac launched from within it
  dies with the session (bwrap --die-with-parent). Relaunch: `tools/launch_isaac.sh`.
