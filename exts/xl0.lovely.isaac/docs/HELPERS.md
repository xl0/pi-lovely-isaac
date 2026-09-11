# Driving Isaac Sim via exec

Your code runs inside the running Isaac Sim process (Kit), on its main asyncio
loop, in **one persistent namespace** shared by all clients — variables survive
across calls and reconnects. Top-level `await` is supported. Pre-imported:
`agent`, `asyncio`, `np` (numpy), `omni`, `carb`. Everything else: import it.
The full Isaac/USD/Kit Python API is available; `agent` covers the fiddly parts.

**Honest limits**: synchronous code blocks the whole simulator UI — nothing can
preempt it. Cancellation (client timeout/abort) takes effect only at `await`
points. Like a notebook cell, the value of the last top-level expression is
returned (null if the code ends in a statement). stdout is captured and returned
at completion, not streamed.

## agent helpers

- `await agent.viewport(width=None, height=None, camera=None) -> ndarray` —
  captured RGBA frame (H,W,4 uint8). Default: active viewport as the user sees
  it, resized without stretching: one dimension scales proportionally; two
  dimensions are a bounding box (e.g. 1280×720 into 960×720 gives 960×540).
  With `camera` (prim path): offscreen render at the requested resolution, user
  viewport untouched. Offscreen defaults to 1280×720; one dimension assumes 16:9.
  Dimensions must be positive integers.
  Active-viewport timeouts include camera/render/timeline diagnostics without
  changing settings or stopping Replicator. FPS is not proof of a fresh frame.
  Camera capture requires a STOPPED Replicator orchestrator; it refuses to take
  over a user job. Its render resources/settings are restored even on cancel.
  Async cleanup waits share a 10 s deadline; a stuck orchestrator is reported and
  may require manual recovery before another camera capture.
- `agent.image(x, name=None)` — attach ndarray / PIL image / matplotlib figure /
  PNG bytes as PNG to this call's result (it lands in your context as an image).
- `agent.attach(data: bytes, mime: str, name=None)` — attach raw bytes (npz,
  JSON dumps, point clouds...).
- `agent.emit(name, payload=None)` — push an `event` notification to subscribed
  clients; safe from physics callbacks (telemetry channel). Payloads must be
  JSON-compatible (no NaN/Infinity); invalid payloads raise.
- `agent.watch(task, label, notify=False) -> asyncio.Task` — register one terminal
  event for a native Task, returning the same Task. Reports `ok`, `error` (with
  traceback), or `cancelled` to the registering connection's event buffer.
  `notify=True` additionally wakes pi when idle, or queues a steer when busy;
  MCP only buffers the event. Register from an event-subscribed exec (pi/MCP).
  This does not schedule the task, retain a strong reference, or attach its result.
- `agent.logs(n=50, min_severity="warning") -> list[dict]` — recent Carbonite
  log entries (severity: verbose|info|warning|error|fatal).
- `agent.play()` / `agent.pause()` / `agent.stop()` — timeline control; stop
  resets sim time.
- `await agent.step(n=1) -> simTime` — advance exactly n update steps, then pause.
- `agent.state(paths) -> dict` — composed USD world pose per prim path:
  `{"pose": {"pos", "quat_wxyz"}, "lin_vel"?, "ang_vel"?}` (velocities for rigid
  bodies). `paths`: str or list.
  Not a direct PhysX query: disabled USD writeback or stronger authored layers can
  hide simulated poses. Author physics fixtures in the simulation's edit target;
  session-layer transforms can mask root-layer physics updates.
- `agent.status() -> dict` — stage path, playing, simTime, fps, viewport info.
  `simTime` is the timeline clock, not accumulated manual physics time. A paused
  timeline can stay at zero while a controller advances physics; report controller
  progress with explicit telemetry.
- `await agent.preview_asset(url, image=True) -> dict` — inspect an asset (USD
  file/omniverse URL) before referencing it: default prim, prim counts, bounds,
  variants, physics APIs, plus a thumbnail or offscreen preview render attached
  as an image (render skipped while the timeline plays). Render previews use a
  unique temporary prim namespace, removing only their own specs. Replicator may
  temporarily author root-layer overrides; this is not a read-only operation.
- `agent.docs() -> str` — this document.

## Recipes

Screenshot: `agent.image(await agent.viewport())`

Shared-view framing (changes the visible view and leaves it there):

```python
from omni.kit.viewport.utility import get_active_viewport, frame_viewport_prims
# Assumes the active camera is an inspection camera, not a calibrated camera.
assert frame_viewport_prims(get_active_viewport(), ["/World/Gripper"])
for _ in range(8):
    await omni.kit.app.get_app().next_update_async()
agent.image(await agent.viewport(width=960))
```

Those waits let the view change render; they are not a fresh-frame guarantee.
Offscreen `camera=...` capture does not show the user what the agent is looking at.

Fly and measure — the sim runs on Kit's loop, so start behavior, let it run,
then inspect the live sim in later calls:

```python
import omni.physx
def on_step(dt):
    ...  # control law; agent.emit("pose", {...}) for telemetry
sub = omni.physx.get_physx_interface().subscribe_physics_step_events(on_step)
agent.play()   # returns immediately; keep `sub` referenced in the namespace
```

Long job (minutes+): don't block the call — detach a task, poll it later.
Bind the Task to a variable (asyncio holds only weak refs):

```python
t = asyncio.ensure_future(long_coro())   # this call returns immediately
# Optional: terminal-event wakeup, not per-sample telemetry.
agent.watch(t, "contact/release experiment", notify=True)
# later calls: t.done(), t.result(), t.cancel()
```

Watching consumes asyncio's unhandled-exception warning but leaves the exception
and traceback on the retained Task. Each Task can be watched once per connection.
Tasks/namespace survive client reconnects, **not Isaac process exit**. Extension
reload replaces the exec namespace, so it is not a persistence mechanism either.
Delivery is best-effort to the registering connection only: no offline replay,
rerouting to other sessions, or wakeup after that connection closes. After
reconnecting, inspect `t`, or explicitly call `agent.watch` again to rearm delivery
(an already-completed Task reports immediately).

To yield actual Kit frames between small batches:

```python
for batch in batches:
    advance_a_small_batch(batch)
    await omni.kit.app.get_app().next_update_async()
```

`asyncio.sleep(0)` yields Python tasks, not necessarily a Kit UI frame. When stepping
physics manually, disable timeline auto-stepping or keep it paused so UI updates
do not add physics steps. Synchronous native calls and large batches can still
freeze the UI; cancellation waits for an await point.

Media must be attached by the call that returns it: attach from the exec that
fetches results, not from inside a background task (the sink closes per-call).
