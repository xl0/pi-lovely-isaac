"""isaac-agent protocol v1 server: WebSocket + JSON-RPC 2.0 on Kit's asyncio loop.

Wire surface: hello, exec, ping requests; cancel notification (client->server);
log / timeline.changed / event notifications (server->client, per hello
subscriptions). One persistent exec namespace, global exec FIFO, cancellation at
await points, disconnect cancels that connection's pending execs.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import http
import io
import json
import os
import secrets
import sys
import threading
import time
import weakref
from contextvars import ContextVar

import carb
import carb.logging  # pyright: ignore[reportMissingImports] - registered at native init, no on-disk module
import carb.settings
import numpy
import omni
import omni.kit.app
import omni.timeline
import omni.usd

from .executor import Executor, format_exc_list
from .helpers import Agent, MediaSink, _media_ctx

PROTOCOL = "isaac-agent"
PROTOCOL_VERSION = 1
AUTH_HEADER = "X-Isaac-Agent-Authorization"
LOCK_DIR = os.path.expanduser("~/.isaac-agent")
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
SUBSCRIPTION_KINDS = ("log", "timeline", "event")
LOG_PUSH_MIN_LEVEL = 0  # warning+; extend hello subscriptions if a client ever needs more
LOG_PUSH_RATE = 30  # notifications/second

_CARB_SEVERITY = {-2: "verbose", -1: "info", 0: "warning", 1: "error", 2: "fatal"}

# marks an exec id whose cancel arrived before its task started running
_CANCELED_BEFORE_START = object()

# (stdout_buf, stderr_buf) for the currently running exec request; task-scoped.
_capture_ctx: ContextVar[tuple[io.StringIO, io.StringIO] | None] = ContextVar(
    "isaac_agent_capture", default=None
)
# Inherited by detached coroutines, but never rebound to a replacement socket.
_connection_ctx: ContextVar[_Connection | None] = ContextVar("isaac_agent_connection", default=None)


class _StreamRouter(io.TextIOBase):
    """Permanent sys.stdout/stderr proxy routing writes to the active exec's
    capture buffer (task-scoped contextvar) or through to the original stream."""

    def __init__(self, original, index: int) -> None:
        self._original = original
        self._index = index

    @property
    def original(self):
        return self._original

    def _target(self):
        bufs = _capture_ctx.get(None)
        if bufs is not None:
            buf = bufs[self._index]
            if not buf.closed:
                return buf
        return self._original

    def write(self, s):
        try:
            return self._target().write(s)
        except ValueError:  # buffer closed between check and write
            return self._original.write(s)

    def flush(self):
        try:
            self._target().flush()
        except ValueError:
            pass

    def writable(self):
        return True

    def isatty(self):
        return False

    def fileno(self):
        return self._original.fileno()

    @property
    def encoding(self):  # type: ignore[override] - typeshed declares a plain str; C getset is not instance-writable
        return getattr(self._original, "encoding", "utf-8")


class _Connection:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.hello_done = False
        self.subscriptions: set[str] = set()
        self.client: dict = {}
        self.pending: dict = {}  # request id -> inner exec Task
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.writer_task: asyncio.Task | None = None
        self.watched_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()

    def send_json(self, obj: dict) -> None:
        self.queue.put_nowait(json.dumps(obj))


class AgentServer:
    def __init__(self, ext_id: str) -> None:
        self.ext_id = ext_id
        self.token = secrets.token_urlsafe(24)  # per launch; the lockfile is the only distribution channel
        self.helper_docs = self._load_helper_docs()
        self.log_ring: collections.deque[dict] = collections.deque(maxlen=1000)
        self.agent = Agent(self)
        self.executor = Executor(self._initial_namespace())

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._exec_lock = asyncio.Lock()
        self._conns: set[_Connection] = set()
        self._ws_server = None
        self.port: int | None = None
        self._lockfile: str | None = None
        self._log_handle = None
        self._tl_sub = None
        self._stdout_router: _StreamRouter | None = None
        self._stderr_router: _StreamRouter | None = None
        # log push flood control
        self._log_window = 0.0
        self._log_sent = 0
        self._log_dropped = 0
        self._tl_last_tick_push = 0.0

    # ---------------------------------------------------------------- startup

    def _initial_namespace(self) -> dict:
        return {
            "__name__": "__isaac_agent__",
            "agent": self.agent,
            "asyncio": asyncio,
            "np": numpy,
            "omni": omni,
            "carb": carb,
        }

    def _load_helper_docs(self) -> str:
        mgr = omni.kit.app.get_app().get_extension_manager()
        root = mgr.get_extension_path(self.ext_id)
        try:
            with open(os.path.join(root, "docs", "HELPERS.md"), encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            carb.log_error(f"[xl0.lovely.isaac] cannot read HELPERS.md: {e}")
            return "helper docs unavailable"

    async def start(self) -> None:
        """Bind first; only advertise (lockfile) after listen succeeds."""
        import websockets

        self._loop = asyncio.get_event_loop()
        self._loop_thread = threading.get_ident()
        try:
            self._ws_server = await websockets.serve(
                self._handler,
                host="127.0.0.1",
                port=0,
                process_request=self._process_request,
                max_size=MAX_MESSAGE_BYTES,
                compression=None,
                ping_interval=None,
            )
        except OSError as e:
            carb.log_error(f"[xl0.lovely.isaac] cannot bind 127.0.0.1: {e} — server disabled")
            return
        self.port = next(iter(self._ws_server.sockets)).getsockname()[1]
        self._install_routers()
        self._install_log_listener()
        self._install_timeline_listener()
        self._write_lockfile()
        carb.log_info(f"[xl0.lovely.isaac] listening on ws://127.0.0.1:{self.port}")

    def stop_sync(self) -> None:
        """Synchronous teardown for extension shutdown/hot-reload."""
        if self._lockfile:
            try:
                os.unlink(self._lockfile)
            except OSError:
                pass
            self._lockfile = None
        if self._log_handle is not None:
            try:
                carb.logging.acquire_logging().remove_logger(self._log_handle)
            except Exception:  # noqa: BLE001
                pass
            self._log_handle = None
        self._tl_sub = None
        self._restore_routers()
        for conn in list(self._conns):
            for task in list(conn.pending.values()):
                if isinstance(task, asyncio.Task):
                    task.cancel()
            if conn.writer_task:
                conn.writer_task.cancel()
            asyncio.ensure_future(conn.ws.close())
        self._conns.clear()
        if self._ws_server is not None:
            self._ws_server.close()
            self._ws_server = None

    def _install_routers(self) -> None:
        if not isinstance(sys.stdout, _StreamRouter):
            self._stdout_router = _StreamRouter(sys.stdout, 0)
            sys.stdout = self._stdout_router
        if not isinstance(sys.stderr, _StreamRouter):
            self._stderr_router = _StreamRouter(sys.stderr, 1)
            sys.stderr = self._stderr_router

    def _restore_routers(self) -> None:
        if self._stdout_router is not None and sys.stdout is self._stdout_router:
            sys.stdout = self._stdout_router.original
        if self._stderr_router is not None and sys.stderr is self._stderr_router:
            sys.stderr = self._stderr_router.original
        self._stdout_router = self._stderr_router = None

    # -------------------------------------------------------------- discovery

    def _write_lockfile(self) -> None:
        os.makedirs(LOCK_DIR, mode=0o700, exist_ok=True)
        self._cleanup_stale_locks()
        settings = carb.settings.get_settings()
        payload = {
            "protocol": PROTOCOL,
            "version": PROTOCOL_VERSION,
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "isaac_version": str(settings.get("/app/version") or "unknown"),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        path = os.path.join(LOCK_DIR, f"{self.port}.lock")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        self._lockfile = path

    def _cleanup_stale_locks(self) -> None:
        try:
            names = os.listdir(LOCK_DIR)
        except OSError:
            return
        for name in names:
            if not name.endswith(".lock"):
                continue
            path = os.path.join(LOCK_DIR, name)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("protocol") != PROTOCOL:
                    continue
                pid = data.get("pid")
                if not isinstance(pid, int):
                    continue
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    os.unlink(path)
                except PermissionError:
                    pass
            except (OSError, ValueError):
                continue

    # ------------------------------------------------------------- WS handling

    async def _process_request(self, path, request_headers):
        if request_headers.get(AUTH_HEADER) != self.token:
            return http.HTTPStatus.UNAUTHORIZED, [], b"invalid or missing token\n"
        return None

    async def _handler(self, ws) -> None:
        import websockets

        conn = _Connection(ws)
        self._conns.add(conn)
        conn.writer_task = asyncio.ensure_future(self._writer(conn))
        try:
            async for raw in ws:
                self._on_message(conn, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._conns.discard(conn)
            for task in list(conn.pending.values()):
                if isinstance(task, asyncio.Task):
                    task.cancel()
            if conn.writer_task:
                conn.writer_task.cancel()

    async def _writer(self, conn: _Connection) -> None:
        import websockets

        try:
            while True:
                msg = await conn.queue.get()
                await conn.ws.send(msg)
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    def _on_message(self, conn: _Connection, raw) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self._error(conn, None, -32700, "parse error")
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            self._error(conn, None, -32600, "invalid request")
            return
        method = msg["method"]
        params = msg.get("params")
        if params is None:
            params = {}
        msg_id = msg.get("id")
        if not isinstance(params, dict):
            if msg_id is not None:
                self._error(
                    conn,
                    msg_id if isinstance(msg_id, (str, int, float)) else None,
                    -32602,
                    "params must be an object",
                )
            return
        if "id" in msg and msg_id is None:
            self._error(conn, None, -32600, "id must not be null")
            return

        if msg_id is None:  # notification
            if method == "cancel":
                self._handle_cancel(conn, params.get("id"))
            return

        if not isinstance(msg_id, (str, int, float)) or isinstance(msg_id, float) and msg_id != msg_id:
            self._error(conn, None, -32600, "id must be a string or number")
            return

        if method == "hello":
            self._handle_hello(conn, msg_id, params)
        elif not conn.hello_done:
            self._error(conn, msg_id, -32002, f"hello required before {method}")
        elif method == "ping":
            self._respond(conn, msg_id, {})
        elif method == "exec":
            code = params.get("code")
            if not isinstance(code, str):
                self._error(conn, msg_id, -32602, "params.code must be a string")
                return
            # pre-register synchronously: a cancel in the same read burst must not
            # be lost before the exec task gets to run (reader never yields between
            # buffered messages)
            conn.pending[msg_id] = None
            asyncio.ensure_future(self._handle_exec(conn, msg_id, code))
        else:
            self._error(conn, msg_id, -32601, f"method not found: {method}")

    def _handle_cancel(self, conn: _Connection, target_id) -> None:
        try:
            entry = conn.pending.get(target_id)
        except TypeError:  # unhashable id
            return
        if isinstance(entry, asyncio.Task):
            entry.cancel()
        elif entry is None and target_id in conn.pending:
            conn.pending[target_id] = _CANCELED_BEFORE_START

    # ----------------------------------------------------------------- methods

    def _handle_hello(self, conn: _Connection, msg_id, params: dict) -> None:
        version = params.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            self._error(
                conn, msg_id, -32001, f"unsupported protocolVersion {version!r} (server: {PROTOCOL_VERSION})"
            )
            return
        conn.client = params.get("client") or {}
        subs = params.get("subscriptions") or []
        conn.subscriptions = {s for s in subs if s in SUBSCRIPTION_KINDS}
        conn.hello_done = True

        settings = carb.settings.get_settings()
        app = omni.kit.app.get_app()
        ext_version = self.ext_id.split("-", 1)[1] if "-" in self.ext_id else "unknown"
        self._respond(
            conn,
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "server": {
                    "isaacVersion": str(settings.get("/app/version") or "unknown"),
                    "kitVersion": app.get_kit_version(),
                    "extensionVersion": ext_version,
                },
                "stage": {"path": omni.usd.get_context().get_stage_url()},
                "helperDocs": self.helper_docs,
            },
        )

    async def _handle_exec(self, conn: _Connection, msg_id, code: str) -> None:
        canceled = {
            "status": "error",
            "ename": "CancelledError",
            "evalue": "execution canceled",
            "traceback": [],
        }
        if conn.pending.get(msg_id) is _CANCELED_BEFORE_START:
            conn.pending.pop(msg_id, None)
            self._respond(conn, msg_id, dict(canceled, stdout=""))
            return
        out_buf, err_buf = io.StringIO(), io.StringIO()
        sink = MediaSink()
        inner = asyncio.ensure_future(self._run_exec(code, out_buf, err_buf, sink, conn))
        conn.pending[msg_id] = inner
        try:
            payload = await inner
        except asyncio.CancelledError:
            payload = canceled
        except BaseException as exc:  # noqa: BLE001 - a response must always be sent
            payload = self._error_payload(exc)
        finally:
            conn.pending.pop(msg_id, None)
            sink.close()

        payload["stdout"] = out_buf.getvalue()
        stderr = err_buf.getvalue()
        if stderr:
            payload["stderr"] = stderr
        out_buf.close()
        err_buf.close()
        if sink.items:
            payload["media"] = [
                {
                    "mimeType": m["mimeType"],
                    "data": base64.b64encode(m["data"]).decode("ascii"),
                    "name": m["name"],
                }
                for m in sink.items
            ]
        self._respond(conn, msg_id, payload)

    async def _run_exec(self, code: str, out_buf, err_buf, sink: MediaSink, conn: _Connection) -> dict:
        _capture_ctx.set((out_buf, err_buf))
        _media_ctx.set(sink)
        _connection_ctx.set(conn)
        async with self._exec_lock:
            try:
                has_value, value = await self.executor.run(code)
            except asyncio.CancelledError:
                raise
            except SystemExit as exc:
                exc = RuntimeError(f"SystemExit({exc.code}) intercepted — it would shut down Isaac Sim")
                return self._error_payload(exc)
            except BaseException as exc:  # noqa: BLE001 - user code may raise anything
                return self._error_payload(exc)
            return {"status": "ok", "result": self._serialize(value) if has_value else None}

    @staticmethod
    def _error_payload(exc: BaseException) -> dict:
        try:
            evalue = str(exc)
        except BaseException:  # noqa: BLE001 - broken __str__
            evalue = f"<unprintable {type(exc).__name__}>"
        return {
            "status": "error",
            "ename": type(exc).__name__,
            "evalue": evalue,
            "traceback": format_exc_list(exc),
        }

    @staticmethod
    def _serialize(value):
        if value is None:
            return None
        try:
            json.dumps(value, allow_nan=False)  # NaN/Infinity are not valid JSON
            return value
        except (TypeError, ValueError):
            return repr(value)

    # ------------------------------------------------------------ notifications

    def watch_task(self, task: asyncio.Task, label: str, *, notify: bool = False) -> asyncio.Task:
        """Observe a native Task once per connection; no scheduling or result registry."""
        if not isinstance(task, asyncio.Task) or task.get_loop() is not asyncio.get_running_loop():
            raise TypeError("watch requires an asyncio.Task on Kit's event loop")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("watch requires a nonempty task label")
        if not isinstance(notify, bool):
            raise TypeError("notify must be a bool")
        conn = _connection_ctx.get()
        if conn not in self._conns or "event" not in conn.subscriptions:
            raise RuntimeError("watch requires an exec from a connected, event-subscribed client")
        if task in conn.watched_tasks:
            raise ValueError("this task is already watched by this connection")
        conn.watched_tasks.add(task)

        def on_done(done: asyncio.Task):
            payload = {"label": label, "status": "cancelled" if done.cancelled() else "ok"}
            if not done.cancelled():
                exc = done.exception()  # Consume the warning, not the Task's stored exception/traceback.
                if exc is not None:
                    payload.update(self._error_payload(exc))
            if conn in self._conns and "event" in conn.subscriptions:
                conn.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "name": "task.done",
                            "payload": payload,
                            "t": time.time(),
                            "notify": notify,
                        },
                    }
                )

        task.add_done_callback(on_done)
        return task

    def _respond(self, conn: _Connection, msg_id, result: dict) -> None:
        conn.send_json({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, conn: _Connection, msg_id, code: int, message: str) -> None:
        conn.send_json({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    def _broadcast(self, kind: str, method: str, params: dict) -> None:
        for conn in list(self._conns):
            if conn.hello_done and kind in conn.subscriptions:
                conn.send_json({"jsonrpc": "2.0", "method": method, "params": params})

    def emit_event(self, name: str, payload) -> None:
        params = {"name": name, "payload": payload, "t": time.time()}
        json.dumps(params, allow_nan=False)
        if threading.get_ident() == self._loop_thread:
            self._broadcast("event", "event", params)
        elif self._loop is not None:
            self._loop.call_soon_threadsafe(self._broadcast, "event", "event", params)

    # ---------------------------------------------------------------- carb log

    def _install_log_listener(self) -> None:
        def on_log(source, level, _filename, _line_number, message):
            severity = _CARB_SEVERITY.get(level)
            if severity is None or level < -1:  # ring keeps info+
                return
            entry = {"t": time.time(), "severity": severity, "source": source, "message": message}
            self.log_ring.append(entry)  # deque append is thread-safe
            if level >= LOG_PUSH_MIN_LEVEL and self._loop is not None:
                self._loop.call_soon_threadsafe(self._push_log, entry)

        self._log_handle = carb.logging.acquire_logging().add_logger(on_log)

    def _push_log(self, entry: dict) -> None:
        now = time.monotonic()
        if now - self._log_window >= 1.0:
            self._log_window = now
            self._log_sent = 0
        if self._log_sent >= LOG_PUSH_RATE:
            self._log_dropped += 1
            return
        self._log_sent += 1
        params = dict(entry)
        if self._log_dropped:
            params["dropped"] = self._log_dropped
            self._log_dropped = 0
        self._broadcast("log", "log", params)

    # ---------------------------------------------------------------- timeline

    def _install_timeline_listener(self) -> None:
        tl = omni.timeline.get_timeline_interface()
        t = omni.timeline.TimelineEventType
        self._tl_state_events = {int(t.PLAY), int(t.PAUSE), int(t.STOP)}
        self._tl_tick_event = int(t.CURRENT_TIME_TICKED)
        self._tl_sub = tl.get_timeline_event_stream().create_subscription_to_pop(self._on_timeline_event)

    def _on_timeline_event(self, e) -> None:
        et = int(e.type)
        now = time.monotonic()
        if et in self._tl_state_events:
            pass
        elif et == self._tl_tick_event and now - self._tl_last_tick_push >= 1.0:
            pass
        else:
            return
        self._tl_last_tick_push = now
        tl = omni.timeline.get_timeline_interface()
        self._broadcast(
            "timeline",
            "timeline.changed",
            {"playing": tl.is_playing(), "simTime": tl.get_current_time()},
        )
