"""Protocol gate for the isaac-agent server. Needs a running Isaac Sim with
xl0.lovely.isaac enabled (lockfile in ~/.isaac-agent). Run: pytest tests/ -v

Tests share the live server; each uses uniquely named namespace vars.
"""

import asyncio
import base64
import glob
import json
import os
import struct
import time

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
            assert r["status"] == "ok"
            assert "unrepresentable" in r["result"]
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
                "import omni.usd\nfrom pxr import UsdGeom\n"
                "st = omni.usd.get_context().get_stage()\n"
                "UsdGeom.Xform.Define(st, '/World/PtProbe')\n"
                "UsdGeom.XformCommonAPI(st.GetPrimAtPath('/World/PtProbe')).SetTranslate((1.0, 2.0, 3.0))\n"
                "agent.state('/World/PtProbe')"
            )
            entry = r["result"]["/World/PtProbe"]
            assert entry["pose"]["pos"] == [1.0, 2.0, 3.0]
            assert len(entry["pose"]["quat_wxyz"]) == 4
            r = await c.exec("agent.state('/World/DoesNotExist')")
            assert r["result"]["/World/DoesNotExist"] is None

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
                "from pxr import Usd, UsdGeom\n"
                "s = Usd.Stage.CreateInMemory()\n"
                "xf = UsdGeom.Xform.Define(s, '/Thing')\n"
                "s.SetDefaultPrim(xf.GetPrim())\n"
                "UsdGeom.Sphere.Define(s, '/Thing/Ball').GetRadiusAttr().Set(0.5)\n"
                "s.Export('/tmp/pt_preview_asset.usda')\n"
                "'written'"
            )
            assert r["status"] == "ok", r
            r = await c.exec("await agent.preview_asset('/tmp/pt_preview_asset.usda')", timeout=120)
            assert r["status"] == "ok", r
            meta = r["result"]
            assert meta["defaultPrim"] == "/Thing"
            assert meta["primCounts"].get("Sphere") == 1
            assert meta["bounds"]["size"][0] == 1.0
            assert meta["image"] in ("thumbnail", "rendered")
            (m,) = r["media"]
            assert base64.b64decode(m["data"])[:8] == b"\x89PNG\r\n\x1a\n"
            # session layer left clean
            r = await c.exec(
                "import omni.usd; bool(omni.usd.get_context().get_stage().GetPrimAtPath('/AgentPreview'))"
            )
            assert r["result"] is False

    run(t())
