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
  it. With `camera` (prim path): offscreen render, user viewport untouched.
  Camera capture requires a STOPPED Replicator orchestrator; it refuses to take
  over a user job. Its render resources/settings are restored even on cancel.
  Async cleanup waits share a 10 s deadline; a stuck orchestrator is reported and
  may require manual recovery before another camera capture.
- `agent.image(x, name=None)` — attach ndarray / PIL image / matplotlib figure /
  PNG bytes as PNG to this call's result (it lands in your context as an image).
- `agent.attach(data: bytes, mime: str, name=None)` — attach raw bytes (npz,
  JSON dumps, point clouds...).
- `agent.emit(name, payload=None)` — push an `event` notification to subscribed
  clients; safe from physics callbacks (telemetry channel).
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
- `await agent.preview_asset(url, image=True) -> dict` — inspect an asset (USD
  file/omniverse URL) before referencing it: default prim, prim counts, bounds,
  variants, physics APIs, plus a thumbnail or offscreen preview render attached
  as an image (render skipped while the timeline plays). Render previews use a
  unique temporary prim namespace, removing only their own specs. Replicator may
  temporarily author root-layer overrides; this is not a read-only operation.
- `agent.docs() -> str` — this document.

## Recipes

Screenshot: `agent.image(await agent.viewport())`

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
# later calls: t.done(), t.result(), t.cancel()
```

Media must be attached by the call that returns it: attach from the exec that
fetches results, not from inside a background task (the sink closes per-call).
