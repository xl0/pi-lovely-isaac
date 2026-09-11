"""The `agent` helper object injected into the exec namespace, plus the media sink."""

from __future__ import annotations

import asyncio
import ctypes
import io
import os
import sys
import uuid
from contextvars import ContextVar

import carb.settings
import numpy as np
import omni.kit.app
import omni.timeline
import omni.usd
from omni.kit.viewport.utility import capture_viewport_to_buffer, get_active_viewport
from pxr import Gf, Usd, UsdGeom, UsdLux, UsdPhysics

# Media sink for the currently running exec request. Set per exec task; inherited
# by tasks the exec spawns, so a leaked background coroutine hits a closed sink.
_media_ctx: ContextVar[MediaSink | None] = ContextVar("isaac_agent_media", default=None)

_SEVERITY_ORDER = {"verbose": -2, "info": -1, "warning": 0, "error": 1, "fatal": 2}
_CAMERA_CLEANUP_TIMEOUT_S = 10.0
_ACTIVE_VIEWPORT_CAPTURE_TIMEOUT_S = 15.0


class MediaSink:
    """Collects media payloads for one exec request; closed when the request resolves."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.closed = False

    def attach(self, data: bytes, mime: str, name: str | None = None) -> None:
        if self.closed:
            raise RuntimeError(
                "media sink closed: this exec request already completed "
                "(attach media from the exec that fetches it, not from a leaked background task)"
            )
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"data must be bytes, got {type(data).__name__}")
        self.items.append({"mimeType": mime, "data": bytes(data), "name": name})

    def close(self) -> None:
        self.closed = True


def _to_png(x) -> bytes:
    """ndarray / PIL image / matplotlib figure / image bytes -> PNG bytes."""
    if isinstance(x, (bytes, bytearray)):
        if bytes(x[:8]) == b"\x89PNG\r\n\x1a\n":
            return bytes(x)
        from PIL import Image

        try:
            img = Image.open(io.BytesIO(x))  # non-PNG image bytes: re-encode
        except Exception:
            raise ValueError(
                "bytes passed to agent.image are not a decodable image — use agent.attach for raw data"
            ) from None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    if hasattr(x, "savefig"):  # matplotlib Figure
        buf = io.BytesIO()
        x.savefig(buf, format="png", bbox_inches="tight")
        return buf.getvalue()
    from PIL import Image

    if isinstance(x, Image.Image):
        img = x
    else:
        arr = np.asarray(x)
        if arr.dtype != np.uint8:
            raise TypeError(f"array must be uint8 (got {arr.dtype}); scale/convert it first")
        if arr.ndim == 3 and arr.shape[2] == 4:
            arr = arr[:, :, :3]  # drop alpha: viewport alpha is unreliable
        img = Image.fromarray(arr)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class Agent:
    """Curated helper surface for driving Isaac Sim from exec'd code.

    Injected into the persistent exec namespace as `agent`. See docs/HELPERS.md
    (served as `helperDocs` in the hello response) for the user-facing contract.
    """

    def __init__(self, server) -> None:
        self._server = server

    # ---------------------------------------------------------------- capture

    async def viewport(self, width: int | None = None, height: int | None = None, camera: str | None = None):
        """RGBA uint8 ndarray (H, W, 4) of a rendered frame.

        Without `camera`: captures the active viewport as the user sees it.
        Dimensions preserve aspect ratio; two dimensions are a bounding box.
        With `camera` (prim path): renders through an offscreen render product
        at the requested resolution; the user's viewport is never touched.
        """
        for name, value in (("width", width), ("height", height)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        if camera is not None:
            return await self._capture_camera(str(camera), width, height)
        arr = await self._capture_active_viewport()
        h, w = arr.shape[:2]
        if width and not height:
            height = max(1, round(h * width / w))
        elif height and not width:
            width = max(1, round(w * height / h))
        elif width and height:
            scale = min(width / w, height / h)
            width = min(width, max(1, round(w * scale)))
            height = min(height, max(1, round(h * scale)))
        if width and height:
            from PIL import Image

            img = Image.fromarray(arr).resize((width, height), Image.Resampling.LANCZOS)
            arr = np.asarray(img)
        return arr

    async def _capture_active_viewport(self):
        vp = get_active_viewport()
        if vp is None:
            raise RuntimeError("no active viewport")
        loop = asyncio.get_event_loop()
        fut = loop.create_future()

        def on_capture(buf, size, width, height, fmt):
            try:
                ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
                ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
                ptr = ctypes.pythonapi.PyCapsule_GetPointer(buf, None)
                data = bytes(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_byte * size)).contents)
                result = (data, width, height, str(fmt))
                loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(result))
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(lambda exc=e: fut.done() or fut.set_exception(exc))

        capture_viewport_to_buffer(vp, on_capture)
        try:
            data, width, height, fmt = await asyncio.wait_for(fut, timeout=_ACTIVE_VIEWPORT_CAPTURE_TIMEOUT_S)
        except TimeoutError:
            # Observe only: never import/enable Replicator or change render settings.
            rep = sys.modules.get("omni.replicator.core")
            try:
                status = str(rep.orchestrator.get_status()) if rep else "unavailable: module not loaded"
            except AttributeError:  # get_status reads _orchestrator.status; initialization may be incomplete.
                status = "unavailable: orchestrator not initialized"
            settings = carb.settings.get_settings()
            diag = {
                "camera": str(vp.camera_path),
                "updates_enabled": vp.updates_enabled,
                "replicator_status": status,
                "asyncRendering": settings.get("/app/asyncRendering"),
                "eco": settings.get("/rtx/ecoMode/enabled"),
                "timeline_playing": omni.timeline.get_timeline_interface().is_playing(),
                "fps": getattr(vp, "fps", None),
            }
            raise RuntimeError(
                "active viewport capture timed out after "
                f"{_ACTIVE_VIEWPORT_CAPTURE_TIMEOUT_S:g} s ("
                + ", ".join(f"{key}={value!r}" for key, value in diag.items())
                + ")"
            ) from None
        if "RGBA8" not in fmt:
            raise RuntimeError(f"unexpected capture format {fmt}")
        return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4).copy()

    async def _capture_camera(self, camera: str, width: int | None, height: int | None):
        import carb.settings
        import omni.replicator.core as rep

        # STEPPED/PAUSED may belong to a user's job, not a stale helper capture.
        if rep.orchestrator.get_status() != rep.orchestrator.Status.STOPPED:
            raise RuntimeError("camera capture requires a STOPPED Replicator orchestrator")
        if getattr(self, "_camera_capture_active", False):
            raise RuntimeError("another camera capture is still running or cleaning up")
        if width and not height:
            height = round(width * 9 / 16)
        elif height and not width:
            width = round(height * 16 / 9)
        resolution = (width or 1280, height or 720)
        settings = carb.settings.get_settings()
        tl = omni.timeline.get_timeline_interface()
        auto_update = tl.is_auto_updating()
        play_every_frame = tl.get_play_every_frame()
        async_rendering = settings.get("/app/asyncRendering")
        capture_on_play = settings.get("/omni/replicator/captureOnPlay")
        # Native stop only restores these after its async wait completes.
        render_settings = {
            key: value
            for key in rep.orchestrator.SETTINGS_TO_SAVE
            if (value := settings.get(key)) is not None
        }
        rp = ann = None
        stepped = False
        self._camera_capture_active = True

        async def cleanup():
            deadline = asyncio.get_running_loop().time() + _CAMERA_CLEANUP_TIMEOUT_S
            try:
                try:
                    if ann is not None:
                        ann.detach()
                finally:
                    if rp is not None:
                        rp.destroy()  # pyright: ignore[reportAttributeAccessIssue] - runtime HydraTexture
            finally:
                try:
                    if stepped:
                        # stop_async restores cached RTX settings (including eco mode).
                        async with asyncio.timeout_at(deadline):
                            await rep.orchestrator.stop_async()
                finally:
                    try:
                        if stepped:
                            # Both Isaac 5.1 and 6 defer asyncRendering restoration by
                            # five updates to avoid a hang after annotator destruction.
                            # Also cover cancellation before Replicator caches settings.
                            async with asyncio.timeout_at(deadline):
                                for _ in range(6):
                                    await omni.kit.app.get_app().next_update_async()  # pyright: ignore[reportAttributeAccessIssue]
                    finally:
                        try:
                            if stepped:
                                for key, value in render_settings.items():
                                    settings.set(key, value)
                                settings.set("/app/asyncRendering", async_rendering)
                            tl.set_auto_update(auto_update)
                            tl.set_play_every_frame(play_every_frame)
                            tl.commit_silently()
                            settings.set("/omni/replicator/captureOnPlay", capture_on_play)
                        finally:
                            self._camera_capture_active = False

        try:
            # Otherwise attaching while playing starts Replicator, and stopping it
            # also stops/resets the user's timeline.
            rep.orchestrator.set_capture_on_play(False)
            # force_new: never adopt (and later destroy) a render product the user created
            rp = rep.create.render_product(camera, resolution, force_new=True)
            ann = rep.AnnotatorRegistry.get_annotator("rgb")
            ann.attach(rp)  # pyright: ignore[reportArgumentType] - runtime accepts HydraTexture
            # renders one frame for this product without advancing sim time
            stepped = True
            try:
                await asyncio.wait_for(
                    rep.orchestrator.step_async(delta_time=0.0, pause_timeline=False), 60.0
                )
            except TimeoutError:
                raise RuntimeError(f"render for camera {camera!r} timed out after 60 s") from None
            d = ann.get_data()
            if d is None or not getattr(d, "size", 0):
                raise RuntimeError(f"no frame rendered for camera {camera!r}")
            return np.asarray(d, dtype=np.uint8).reshape(resolution[1], resolution[0], -1).copy()
        finally:
            # Keep ownership until cleanup completes, even if canceled again while
            # awaiting stop. Shield alone would let the exec return before restoration.
            task = asyncio.create_task(cleanup())
            canceled = False
            try:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        canceled = True
                task.result()
            except TimeoutError:
                raise RuntimeError(
                    f"camera cleanup timed out after {_CAMERA_CLEANUP_TIMEOUT_S:g} s; "
                    "Replicator may require manual recovery before another capture"
                ) from None
            if canceled:
                raise asyncio.CancelledError

    # ------------------------------------------------------------------ media

    def image(self, x, name: str | None = None) -> None:
        """Encode ndarray / PIL image / matplotlib figure / PNG bytes as PNG and
        attach it to this exec request's media."""
        self.attach(_to_png(x), "image/png", name=name or "image")

    def attach(self, data: bytes, mime: str, name: str | None = None) -> None:
        """Attach raw bytes with a mime type to this exec request's media."""
        sink = _media_ctx.get(None)
        if sink is None:
            raise RuntimeError("no active exec request (agent.attach only works inside exec'd code)")
        sink.attach(data, mime, name)

    # ----------------------------------------------------------------- events

    def emit(self, name: str, payload=None) -> None:
        """Push an `event` notification to subscribed clients. Safe from physics
        callbacks; delivery is fire-and-forget. Payloads must be JSON-compatible
        (no NaN/Infinity); invalid payloads raise."""
        self._server.emit_event(str(name), payload)

    def watch(self, task: asyncio.Task, label: str, *, notify: bool = False) -> asyncio.Task:
        """Send one terminal event to this exec's client; opt in to a pi wakeup.

        Keep the returned native Task referenced for its result/exception. No
        scheduling, persistence, or automatic replay after a connection closes.
        """
        return self._server.watch_task(task, label, notify=notify)

    def logs(self, n: int = 50, min_severity: str = "warning") -> list[dict]:
        """Most recent Carbonite log entries (up to n) at or above min_severity.

        Entries: {"t": unix_time, "severity": str, "source": str, "message": str}.
        """
        level = _SEVERITY_ORDER.get(min_severity)
        if level is None:
            raise ValueError(f"min_severity must be one of {sorted(_SEVERITY_ORDER)}")
        # list() snapshots atomically; the carb logger appends from other threads
        out = [e for e in list(self._server.log_ring) if _SEVERITY_ORDER[e["severity"]] >= level]
        return out[-n:]

    # --------------------------------------------------------------- timeline

    def play(self) -> None:
        """Start the timeline (physics/animation run continuously)."""
        tl = omni.timeline.get_timeline_interface()
        tl.play()
        tl.commit()  # timeline ops are frame-queued; commit applies immediately

    def pause(self) -> None:
        """Pause the timeline; sim time is preserved."""
        tl = omni.timeline.get_timeline_interface()
        tl.pause()
        tl.commit()

    def stop(self) -> None:
        """Stop the timeline and reset sim time to start."""
        tl = omni.timeline.get_timeline_interface()
        tl.stop()
        tl.commit()

    async def step(self, n: int = 1) -> float:
        """Advance exactly n update steps then pause. Returns sim time."""
        tl = omni.timeline.get_timeline_interface()
        app = omni.kit.app.get_app()
        if not tl.is_playing():
            tl.play()
            tl.commit()
        try:
            for _ in range(int(n)):
                await app.next_update_async()  # pyright: ignore[reportAttributeAccessIssue] - runtime monkey-patch, absent from binding stub
        finally:
            tl.pause()  # also on cancel: never leave the sim running unrequested
            tl.commit()
        return tl.get_current_time()

    # ------------------------------------------------------------------ state

    def state(self, paths) -> dict:
        """Per-prim composed USD world pose (+ USD velocities for rigid bodies).

        Not a native PhysX query: stronger authored layers or disabled USD
        writeback can hide simulation updates.

        paths: str or list of prim paths. Returns
        {path: {"pose": {"pos": [x,y,z], "quat_wxyz": [w,x,y,z]},
                "lin_vel"?: [...], "ang_vel"?: [...]} | None}.
        """
        stage = omni.usd.get_context().get_stage()  # pyright: ignore[reportAttributeAccessIssue] - runtime monkey-patch, absent from binding stub
        if isinstance(paths, str):
            paths = [paths]
        out = {}
        for p in paths:
            prim = stage.GetPrimAtPath(str(p))
            if not prim or not prim.IsValid():
                out[str(p)] = None
                continue
            xf = Gf.Transform(omni.usd.get_world_transform_matrix(prim))
            pos = xf.GetTranslation()
            q = xf.GetRotation().GetQuat()
            entry: dict = {
                "pose": {
                    "pos": [pos[0], pos[1], pos[2]],
                    "quat_wxyz": [q.GetReal(), *q.GetImaginary()],
                }
            }
            if prim.HasAPI(UsdPhysics.RigidBodyAPI):
                rb = UsdPhysics.RigidBodyAPI(prim)
                v = rb.GetVelocityAttr().Get()
                w = rb.GetAngularVelocityAttr().Get()
                if v is not None:
                    entry["lin_vel"] = list(v)
                if w is not None:
                    entry["ang_vel"] = list(w)
            out[str(p)] = entry
        return out

    def status(self) -> dict:
        """Stage, playing, timeline clock (not manually advanced physics time), viewport info."""
        tl = omni.timeline.get_timeline_interface()
        d = {
            "stagePath": omni.usd.get_context().get_stage_url(),
            "playing": tl.is_playing(),
            "simTime": tl.get_current_time(),
            "appUptime": omni.kit.app.get_app().get_time_since_start_s(),
        }
        try:
            vp = get_active_viewport()
            if vp is not None:
                d["viewport"] = {"resolution": list(vp.resolution), "camera": str(vp.camera_path)}
                fps = getattr(vp, "fps", None)
                if fps:
                    d["fps"] = fps
        except Exception:  # noqa: BLE001 - viewport optional (headless)
            pass
        return d

    def docs(self) -> str:
        """Full helper documentation (same markdown served as helperDocs)."""
        return self._server.helper_docs

    # ---------------------------------------------------------- asset preview

    async def preview_asset(self, url: str, image: bool = True) -> dict:
        """Inspect an asset before referencing it into the scene.

        Tiers: (1) existing Omniverse thumbnail beside the asset, (2) metadata
        from an independent Usd.Stage.Open (default prim, prim counts, bounds,
        variants, physics APIs) — zero effect on the open stage, (3) if `image`
        and no thumbnail: temporary reference in the session layer under
        a unique /AgentPreview_* path rendered offscreen (refused while the timeline plays).
        Attaches the image via agent.image; returns the metadata dict.
        """
        url = str(url)
        stage = Usd.Stage.Open(url)
        if stage is None:
            raise RuntimeError(f"cannot open {url!r}")
        meta: dict = {"url": url}
        default_prim = stage.GetDefaultPrim()
        meta["defaultPrim"] = default_prim.GetPath().pathString if default_prim else None
        counts: dict[str, int] = {}
        physics = []
        for prim in stage.Traverse():
            t = str(prim.GetTypeName()) or "(untyped)"
            counts[t] = counts.get(t, 0) + 1
            if prim.HasAPI(UsdPhysics.RigidBodyAPI) and "rigidBody" not in physics:
                physics.append("rigidBody")
            if prim.HasAPI(UsdPhysics.CollisionAPI) and "collision" not in physics:
                physics.append("collision")
        meta["primCounts"] = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:15])
        meta["physicsAPIs"] = physics
        root = default_prim if default_prim else stage.GetPseudoRoot()
        bbox = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]
        ).ComputeWorldBound(root)
        rng = bbox.ComputeAlignedRange()
        diag = 1.0
        if not rng.IsEmpty():
            lo, hi = rng.GetMin(), rng.GetMax()
            size = [hi[i] - lo[i] for i in range(3)]
            meta["bounds"] = {"min": list(lo), "max": list(hi), "size": size}
            diag = max(sum(s * s for s in size) ** 0.5, 1e-3)
        variants = {}
        for name in root.GetVariantSets().GetNames() if root else []:
            vs = root.GetVariantSet(name)
            variants[name] = {"values": vs.GetVariantNames(), "current": vs.GetVariantSelection()}
        if variants:
            meta["variants"] = variants
        del stage

        if image and self._attach_thumbnail(url):
            meta["image"] = "thumbnail"
        elif image:
            meta["image"] = await self._preview_render(url, rng, diag)
        return meta

    def _attach_thumbnail(self, url: str) -> bool:
        import omni.client

        folder, _, name = url.rpartition("/")
        if not folder:
            folder, name = os.path.dirname(os.path.abspath(url)), os.path.basename(url)
        thumb = f"{folder}/.thumbs/256x256/{name}.png"
        result, _, content = omni.client.read_file(thumb)
        if result != omni.client.Result.OK or not content:
            return False
        try:
            self.image(bytes(content), name="thumbnail")  # validates/re-encodes; never mislabels
        except ValueError:  # corrupt thumbnail: fall through to a live render
            return False
        return True

    async def _preview_render(self, url: str, rng, diag: float) -> str:
        if omni.timeline.get_timeline_interface().is_playing():
            return "skipped: timeline is playing (pause/stop to allow a preview render)"
        stage = omni.usd.get_context().get_stage()  # pyright: ignore[reportAttributeAccessIssue] - runtime monkey-patch, absent from binding stub
        session = stage.GetSessionLayer()
        while True:
            path = f"/AgentPreview_{uuid.uuid4().hex}"
            if not stage.GetPrimAtPath(path) and not any(
                layer.GetPrimAtPath(path) for layer in stage.GetLayerStack()
            ):
                break
        # far from the scene so the transient prims don't collide with user content;
        # camera/eye math is root-local (parent carries the offset)
        offset = Gf.Vec3d(0, 0, 10_000)
        center = (
            Gf.Vec3d(0, 0, 0)
            if rng.IsEmpty()
            else Gf.Vec3d(*[(rng.GetMin()[i] + rng.GetMax()[i]) / 2 for i in range(3)])
        )
        try:
            with Usd.EditContext(stage, session):
                root = UsdGeom.Xform.Define(stage, path)
                UsdGeom.XformCommonAPI(root).SetTranslate(offset)
                asset = stage.DefinePrim(f"{path}/Asset")
                asset.GetReferences().AddReference(url)
                light = UsdLux.DistantLight.Define(stage, f"{path}/Sun")
                light.CreateIntensityAttr(2500.0)
                UsdGeom.XformCommonAPI(light).SetRotate(Gf.Vec3f(-40, 30, 0))
                cam = UsdGeom.Camera.Define(stage, f"{path}/Cam")
                eye = center + Gf.Vec3d(1, -1, 0.6).GetNormalized() * (1.8 * diag)
                view = Gf.Matrix4d().SetLookAt(eye, center, Gf.Vec3d(0, 0, 1))
                UsdGeom.Xformable(cam).AddTransformOp().Set(view.GetInverse())
                cam.CreateClippingRangeAttr(Gf.Vec2f(max(0.01, diag * 0.01), diag * 100))
            app = omni.kit.app.get_app()
            for _ in range(10):  # let hydra load the reference before capturing
                await app.next_update_async()  # pyright: ignore[reportAttributeAccessIssue] - runtime monkey-patch, absent from binding stub
            self.image(await self._capture_camera(f"{path}/Cam", 512, 384), name="preview")
            return "rendered"
        finally:
            # Replicator can author overs outside the session layer. This namespace
            # was absent from every local layer before we reserved it; never remove
            # the old fixed /AgentPreview path or any other preexisting specs.
            for layer in stage.GetLayerStack():
                if layer.GetPrimAtPath(path):
                    with Usd.EditContext(stage, layer):
                        stage.RemovePrim(path)
