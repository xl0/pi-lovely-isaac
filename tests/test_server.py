"""Protocol gate for the isaac-agent server. Live tests need Isaac Sim with
xl0.lovely.isaac enabled (lockfile in ~/.isaac-agent). Run: pytest tests/ -v

The isolated helper regressions use numpy + pxr and never connect to Isaac.

Tests share the live server; each uses uniquely named namespace vars.
"""

import asyncio
import base64
import glob
import importlib.util
import json
import os
import struct
import sys
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
import websockets.client
import websockets.exceptions

LOCK = None


def lock():
    global LOCK
    if LOCK is None:
        locks = sorted(glob.glob(os.path.expanduser("~/.isaac-agent/*.lock")), key=os.path.getmtime)
        assert locks, "no isaac-agent lockfile — is Isaac running with xl0.lovely.isaac?"
        with open(locks[-1]) as f:
            LOCK = json.load(f)
    return LOCK


class Client:
    def __init__(self, subscriptions=(), hello=True):
        self.subscriptions = list(subscriptions)
        self.do_hello = hello
        self.notifications = []
        self.next_id = 0

    async def __aenter__(self):
        info = lock()
        self.ws = await websockets.client.connect(
            f"ws://127.0.0.1:{info['port']}",
            extra_headers={"X-Isaac-Agent-Authorization": info["token"]},
            max_size=2**26,
        )
        if self.do_hello:
            self.hello = await self.call(
                "hello",
                {
                    "protocolVersion": 1,
                    "client": {"name": "pytest", "version": "0", "pid": os.getpid()},
                    "subscriptions": self.subscriptions,
                },
            )
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def call(self, method, params=None, timeout=60):
        self.next_id += 1
        rpc_id = self.next_id
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}})
        )
        return await self.wait_response(rpc_id, timeout)

    async def send_request(self, method, params):
        """Send without waiting; returns the request id."""
        self.next_id += 1
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        )
        return self.next_id

    async def wait_response(self, rpc_id, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), deadline - time.monotonic()))
            if msg.get("id") == rpc_id:
                if "error" in msg:
                    return {"__rpc_error__": msg["error"]}
                return msg["result"]
            if "method" in msg:
                self.notifications.append(msg)

    async def notify(self, method, params):
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    async def exec(self, code, timeout=60):
        return await self.call("exec", {"code": code}, timeout)

    async def wait_notification(self, method, timeout=10):
        for i, n in enumerate(self.notifications):
            if n["method"] == method:
                return self.notifications.pop(i)["params"]
        deadline = time.monotonic() + timeout
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), deadline - time.monotonic()))
            if msg.get("method") == method:
                return msg["params"]
            if "method" in msg:
                self.notifications.append(msg)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------- isolated helper regressions


@pytest.fixture
def helpers(monkeypatch):
    """Real helper code and USD, mocked Kit: never connects to the live simulator.

    Run with a Python containing numpy and pxr (e.g. the Isaac conda Python).
    """
    pytest.importorskip("numpy")
    pytest.importorskip("pxr.Usd")
    for name in (
        "carb",
        "carb.settings",
        "omni",
        "omni.kit",
        "omni.kit.app",
        "omni.timeline",
        "omni.usd",
        "omni.kit.viewport",
        "omni.kit.viewport.utility",
        "omni.replicator",
        "omni.replicator.core",
    ):
        module = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, module)
    utility = sys.modules["omni.kit.viewport.utility"]
    utility.capture_viewport_to_buffer = Mock()
    utility.get_active_viewport = Mock()
    path = Path(__file__).resolve().parents[1] / "exts/xl0.lovely.isaac/xl0/lovely/isaac/helpers.py"
    spec = importlib.util.spec_from_file_location("isolated_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "create_error",
        "attach_error",
        "init_error",
        "step_error",
        "timeout",
        "cancel_init",
        "cancel_step",
        "cancel_cleanup",
        "detach_error",
        "stop_timeout",
        "cancel_stop_timeout",
        "restore_timeout",
    ],
)
def test_camera_owned_cleanup(helpers, outcome):
    async def t():
        rep = sys.modules["omni.replicator.core"]
        values = {
            "/app/asyncRendering": True,
            "/rtx/ecoMode/enabled": True,
            "/omni/replicator/captureOnPlay": True,
            "/app/viewport/grid/enabled": True,
        }
        before = values.copy()
        settings = Mock(get=values.get, set=values.__setitem__)
        sys.modules["carb.settings"].get_settings = lambda: settings
        timeline = {"auto": False, "every": False}
        tl = Mock(
            is_auto_updating=lambda: timeline["auto"],
            get_play_every_frame=lambda: timeline["every"],
            set_auto_update=lambda v: timeline.__setitem__("auto", v),
            set_play_every_frame=lambda v: timeline.__setitem__("every", v),
        )
        helpers.omni.timeline.get_timeline_interface = lambda: tl
        entered = asyncio.Event()
        stopping = asyncio.Event()
        finish_stop = asyncio.Event()
        delayed_restore = []
        updates = 0
        cached = False
        status = "STOPPED"
        stop_canceled = False
        helpers._CAMERA_CLEANUP_TIMEOUT_S = 0.05

        async def update():
            nonlocal updates
            updates += 1
            if outcome == "restore_timeout":
                await finish_stop.wait()
            if delayed_restore and updates >= delayed_restore[0]:
                values["/app/asyncRendering"] = True
                delayed_restore.clear()
            await asyncio.sleep(0)

        helpers.omni.kit.app.get_app = lambda: types.SimpleNamespace(next_update_async=update)

        async def step(**kwargs):
            nonlocal status, cached
            assert kwargs == {"delta_time": 0.0, "pause_timeline": False}
            assert values["/omni/replicator/captureOnPlay"] is False
            status = "INITIALIZING"
            values["/app/asyncRendering"] = False
            timeline["auto"] = False
            if outcome == "init_error":
                raise ValueError("initialization failed before settings were cached")
            if outcome != "cancel_init":
                cached = True
                values["/rtx/ecoMode/enabled"] = False
                values["/app/viewport/grid/enabled"] = False
                timeline["every"] = True
                status = "STEPPED"
            entered.set()
            if outcome.startswith("cancel_") and outcome not in ("cancel_cleanup", "cancel_stop_timeout"):
                await asyncio.Future()
            if outcome == "step_error":
                raise ValueError("render failed")
            if outcome == "timeout":
                raise TimeoutError

        async def stop():
            nonlocal status, stop_canceled
            stopping.set()
            if outcome in ("stop_timeout", "cancel_stop_timeout"):
                status = "STOPPING"
                try:
                    await finish_stop.wait()
                finally:
                    stop_canceled = True
            if outcome == "cancel_cleanup":
                await finish_stop.wait()
            status = "STOPPED"
            timeline["auto"] = True  # Native restoration does not preserve False.
            if cached:
                values["/rtx/ecoMode/enabled"] = True
                values["/app/viewport/grid/enabled"] = True
                delayed_restore.append(updates + 5)

        rep.orchestrator = Mock(
            get_status=lambda: status,
            Status=types.SimpleNamespace(STOPPED="STOPPED"),
            SETTINGS_TO_SAVE=["/rtx/ecoMode/enabled", "/app/viewport/grid/enabled"],
            step_async=step,
            stop_async=Mock(side_effect=stop),
            set_capture_on_play=lambda v: values.__setitem__("/omni/replicator/captureOnPlay", v),
        )
        rp = Mock()
        rep.create = Mock()
        rep.create.render_product.return_value = rp
        if outcome == "create_error":
            rep.create.render_product.side_effect = ValueError("creation failed")
        ann = Mock()
        ann.get_data.return_value = helpers.np.zeros((3, 4, 4), dtype=helpers.np.uint8)
        if outcome == "attach_error":
            ann.attach.side_effect = ValueError("attach failed")
        if outcome == "detach_error":
            ann.detach.side_effect = ValueError("detach failed")
        rep.AnnotatorRegistry = Mock()
        rep.AnnotatorRegistry.get_annotator.return_value = ann
        agent = helpers.Agent(None)
        task = asyncio.create_task(agent._capture_camera("/Camera", 4, 3))
        try:
            if outcome in ("cancel_init", "cancel_step"):
                await entered.wait()
                task.cancel()
            elif outcome in ("cancel_cleanup", "cancel_stop_timeout"):
                await stopping.wait()
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()  # A second cancel must not abandon the shielded cleanup.
                await asyncio.sleep(0)
                assert not task.done()
                with pytest.raises(RuntimeError, match="another camera capture"):
                    # Exercise the STOPPED-but-still-cleaning ownership window.
                    status = "STOPPED"
                    await agent._capture_camera("/Camera", 4, 3)
                if outcome == "cancel_cleanup":
                    finish_stop.set()
            if outcome in ("stop_timeout", "cancel_stop_timeout", "restore_timeout"):
                done, _ = await asyncio.wait({task}, timeout=1)
                assert task in done, "cleanup did not release the request within its deadline"
                with pytest.raises(RuntimeError, match="camera cleanup timed out"):
                    task.result()
            elif outcome.startswith("cancel_"):
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "timeout":
                with pytest.raises(RuntimeError, match="timed out"):
                    await task
            elif outcome.endswith("error"):
                with pytest.raises(ValueError):
                    await task
            else:
                assert (await task).shape == (3, 4, 4)
            assert values == before
            assert timeline == {"auto": False, "every": False}
            if outcome == "stop_timeout":
                assert status == "STOPPING"  # Don't pretend native recovery succeeded.
            else:
                assert status == "STOPPED"
            assert stop_canceled == (outcome in ("stop_timeout", "cancel_stop_timeout"))
            if outcome != "restore_timeout":
                assert not delayed_restore
            assert not agent._camera_capture_active
            assert ann.detach.call_count == (outcome != "create_error")
            assert rp.destroy.call_count == (outcome != "create_error")
            rep.create.render_product.assert_called_once_with("/Camera", (4, 3), force_new=True)
            assert rep.orchestrator.stop_async.call_count == (outcome not in ("create_error", "attach_error"))
        finally:
            finish_stop.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(t())


@pytest.mark.parametrize("status", ["STARTED", "STARTING", "PAUSED", "STEPPED", "STOPPING"])
def test_camera_refuses_user_orchestrator(helpers, status):
    rep = sys.modules["omni.replicator.core"]
    rep.orchestrator = Mock(get_status=lambda: status, Status=types.SimpleNamespace(STOPPED="STOPPED"))
    rep.create = Mock()
    with pytest.raises(RuntimeError, match="STOPPED"):
        asyncio.run(helpers.Agent(None)._capture_camera("/Camera", 4, 3))
    rep.orchestrator.stop.assert_not_called()
    rep.orchestrator.stop_async.assert_not_called()
    rep.orchestrator.set_capture_on_play.assert_not_called()
    rep.create.render_product.assert_not_called()


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_preview_owned_specs(helpers, monkeypatch, outcome):
    from pxr import Gf, Sdf, Usd, UsdGeom

    # All test content lives in private anonymous layers, never the user's stage.
    stage = Usd.Stage.CreateInMemory()
    session = stage.GetSessionLayer()
    root = stage.GetRootLayer()
    sublayer = Sdf.Layer.CreateAnonymous()
    root.subLayerPaths.append(sublayer.identifier)
    for layer in (root, session, sublayer):
        with Usd.EditContext(stage, layer):
            UsdGeom.Xform.Define(stage, "/AgentPreview")
            UsdGeom.Xform.Define(stage, "/AgentPreview/UserContent")
    # An inactive namespace must also be treated as user-owned.
    with Usd.EditContext(stage, root):
        stage.DefinePrim("/AgentPreview_collision").SetActive(False)
    Sdf.CreatePrimInLayer(sublayer, "/AgentPreview_collision/UserContent")
    before = [layer.ExportToString() for layer in (root, session, sublayer)]
    asset = Usd.Stage.CreateInMemory()
    asset.SetDefaultPrim(UsdGeom.Xform.Define(asset, "/Asset").GetPrim())
    paths = iter(["collision", "owned"])
    monkeypatch.setattr(helpers.uuid, "uuid4", lambda: types.SimpleNamespace(hex=next(paths)))
    helpers.omni.usd.get_context = lambda: types.SimpleNamespace(get_stage=lambda: stage)
    helpers.omni.timeline.get_timeline_interface = lambda: types.SimpleNamespace(is_playing=lambda: False)

    async def update():
        await asyncio.sleep(0)

    helpers.omni.kit.app.get_app = lambda: types.SimpleNamespace(next_update_async=update)
    agent = helpers.Agent(None)
    agent.image = Mock()

    async def capture(camera, width, height):
        assert camera == "/AgentPreview_owned/Cam"
        assert stage.GetPrimAtPath(camera)
        # Mimic Replicator's overs, including a non-root local edit target.
        for layer in (root, sublayer):
            with Usd.EditContext(stage, layer):
                stage.OverridePrim(camera)
        if outcome == "error":
            raise ValueError("capture failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return helpers.np.zeros((height, width, 4), dtype=helpers.np.uint8)

    agent._capture_camera = capture
    try:
        coro = agent._preview_render(asset.GetRootLayer().identifier, Gf.Range3d(), 1.0)
        if outcome == "success":
            assert asyncio.run(coro) == "rendered"
            agent.image.assert_called_once()
        else:
            with pytest.raises(ValueError if outcome == "error" else asyncio.CancelledError):
                asyncio.run(coro)
        assert [layer.ExportToString() for layer in (root, session, sublayer)] == before
        assert stage.GetEditTarget().GetLayer() == root
    finally:
        # Release the fixture's USD references even if an assertion fails.
        root.subLayerPaths.clear()
        for layer in (session, root, sublayer):
            layer.Clear()


# ------------------------------------------------------------------ auth/hello


def test_bad_token_rejected():
    async def t():
        info = lock()
        for headers in ({}, {"X-Isaac-Agent-Authorization": "wrong"}):
            with pytest.raises(websockets.exceptions.InvalidStatusCode) as e:
                await websockets.client.connect(f"ws://127.0.0.1:{info['port']}", extra_headers=headers)
            assert e.value.status_code == 401

    run(t())


def test_query_param_token_rejected():
    async def t():
        # auth is header-only; a valid token in the URL must not authenticate
        info = lock()
        with pytest.raises(websockets.exceptions.InvalidStatusCode) as e:
            await websockets.client.connect(f"ws://127.0.0.1:{info['port']}/?token={info['token']}")
        assert e.value.status_code == 401

    run(t())


def test_exec_requires_hello():
    async def t():
        async with Client(hello=False) as c:
            r = await c.call("exec", {"code": "1"})
            assert r["__rpc_error__"]["code"] == -32002
            r = await c.call("ping")
            assert r["__rpc_error__"]["code"] == -32002

    run(t())


def test_hello_result():
    async def t():
        async with Client() as c:
            h = c.hello
            assert h["protocolVersion"] == 1
            assert h["server"]["isaacVersion"]
            assert h["server"]["kitVersion"]
            assert "agent.viewport" in h["helperDocs"]
            assert "path" in h["stage"]

    run(t())


def test_hello_bad_version():
    async def t():
        async with Client(hello=False) as c:
            r = await c.call("hello", {"protocolVersion": 99, "client": {"name": "x"}})
            assert r["__rpc_error__"]["code"] == -32001

    run(t())


# ------------------------------------------------------------------ rpc shapes


def test_ping():
    async def t():
        async with Client() as c:
            assert await c.call("ping") == {}

    run(t())


def test_unknown_method():
    async def t():
        async with Client() as c:
            r = await c.call("frobnicate")
            assert r["__rpc_error__"]["code"] == -32601

    run(t())


def test_parse_error():
    async def t():
        async with Client() as c:
            await c.ws.send("not json{")
            msg = json.loads(await asyncio.wait_for(c.ws.recv(), 5))
            assert msg["error"]["code"] == -32700

    run(t())


def test_bad_exec_params():
    async def t():
        async with Client() as c:
            r = await c.call("exec", {"code": 42})
            assert r["__rpc_error__"]["code"] == -32602

    run(t())


def test_malformed_requests_keep_connection_alive():
    async def t():
        async with Client() as c:
            # params as array (positional) -> -32602, reader must survive
            await c.ws.send(json.dumps({"jsonrpc": "2.0", "id": 990, "method": "exec", "params": ["x"]}))
            msg = json.loads(await asyncio.wait_for(c.ws.recv(), 5))
            assert msg["error"]["code"] == -32602
            # unhashable id -> -32600 with null id, reader must survive
            await c.ws.send(json.dumps({"jsonrpc": "2.0", "id": [1], "method": "ping"}))
            msg = json.loads(await asyncio.wait_for(c.ws.recv(), 5))
            assert msg["error"]["code"] == -32600 and msg["id"] is None
            # id explicitly null -> -32600
            await c.ws.send(json.dumps({"jsonrpc": "2.0", "id": None, "method": "ping"}))
            msg = json.loads(await asyncio.wait_for(c.ws.recv(), 5))
            assert msg["error"]["code"] == -32600
            # connection still fully usable
            assert (await c.exec("'alive'"))["result"] == "alive"

    run(t())


def test_result_always_responds():
    async def t():
        async with Client() as c:
            # NaN result must arrive as valid JSON (repr fallback)
            r = await c.exec("float('nan')")
            assert r["status"] == "ok"
            assert r["result"] == "nan"
            # broken __repr__ must still produce a response
            r = await c.exec(
                "class PtBadRepr:\n    def __repr__(self): raise RuntimeError('boom')\nPtBadRepr()"
            )
            assert r["status"] == "error"
            assert r["ename"] == "RuntimeError" and r["evalue"] == "boom"
            # broken __str__ on a raised exception must still produce an error response
            r = await c.exec(
                "class PtBadStr(Exception):\n    def __str__(self): raise ValueError('nope')\nraise PtBadStr()"
            )
            assert r["status"] == "error"
            assert r["ename"] == "PtBadStr"

    run(t())


def test_cancel_in_same_burst_as_exec():
    async def t():
        async with Client() as c:
            # exec + cancel sent back-to-back with no wait: must resolve as canceled
            # whether the cancel lands before or after the exec task starts
            req = await c.send_request("exec", {"code": "await asyncio.sleep(30)"})
            await c.notify("cancel", {"id": req})
            t0 = time.monotonic()
            r = await c.wait_response(req, timeout=10)
            assert time.monotonic() - t0 < 5
            assert r["status"] == "error" and r["ename"] == "CancelledError"

    run(t())


# ------------------------------------------------------------------- exec core


def test_exec_value_stdout_stderr():
    async def t():
        async with Client() as c:
            r = await c.exec("print('out'); import sys; sys.stderr.write('err'); 40+2")
            assert r["status"] == "ok"
            assert r["result"] == 42
            assert r["stdout"] == "out\n"
            assert r["stderr"] == "err"

    run(t())


def test_last_expression_semantics():
    async def t():
        async with Client() as c:
            assert (await c.exec("pt_x = 5\npt_x * 2"))["result"] == 10
            assert (await c.exec("pt_y = 5"))["result"] is None
            r = await c.exec("import omni.kit.app\nawait omni.kit.app.get_app().next_update_async()\n'done'")
            assert r["result"] == "done"

    run(t())


def test_error_traceback():
    async def t():
        async with Client() as c:
            r = await c.exec("def pt_boom():\n    raise ValueError('pt-test')\npt_boom()")
            assert r["status"] == "error"
            assert r["ename"] == "ValueError"
            assert r["evalue"] == "pt-test"
            assert any("pt_boom" in line for line in r["traceback"])

            r = await c.exec("def broken(:")
            assert r["status"] == "error"
            assert r["ename"] == "SyntaxError"

    run(t())


def test_result_repr_fallback():
    async def t():
        async with Client() as c:
            r = await c.exec("object()")
            assert r["status"] == "ok"
            assert "object object at" in r["result"]

    run(t())


def test_namespace_persists_across_reconnect():
    async def t():
        async with Client() as c:
            await c.exec("pt_persist = 'survived'")
        async with Client() as c:
            assert (await c.exec("pt_persist"))["result"] == "survived"

    run(t())


def test_exec_fifo_across_connections():
    async def t():
        async with Client() as a, Client() as b:
            await a.exec("pt_order = []")
            id_a = await a.send_request(
                "exec", {"code": "pt_order.append('a1')\nawait asyncio.sleep(0.4)\npt_order.append('a2')"}
            )
            await asyncio.sleep(0.1)  # ensure a's exec is running first
            id_b = await b.send_request("exec", {"code": "pt_order.append('b')"})
            await a.wait_response(id_a)
            await b.wait_response(id_b)
            assert (await a.exec("pt_order"))["result"] == ["a1", "a2", "b"]

    run(t())


# ----------------------------------------------------------------- cancellation


def test_cancel_notification():
    async def t():
        async with Client() as c:
            req = await c.send_request(
                "exec", {"code": "pt_c = 'started'\nawait asyncio.sleep(30)\npt_c = 'finished'"}
            )
            await asyncio.sleep(0.3)
            await c.notify("cancel", {"id": req})
            t0 = time.monotonic()
            r = await c.wait_response(req, timeout=10)
            assert time.monotonic() - t0 < 5
            assert r["status"] == "error"
            assert r["ename"] == "CancelledError"
            assert (await c.exec("pt_c"))["result"] == "started"

    run(t())


def test_disconnect_cancels_pending_exec():
    async def t():
        async with Client() as a:
            await a.exec("pt_dc = []")
            await a.send_request(
                "exec",
                {
                    "code": "try:\n    await asyncio.sleep(30)\nexcept asyncio.CancelledError:\n    pt_dc.append('cancelled')\n    raise"
                },
            )
            await asyncio.sleep(0.3)
        # connection dropped with exec pending; FIFO must be free quickly
        async with Client() as b:
            t0 = time.monotonic()
            r = await b.exec("pt_dc")
            assert time.monotonic() - t0 < 5
            assert r["result"] == ["cancelled"]

    run(t())


def test_background_task_pattern():
    async def t():
        async with Client() as c:
            r = await c.exec(
                "pt_task = asyncio.ensure_future(asyncio.sleep(0.5, result='bg-done'))\npt_task.done()"
            )
            assert r["result"] is False
            await asyncio.sleep(0.8)
            assert (await c.exec("pt_task.result()"))["result"] == "bg-done"
            # cancel a long task from a later exec
            await c.exec("pt_task2 = asyncio.ensure_future(asyncio.sleep(60))")
            r = await c.exec("pt_task2.cancel()\nawait asyncio.sleep(0.1)\npt_task2.cancelled()")
            assert r["result"] is True

    run(t())


# ---------------------------------------------------------------------- media


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def test_viewport_image_media():
    async def t():
        async with Client() as c:
            r = await c.exec("agent.image(await agent.viewport(width=160), name='pt-shot')", timeout=90)
            assert r["status"] == "ok", r
            (m,) = r["media"]
            assert m["mimeType"] == "image/png"
            assert m["name"] == "pt-shot"
            w, h = png_size(base64.b64decode(m["data"]))
            assert w == 160

    run(t())


def test_attach_raw_media():
    async def t():
        async with Client() as c:
            r = await c.exec("agent.attach(b'\\x00\\x01payload', 'application/octet-stream', name='blob')")
            (m,) = r["media"]
            assert base64.b64decode(m["data"]) == b"\x00\x01payload"
            assert m["mimeType"] == "application/octet-stream"

    run(t())


def test_media_on_error_result():
    async def t():
        async with Client() as c:
            r = await c.exec("agent.attach(b'partial', 'text/plain')\nraise RuntimeError('after attach')")
            assert r["status"] == "error"
            assert base64.b64decode(r["media"][0]["data"]) == b"partial"

    run(t())


def test_leaked_task_media_sink_closed():
    async def t():
        async with Client() as c:
            await c.exec(
                "pt_leak = []\n"
                "async def pt_leaker():\n"
                "    await asyncio.sleep(0.4)\n"
                "    try:\n"
                "        agent.attach(b'x', 'text/plain')\n"
                "        pt_leak.append('attached')\n"
                "    except RuntimeError:\n"
                "        pt_leak.append('raised')\n"
                "pt_bg = asyncio.ensure_future(pt_leaker())"
            )
            await asyncio.sleep(0.8)
            assert (await c.exec("pt_leak"))["result"] == ["raised"]

    run(t())


# --------------------------------------------------------------- notifications


def test_emit_event_notification():
    async def t():
        async with Client(subscriptions=["event"]) as c:
            await c.exec("agent.emit('pt.event', {'k': 1})")
            p = await c.wait_notification("event")
            assert p["name"] == "pt.event"
            assert p["payload"] == {"k": 1}
            assert "t" in p

    run(t())


def test_event_not_sent_without_subscription():
    async def t():
        async with Client() as c:
            await c.exec("agent.emit('pt.unsub', 1)")
            await c.exec("import time; time.sleep(0.05)")
            assert not [n for n in c.notifications if n["method"] == "event"]

    run(t())


def test_timeline_notifications_and_control():
    async def t():
        async with Client(subscriptions=["timeline"]) as c:
            await c.exec("agent.stop()")
            c.notifications.clear()
            await c.exec("agent.play()")
            p = await c.wait_notification("timeline.changed")
            assert p["playing"] is True
            await c.exec("agent.stop()")
            while p["playing"]:
                p = await c.wait_notification("timeline.changed")
            assert p["playing"] is False
            assert (await c.exec("agent.status()['playing']"))["result"] is False

    run(t())


def test_step_advances_and_pauses():
    async def t():
        async with Client() as c:
            await c.exec("agent.stop()")
            r = await c.exec("await agent.step(12)")
            assert r["status"] == "ok"
            assert abs(r["result"] - 0.2) < 0.05  # 12 steps at 60 Hz
            assert (await c.exec("agent.status()['playing']"))["result"] is False
            await c.exec("agent.stop()")

    run(t())


def test_logs_ring_and_push():
    async def t():
        async with Client(subscriptions=["log"]) as c:
            r = await c.exec(
                "import carb; carb.log_warn('pt-ring-marker'); [e for e in agent.logs(20) if 'pt-ring-marker' in e['message']]"
            )
            assert r["status"] == "ok"
            assert "pt-ring-marker" in r["result"][0]["message"]
            assert r["result"][0]["severity"] == "warning"
            p = await c.wait_notification("log")
            while "pt-ring-marker" not in p["message"]:
                p = await c.wait_notification("log")

    run(t())


# ----------------------------------------------------------------------- state


def test_state_pose():
    async def t():
        async with Client() as c:
            r = await c.exec(
                "def pt_state_probe():\n"
                "    import uuid\n"
                "    from pxr import Usd, UsdGeom\n"
                "    st = omni.usd.get_context().get_stage()\n"
                "    path = '/PtProbe_' + uuid.uuid4().hex\n"
                "    assert not st.GetPrimAtPath(path)\n"
                "    with Usd.EditContext(st, st.GetSessionLayer()):\n"
                "        try:\n"
                "            xf = UsdGeom.Xform.Define(st, path)\n"
                "            UsdGeom.XformCommonAPI(xf).SetTranslate((1.0, 2.0, 3.0))\n"
                "            return agent.state(path)[path], agent.state(path + '/Missing')[path + '/Missing']\n"
                "        finally:\n"
                "            st.RemovePrim(path)\n"
                "pt_state_probe()"
            )
            assert r["status"] == "ok", r
            entry, missing = r["result"]
            assert entry["pose"]["pos"] == [1.0, 2.0, 3.0]
            assert len(entry["pose"]["quat_wxyz"]) == 4
            assert missing is None

    run(t())


def test_status_shape():
    async def t():
        async with Client() as c:
            r = await c.exec("agent.status()")
            s = r["result"]
            for key in ("stagePath", "playing", "simTime", "appUptime"):
                assert key in s

    run(t())


def test_preview_asset():
    async def t():
        async with Client() as c:
            r = await c.exec(
                """async def pt_preview_probe():
    import tempfile
    import omni.replicator.core as rep
    from pxr import Usd, UsdGeom
    st = omni.usd.get_context().get_stage()
    tl = omni.timeline.get_timeline_interface()
    assert not tl.is_playing(), 'preview gate requires a paused/stopped timeline'
    assert rep.orchestrator.get_status() == rep.orchestrator.Status.STOPPED
    settings = carb.settings.get_settings()
    keys = ['/app/asyncRendering', '/rtx/ecoMode/enabled', '/omni/replicator/captureOnPlay']
    before = [settings.get(k) for k in keys] + [tl.is_auto_updating(), tl.get_play_every_frame()]
    def preview_specs():
        return {
            (layer.identifier, spec.name): str(spec.GetAsText())
            for layer in st.GetLayerStack()
            for spec in layer.rootPrims if spec.name.startswith('AgentPreview')
        }
    specs_before = preview_specs()
    with tempfile.TemporaryDirectory(prefix='isaac-agent-preview-test-') as folder:
        s = Usd.Stage.CreateInMemory()
        xf = UsdGeom.Xform.Define(s, '/Thing')
        s.SetDefaultPrim(xf.GetPrim())
        UsdGeom.Sphere.Define(s, '/Thing/Ball').GetRadiusAttr().Set(0.5)
        path = folder + '/asset.usda'
        s.Export(path)
        try:
            meta = await agent.preview_asset(path)
        finally:
            assert preview_specs() == specs_before, 'preview leaked or changed user specs'
            assert rep.orchestrator.get_status() == rep.orchestrator.Status.STOPPED
            after = [settings.get(k) for k in keys] + [tl.is_auto_updating(), tl.get_play_every_frame()]
            assert after == before, (before, after)
    return meta
await pt_preview_probe()""",
                timeout=120,
            )
            assert r["status"] == "ok", r
            meta = r["result"]
            assert meta["defaultPrim"] == "/Thing"
            assert meta["primCounts"].get("Sphere") == 1
            assert meta["bounds"]["size"][0] == 1.0
            assert meta["image"] in ("thumbnail", "rendered")
            (m,) = r["media"]
            assert base64.b64decode(m["data"])[:8] == b"\x89PNG\r\n\x1a\n"

    run(t())
