"""Exercise watch routing through the real exec handler, without a running Kit."""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from test_server import helpers  # noqa: F401 - shared isolated Kit fixture


@pytest.fixture
def server_module(helpers, monkeypatch):  # noqa: F811 - imported pytest fixture
    root = Path(__file__).resolve().parents[1] / "exts/xl0.lovely.isaac/xl0/lovely/isaac"
    package = types.ModuleType("isolated_isaac")
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, "isolated_isaac.helpers", helpers)
    logging = types.ModuleType("carb.logging")
    monkeypatch.setitem(sys.modules, "carb.logging", logging)
    monkeypatch.setattr(sys.modules["carb"], "logging", logging, raising=False)
    for name in ("executor", "server"):
        spec = importlib.util.spec_from_file_location(f"isolated_isaac.{name}", root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    monkeypatch.setattr(module.AgentServer, "_load_helper_docs", lambda _: "test helpers")
    return module


def connection(module, server):
    conn = module._Connection(None)
    conn.hello_done = True
    conn.subscriptions = {"event"}
    server._conns.add(conn)
    return conn


async def execute(server, conn, code):
    server._on_message(conn, json.dumps({"id": 1, "method": "exec", "params": {"code": code}}))
    response = json.loads(await asyncio.wait_for(conn.queue.get(), 1))
    assert response["id"] == 1
    return response["result"]


@pytest.mark.parametrize("status", ["ok", "error", "cancelled"])
@pytest.mark.parametrize("notify", [False, True])
def test_watch_terminal_event_is_targeted_and_preserves_task(server_module, status, notify):
    async def t():
        server = server_module.AgentServer("test")
        owner = connection(server_module, server)
        observer = connection(server_module, server)
        result = await execute(
            server,
            owner,
            f"""
gate = asyncio.Event()
async def job():
    await gate.wait()
    if {status!r} == 'error':
        raise ValueError('FEM failed after exec returned')
    return 42
task = agent.watch(asyncio.create_task(job()), 'fin ray', notify={notify!r})
""",
        )
        assert result["status"] == "ok"
        task = server.executor.namespace["task"]
        try:
            duplicate = await execute(server, owner, "agent.watch(task, 'duplicate')")
            assert duplicate["ename"] == "ValueError"
            assert "already watched" in duplicate["evalue"]
            if status == "cancelled":
                task.cancel()
            else:
                server.executor.namespace["gate"].set()
            await asyncio.gather(task, return_exceptions=True)
            event = json.loads(await asyncio.wait_for(owner.queue.get(), 1))
            assert event["method"] == "event"
            assert event["params"]["name"] == "task.done"
            assert event["params"]["notify"] is notify
            payload = event["params"]["payload"]
            assert payload["label"] == "fin ray"
            assert payload["status"] == status
            if status == "error":
                assert "ValueError: FEM failed after exec returned" in "".join(payload["traceback"])
                assert task.exception().__traceback__ is not None
            elif status == "ok":
                assert task.result() == 42
            assert owner.queue.empty() and observer.queue.empty()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(t())


def test_disconnect_does_not_cancel_task_or_replay_completion(server_module):
    async def t():
        server = server_module.AgentServer("test")
        old = connection(server_module, server)
        await execute(
            server,
            old,
            """
gate = asyncio.Event()
task = agent.watch(asyncio.create_task(gate.wait()), 'survives disconnect', notify=True)
""",
        )
        task = server.executor.namespace["task"]
        try:
            server._conns.remove(old)
            replacement = connection(server_module, server)
            assert (await execute(server, replacement, "task.done()"))["result"] is False
            server.executor.namespace["gate"].set()
            await task
            await asyncio.sleep(0)  # Run the done callback.
            assert old.queue.empty() and replacement.queue.empty()
            assert (await execute(server, replacement, "task.result()"))["result"] is True

            # Explicit re-registration on the new connection is allowed, even after completion.
            server._on_message(
                replacement,
                json.dumps(
                    {
                        "id": 2,
                        "method": "exec",
                        "params": {"code": "agent.watch(task, 'rearmed', notify=True); None"},
                    }
                ),
            )
            messages = [json.loads(await asyncio.wait_for(replacement.queue.get(), 1)) for _ in range(2)]
            assert next(m for m in messages if m.get("id") == 2)["result"]["status"] == "ok"
            assert (
                next(m for m in messages if m.get("method") == "event")["params"]["payload"]["label"]
                == "rearmed"
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(t())


def test_watch_rejects_unroutable_registration(server_module):
    async def t():
        server = server_module.AgentServer("test")
        task = asyncio.create_task(asyncio.sleep(0))
        try:
            with pytest.raises(RuntimeError, match="connected"):
                server.agent.watch(task, "outside exec", notify=True)
            owner = connection(server_module, server)
            owner.subscriptions.clear()
            server.executor.namespace["task"] = task
            result = await execute(server, owner, "agent.watch(task, 'no events')")
            assert result["ename"] == "RuntimeError"
            owner.subscriptions.add("event")
            for code, ename in [
                ("agent.watch(None, 'bad task')", "TypeError"),
                ("agent.watch(task, '')", "ValueError"),
                ("agent.watch(task, 'label', notify='false')", "TypeError"),
            ]:
                assert (await execute(server, owner, code))["ename"] == ename
        finally:
            await task

    asyncio.run(t())
