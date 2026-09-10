"""MCP stdio adapter for the isaac-agent protocol.

Discovers a running Isaac Sim via ~/.isaac-agent/*.lock, dials its WebSocket,
and exposes `isaac_exec` / `isaac_events` MCP tools. The adapter owns timeout
policy: it cancels the exec on its own timeout or on host-side abort.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import glob
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import mcp.types as types
import websockets
from mcp.server import Server
from mcp.server.stdio import stdio_server

AUTH_HEADER = "X-Isaac-Agent-Authorization"
LOCK_GLOB = os.path.expanduser("~/.isaac-agent/*.lock")
DEFAULT_TIMEOUT_S = float(os.environ.get("ISAAC_AGENT_TIMEOUT_S", "120"))

EXEC_INTRO = """Execute Python inside the running Isaac Sim (persistent namespace, \
top-level await, notebook-style last-expression result). Attached images land in \
this tool result. On timeout ({timeout:.0f}s default, override per call) the exec is \
canceled at its next await point.

Supply exactly one of code or path. Files are read by the adapter as UTF-8;
relative paths use the adapter's working directory. File contents execute in the
shared namespace, without setting __main__/__file__ or changing cwd/sys.path.
Text over 2000 lines or 50 KiB is previewed with a full-output file path.

"""

DOCS_FALLBACK = """(Isaac Sim is not reachable right now, so the live helper docs are
unavailable — the tool will connect on demand. Inside exec'd code, an `agent` helper
object provides viewport/screenshot capture, media attach, timeline control, state
queries, logs, and events; call `print(agent.docs())` once connected for the docs.)"""


class IsaacUnavailable(Exception):
    pass


class IsaacConnection:
    """One persistent WS connection + notification buffer; redials on demand."""

    def __init__(self) -> None:
        # connection once dialed; Any: asserting non-None at every use isn't worth it
        self.ws: Any = None
        self.helper_docs: str | None = None
        self.server_info: dict = {}
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.notifications: collections.deque = collections.deque(maxlen=500)
        self.dropped = 0
        self._recv_task: asyncio.Task | None = None
        self._connect_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------- lifecycle

    @staticmethod
    def _locks() -> list[dict]:
        locks = []
        for path in glob.glob(LOCK_GLOB):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("protocol") != "isaac-agent":
                    continue
                try:
                    os.kill(int(data["pid"]), 0)
                except (ProcessLookupError, ValueError, KeyError, TypeError):
                    continue
                except PermissionError:
                    pass
                locks.append(data)
            except (OSError, ValueError):
                continue
        return sorted(locks, key=lambda d: d.get("started_at", ""), reverse=True)

    async def ensure(self) -> None:
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:  # concurrent tool calls must not double-dial
            await self._ensure_locked()

    async def _ensure_locked(self) -> None:
        if self.ws is not None:
            return
        errors = []
        for lock in self._locks():
            try:
                self.ws = await websockets.connect(
                    f"ws://127.0.0.1:{lock['port']}",
                    additional_headers={AUTH_HEADER: lock["token"]},
                    max_size=2**26,
                    open_timeout=5,
                )
                hello = await self._call(
                    "hello",
                    {
                        "protocolVersion": 1,
                        "client": {"name": "isaac-agent-mcp", "version": "0.1.0", "pid": os.getpid()},
                        "subscriptions": ["log", "timeline", "event"],
                    },
                    timeout=10,
                )
                self.helper_docs = hello["helperDocs"]
                self.server_info = hello.get("server", {})
                self._recv_task = asyncio.ensure_future(self._recv_loop())
                return
            except Exception as e:  # noqa: BLE001 - stale lock / dead server: try next
                if self.ws is not None:
                    try:
                        await self.ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.ws = None
                errors.append(f"port {lock.get('port')}: {type(e).__name__}: {e}")
        detail = "\n".join(errors) if errors else "no live lockfile in ~/.isaac-agent"
        raise IsaacUnavailable(
            f"cannot reach Isaac Sim — is it running with the xl0.lovely.isaac extension?\n{detail}"
        )

    def _drop_connection(self) -> None:
        self.ws = None
        if self._recv_task is not None:
            self._recv_task.cancel()
            self._recv_task = None
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(IsaacUnavailable("connection to Isaac Sim lost"))
        self.pending.clear()

    async def _recv_loop(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self.pending:
                    fut = self.pending.pop(msg["id"])
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(f"rpc error {msg['error']['code']}: {msg['error']['message']}")
                            )
                        else:
                            fut.set_result(msg["result"])
                elif "method" in msg:
                    if len(self.notifications) == self.notifications.maxlen:
                        self.dropped += 1
                    self.notifications.append((time.time(), msg["method"], msg.get("params") or {}))
        except (websockets.ConnectionClosed, asyncio.CancelledError, OSError):
            pass
        finally:
            self._drop_connection()

    # ------------------------------------------------------------------- rpc

    async def _call(self, method: str, params: dict, timeout: float | None) -> dict:
        self.next_id += 1
        rpc_id = self.next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[rpc_id] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}))
        if self._recv_task is None:  # during hello the recv loop is not running yet
            while not fut.done():
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
                if msg.get("id") == rpc_id:
                    if "error" in msg:
                        raise RuntimeError(f"rpc error {msg['error']['code']}: {msg['error']['message']}")
                    return msg["result"]
            return fut.result()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rpc_id, None)

    async def exec(self, code: str, timeout_s: float) -> dict:
        await self.ensure()
        self.next_id += 1
        rpc_id = self.next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[rpc_id] = fut
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": "exec", "params": {"code": code}})
        )
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout_s)
        except TimeoutError:
            await self._cancel_and_wait(rpc_id, fut)
            return (
                fut.result()
                if fut.done() and fut.exception() is None
                else {
                    "status": "error",
                    "ename": "CancelledError",
                    "evalue": f"adapter timeout after {timeout_s:.0f}s",
                    "traceback": [],
                    "stdout": "",
                }
            )
        except asyncio.CancelledError:
            # host aborted the tool call: cancel the exec, then re-raise
            await asyncio.shield(self._cancel_and_wait(rpc_id, fut))
            raise
        finally:
            self.pending.pop(rpc_id, None)

    async def _cancel_and_wait(self, rpc_id: int, fut: asyncio.Future) -> None:
        try:
            await self.ws.send(json.dumps({"jsonrpc": "2.0", "method": "cancel", "params": {"id": rpc_id}}))
            await asyncio.wait_for(asyncio.shield(fut), 10)
        except Exception:  # noqa: BLE001 - best effort; exec result no longer deliverable
            pass


CONN = IsaacConnection()

# ------------------------------------------------------------------ rendering

MEDIA_DIR = os.path.join(tempfile.gettempdir(), "isaac-agent-media")


def format_notification(ts: float, method: str, params: dict) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(ts))
    if method == "log":
        return f"[{t}] {params.get('severity')} [{params.get('source')}] {params.get('message')}"
    if method == "timeline.changed":
        state = "playing" if params.get("playing") else "stopped"
        return f"[{t}] timeline {state} simTime={params.get('simTime', 0):.3f}"
    if method == "event":
        return f"[{t}] event {params.get('name')} {json.dumps(params.get('payload'))}"
    return f"[{t}] {method} {json.dumps(params)}"


def text_block(text: str) -> types.TextContent:
    data = text.encode("utf-8")
    if len(data) > 50 * 1024 or len(text.splitlines()) > 2000:
        fd, path = tempfile.mkstemp(prefix="isaac-agent-output-", suffix=".txt")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        preview = "".join(text.splitlines(keepends=True)[:2000])
        preview = preview.encode("utf-8")[: 50 * 1024].decode("utf-8", errors="ignore")
        text = f"{preview}\n[Output truncated. Full output saved to {path}]"
    return types.TextContent(type="text", text=text)


def render_exec_result(result: dict) -> list[types.TextContent | types.ImageContent]:
    out: list[types.TextContent | types.ImageContent] = []
    text_parts = []
    if result.get("stdout"):
        text_parts.append(result["stdout"].rstrip("\n"))
    if result.get("stderr"):
        text_parts.append("stderr:\n" + result["stderr"].rstrip("\n"))
    if result.get("status") == "ok":
        if result.get("result") is not None:
            value = result["result"]
            text_parts.append("result: " + (value if isinstance(value, str) else json.dumps(value, indent=2)))
        elif not text_parts:
            text_parts.append("ok (no output)")
    else:
        text_parts.append(
            "".join(result.get("traceback") or []) or f"{result.get('ename')}: {result.get('evalue')}"
        )
    for m in result.get("media") or []:
        if m["mimeType"].startswith("image/"):
            out.append(types.ImageContent(type="image", data=m["data"], mimeType=m["mimeType"]))
        else:
            os.makedirs(MEDIA_DIR, exist_ok=True)
            suffix = {"application/json": ".json", "text/plain": ".txt"}.get(m["mimeType"], ".bin")
            prefix = re.sub(r"[^\w.-]+", "_", m.get("name") or "media") + "-"
            fd, path = tempfile.mkstemp(dir=MEDIA_DIR, prefix=prefix, suffix=suffix)
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(m["data"]))
            text_parts.append(f"media {m.get('name')!r} ({m['mimeType']}) saved to {path}")
    pending = len(CONN.notifications)
    if pending:
        text_parts.append(
            f"({pending} notification{'s' if pending != 1 else ''} buffered — call isaac_events)"
        )
    out.insert(0, text_block("\n".join(text_parts)))
    return out


# ---------------------------------------------------------------------- server

app: Server = Server("isaac-agent")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    if CONN.helper_docs is None:
        try:
            await CONN.ensure()
        except IsaacUnavailable:
            pass
    docs = CONN.helper_docs or DOCS_FALLBACK
    return [
        types.Tool(
            name="isaac_exec",
            description=EXEC_INTRO.format(timeout=DEFAULT_TIMEOUT_S) + docs,
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to execute in Isaac Sim"},
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "UTF-8 script, relative to adapter cwd; mutually exclusive with code",
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": (
                            f"Cancel the exec after this many seconds (default {DEFAULT_TIMEOUT_S:.0f})"
                        ),
                    },
                },
                "oneOf": [{"required": ["code"]}, {"required": ["path"]}],
            },
        ),
        types.Tool(
            name="isaac_events",
            description=(
                "Read and consume buffered Isaac Sim notifications, oldest first (carb log "
                "warnings/errors, timeline changes, agent.emit events). Unreturned entries "
                "stay buffered unless flush=true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "max": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Max entries to consume (default 100, oldest first)",
                    },
                    "flush": {
                        "type": "boolean",
                        "description": "Discard remaining buffered entries after this read (default false)",
                    },
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> types.CallToolResult | list[types.TextContent | types.ImageContent]:
    if name == "isaac_exec":
        if ("code" in arguments) == ("path" in arguments):
            raise ValueError("Supply exactly one of code or path")
        code = (
            arguments["code"] if "code" in arguments else Path(arguments["path"]).read_text(encoding="utf-8")
        )
        timeout_s = float(arguments.get("timeout_s") or DEFAULT_TIMEOUT_S)
        result = await CONN.exec(code, timeout_s)
        return types.CallToolResult(content=render_exec_result(result), isError=result["status"] == "error")

    if name == "isaac_events":
        limit = arguments.get("max", 100)
        drained = [CONN.notifications.popleft() for _ in range(min(limit, len(CONN.notifications)))]
        flushed = 0
        if arguments.get("flush", False):
            flushed = len(CONN.notifications)
            CONN.notifications.clear()
        dropped, CONN.dropped = CONN.dropped, 0
        lines = [format_notification(*n) for n in drained]
        header = []
        if dropped:
            header.append(f"({dropped} older notifications dropped)")
        if flushed:
            header.append(f"({flushed} remaining notifications flushed)")
        if CONN.notifications:
            header.append(f"({len(CONN.notifications)} notifications remain buffered)")
        text = "\n".join(header + lines) if (header or lines) else "no buffered notifications"
        return [text_block(text)]

    raise ValueError(f"unknown tool {name}")


def main() -> None:
    async def run() -> None:
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    asyncio.run(run())
