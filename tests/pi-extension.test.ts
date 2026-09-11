import { afterAll, afterEach, expect, mock, test } from "bun:test";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import * as os from "node:os";
import { dirname, join } from "node:path";
import { stripVTControlCharacters } from "node:util";
import { initTheme, type ExtensionAPI, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { visibleWidth } from "@earendil-works/pi-tui";

const home = mkdtempSync(join(os.tmpdir(), "isaac-pi-test-"));
mkdirSync(join(home, ".isaac-agent"));
writeFileSync(join(home, ".isaac-agent", "1234.lock"), JSON.stringify({
  protocol: "isaac-agent", version: 1, pid: process.pid, port: 1234, token: "test",
}));
mock.module("node:os", () => ({ ...os, homedir: () => home, tmpdir: () => home }));
const requests: any[] = [];
const sockets: Socket[] = [];
let autoOpen = true;
let autoReply = true;
let reply: any = { status: "ok", result: 42, stdout: "" };
class Socket extends EventTarget {
  static OPEN = 1;
  readyState = Socket.OPEN;
  closed = false;
  constructor() {
    super();
    sockets.push(this);
    if (autoOpen) queueMicrotask(() => this.dispatchEvent(new Event("open")));
  }
  send(raw: string) {
    const request = JSON.parse(raw);
    requests.push(request);
    const result = request.method === "hello" ? { helperDocs: "test helpers", server: {} } : reply;
    if (autoReply) queueMicrotask(() => this.dispatchEvent(new MessageEvent("message", {
      data: JSON.stringify({ id: request.id, result }),
    })));
  }
  close() { this.closed = true; this.readyState = 3; this.dispatchEvent(new Event("close")); }
}
mock.module("undici", () => ({ WebSocket: Socket }));
const { default: lovelyIsaac } = await import("../extensions/lovely-isaac/index.ts");
initTheme("dark");
const theme: any = {
  fg: (_: string, text: string) => `\x1b[36m${text}\x1b[39m`,
  bold: (text: string) => `\x1b[1m${text}\x1b[22m`,
};
const shutdowns: (() => void)[] = [];
function setup() {
  const tools = new Map<string, ToolDefinition<any, any>>();
  const hooks = new Map<string, (...args: any[]) => any>();
  const messages: any[] = [];
  lovelyIsaac({
    registerTool: (t: ToolDefinition<any, any>) => tools.set(t.name, t),
    registerCommand() {},
    sendMessage: (message: any, options: any) => messages.push({ message, options }),
    on: (name: string, fn: (...args: any[]) => any) => hooks.set(name, fn),
  } as unknown as ExtensionAPI);
  shutdowns.push(() => hooks.get("session_shutdown")!({}, { hasUI: false }));
  const exec = tools.get("isaac_exec")!;
  return { exec, hooks, tools, messages };
}
const plain = (lines: string[]) => lines.map((s) => stripVTControlCharacters(s).trimEnd()).join("\n");
afterEach(async () => {
  for (const stop of shutdowns.splice(0)) stop();
  await new Promise(setImmediate); // Settle detached RPC failures before resetting the fake transport.
  requests.length = 0;
  sockets.length = 0;
  autoOpen = autoReply = true;
  reply = { status: "ok", result: 42, stdout: "" };
});
afterAll(() => { rmSync(home, { recursive: true, force: true }); mock.restore(); });

test("file exec reads relative to session cwd and renders the submitted snapshot", async () => {
  const { exec } = setup();
  const code = "x = 21\nawait asyncio.sleep(0)\nx * 2";
  writeFileSync(join(home, "experiment.py"), code);
  const updates: any[] = [];
  const result = await exec.execute("file", { path: "experiment.py" }, undefined,
    (update) => updates.push(update), { cwd: home } as any);
  expect(requests.find((r) => r.method === "exec").params).toEqual({ code });
  expect(updates[0].details.source).toEqual({ path: join(home, "experiment.py"), code });
  writeFileSync(join(home, "experiment.py"), "different source");
  const context: any = { state: {}, expanded: true };
  const call = exec.renderCall!({ path: "experiment.py" }, theme, context);
  exec.renderResult!(result, { expanded: true, isPartial: false }, theme, context);
  expect(plain(call.render(100))).toContain(code);
  expect(plain(call.render(100))).not.toContain("different source");
  expect(result.details.status).toBe("ok");
});

test("ambiguous inputs, missing files and pre-aborts never submit exec", async () => {
  const { exec } = setup();
  for (const args of [{}, { code: "1", path: "x.py" }]) {
    await expect(exec.execute("bad", args, undefined, undefined, { cwd: home } as any))
      .rejects.toThrow("exactly one");
  }
  await expect(exec.execute("missing", { path: "missing.py" }, undefined, undefined, { cwd: home } as any))
    .rejects.toThrow("ENOENT");
  await expect(exec.execute("abort", { code: "1" }, AbortSignal.abort(), undefined, { cwd: home } as any))
    .rejects.toThrow();
  expect(requests).toHaveLength(0);
  for (const waitMs of [-1, 1.5, Infinity, 600001]) {
    await expect(exec.execute("bad wait", { code: "1", waitMs }, undefined, undefined, { cwd: home } as any))
      .rejects.toThrow("waitMs");
  }
  expect(requests).toHaveLength(0);
});

test("bounded exec returns an ID, keeps one RPC alive, and preserves output and source", async () => {
  const { exec, hooks, tools, messages } = setup();
  await hooks.get("session_start")!({}, { hasUI: false });
  autoReply = false;
  const code = "await experiment()\n42";
  writeFileSync(join(home, "async.py"), code);
  const started = await exec.execute("slow", { path: "async.py", waitMs: 5, label: "experiment" },
    undefined, undefined, { cwd: home } as any);
  expect(started.details.status).toBe("pending");
  const id = started.details.id;
  expect(id).toMatch(/^i_[0-9a-f]{8}$/);
  const output = tools.get("isaac_result")!;
  const read = (args: any = {}) => output.execute("read", { id, ...args }, undefined, undefined, {} as any);
  expect((await read()).details.status).toBe("pending");
  const waiting = read({ waitMs: 1000 });
  writeFileSync(join(home, "async.py"), "different source");
  const rpc = requests.find((r) => r.method === "exec");
  sockets[0].dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ id: rpc.id, result: {
      status: "error", ename: "ValueError", evalue: "boom", stdout: "partial stdout",
      media: [{ mimeType: "image/png", data: "cG5n" }],
    } }),
  }));
  const result = await waiting;
  expect(result.details).toMatchObject({ id, status: "error", source: { code, path: join(home, "async.py") } });
  expect(result.content[1]).toEqual({ type: "image", mimeType: "image/png", data: "cG5n" });
  expect((result.content[0] as any).text).toContain("partial stdout");
  expect(readFileSync(result.details.outputPath, "utf8")).toContain("cG5n");
  expect((await read()).content).toEqual(result.content);
  expect(requests.filter((r) => r.method === "exec")).toHaveLength(1);
  expect(messages).toHaveLength(1);
  expect(messages[0].message.content).toContain(id);
  expect(messages[0].options).toEqual({ triggerTurn: true, deliverAs: "steer" });
  expect(hooks.get("tool_result")!({ toolName: "isaac_result", details: result.details })).toEqual({ isError: true });
});

test("an archive failure wakes waiters with a terminal error and is not retried", async () => {
  const before = new Set(readdirSync(home));
  const { exec, hooks, tools, messages } = setup();
  await hooks.get("session_start")!({}, { hasUI: false });
  autoReply = false;
  const start = await exec.execute("slow", { code: "42", waitMs: 0 }, undefined, undefined, { cwd: home } as any);
  const dir = join(home, readdirSync(home).find((name) => name.startsWith("isaac-agent-runs-") && !before.has(name))!);
  rmSync(dir, { recursive: true });
  const output = tools.get("isaac_result")!;
  const waiting = output.execute("wait", { id: start.details.id, waitMs: 10000 }, undefined, undefined, {} as any);
  const rpc = requests.find((r) => r.method === "exec");
  sockets[0].dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ id: rpc.id, result: { status: "ok", result: 42, stdout: "preserve me" } }),
  }));
  await expect(waiting).rejects.toThrow("Could not save Isaac exec");
  expect(messages[0].message.content).toContain("Output is unavailable");
  expect(messages[0].message.content).toContain("Execution returned ok");
  expect(messages[0].message.content).toContain("Do not automatically resubmit");
  mkdirSync(dir);
  await expect(output.execute("read again", { id: start.details.id }, undefined, undefined, {} as any))
    .rejects.toThrow("ENOENT");
  expect(readdirSync(dir)).toEqual([]);
});

test("retrieval abort does not cancel execution; explicit cancel is bounded and suppresses wakeup", async () => {
  const { exec, hooks, tools, messages } = setup();
  await hooks.get("session_start")!({}, { hasUI: false });
  autoReply = false;
  const start = await exec.execute("slow", { code: "await long_job()", waitMs: 0 },
    undefined, undefined, { cwd: home } as any);
  const id = start.details.id;
  const output = tools.get("isaac_result")!;
  const controller = new AbortController();
  const waiting = output.execute("wait", { id, waitMs: 10000 }, controller.signal, undefined, {} as any);
  controller.abort();
  await expect(waiting).rejects.toThrow();
  expect(requests.filter((r) => r.method === "cancel")).toHaveLength(0);
  const cancel = await output.execute("cancel", { id, cancel: true }, undefined, undefined, {} as any);
  expect(cancel.details).toMatchObject({ status: "pending", cancelRequested: true });
  await output.execute("cancel again", { id, cancel: true }, undefined, undefined, {} as any);
  const rpc = requests.find((r) => r.method === "exec");
  expect(requests.filter((r) => r.method === "cancel")).toEqual([
    { jsonrpc: "2.0", method: "cancel", params: { id: rpc.id } },
  ]);
  sockets[0].dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ id: rpc.id, result: { status: "error", ename: "CancelledError", stdout: "" } }),
  }));
  await new Promise(setImmediate);
  expect((await output.execute("read", { id }, undefined, undefined, {} as any)).details.ename).toBe("CancelledError");
  expect(messages).toHaveLength(0);
});

test("initial exec abort returns the run ID without waiting indefinitely for cancellation", async () => {
  const { exec, hooks, messages } = setup();
  await hooks.get("session_start")!({}, { hasUI: false });
  autoReply = false;
  const controller = new AbortController();
  let id: string | undefined;
  const started = exec.execute("abort", { code: "await long_job()", waitMs: 10000 },
    controller.signal, (update) => {
      id = update.details.id;
      if (id) controller.abort(); // Abort before waitRun installs its listener.
    }, { cwd: home } as any);
  const result = await started;
  expect(result.details).toMatchObject({ id, status: "pending", cancelRequested: true });
  expect(requests.filter((r) => r.method === "cancel")).toHaveLength(1);
  expect(messages).toHaveLength(0);
});

test("connection loss is unknown, never retried, and old run IDs cannot cancel new requests", async () => {
  const { exec, hooks, tools, messages } = setup();
  await hooks.get("session_start")!({}, { hasUI: false });
  autoReply = false;
  const start = await exec.execute("old", { code: "await work()", waitMs: 0 }, undefined, undefined, { cwd: home } as any);
  sockets[0].close();
  await new Promise(setImmediate);
  const output = tools.get("isaac_result")!;
  const lost = await output.execute("lost", { id: start.details.id }, undefined, undefined, {} as any);
  expect(lost.details.status).toBe("lost");
  expect((lost.content[0] as any).text).toContain("outcome is unknown");
  expect(messages).toHaveLength(1);
  autoReply = true;
  await exec.execute("new", { code: "2" }, undefined, undefined, { cwd: home } as any);
  await output.execute("old cancel", { id: start.details.id, cancel: true }, undefined, undefined, {} as any);
  expect(requests.filter((r) => r.method === "cancel")).toHaveLength(0);
  expect(requests.filter((r) => r.method === "exec")).toHaveLength(2);
  const other = setup();
  await expect(other.tools.get("isaac_result")!.execute("foreign", { id: start.details.id }, undefined, undefined, {} as any))
    .rejects.toThrow("Unknown Isaac run");
});

test.each(["quiet", "shutdown"])("detached %s runs never wake the agent", async (mode) => {
  const { exec, hooks, tools, messages } = setup();
  await hooks.get("session_start")!({}, { hasUI: false });
  autoReply = false;
  const start = await exec.execute("slow", { code: "await work()", waitMs: 0, notify: mode !== "quiet" },
    undefined, undefined, { cwd: home } as any);
  const rpc = requests.find((r) => r.method === "exec");
  if (mode === "shutdown") hooks.get("session_shutdown")!({}, { hasUI: false });
  sockets[0].dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ id: rpc.id, result: { status: "ok", result: 42, stdout: "" } }),
  }));
  await new Promise(setImmediate);
  if (mode === "quiet") {
    expect((await tools.get("isaac_result")!.execute("read", { id: start.details.id }, undefined, undefined, {} as any)).details.status).toBe("ok");
  }
  expect(messages).toHaveLength(0);
});

test("large Unicode output is saved intact and Python failure retains partial media", async () => {
  const { exec, hooks } = setup();
  const stdout = "中文🚀\n".repeat(12_000);
  reply = { status: "error", ename: "ValueError", evalue: "boom", stdout,
    media: [{ mimeType: "image/png", data: "cG5n", name: "partial" }] };
  const result = await exec.execute("error", { code: "raise ValueError('boom')" },
    undefined, undefined, { cwd: home } as any);
  const text = (result.content[0] as any).text;
  expect(Buffer.byteLength(text)).toBeLessThan(52_000);
  const path = text.match(/Full output saved to (.+)\]/)[1];
  expect(readFileSync(path, "utf8")).toBe(stdout.trimEnd() + "\n\nValueError: boom");
  rmSync(dirname(path), { recursive: true });
  expect(result.content[1]).toEqual({ type: "image", data: "cG5n", mimeType: "image/png" });
  expect(hooks.get("tool_result")!({ toolName: "isaac_exec", details: result.details }))
    .toEqual({ isError: true });
  const context: any = { state: {}, expanded: false };
  const call = exec.renderCall!({ code: "raise ValueError('boom')" }, theme, context);
  exec.renderResult!(result, { expanded: false, isPartial: false }, theme, context);
  expect(plain(call.render(100))).toContain("✗ ValueError");
});

test("collapsed headers handle partial arguments, Unicode and narrow terminals", () => {
  const { exec } = setup();
  for (const args of [{}, { code: "" }, { code: "# 中文🚀\n".repeat(100) }, { path: "long/path.py" }]) {
    for (const width of [1, 8, 20, 80]) {
      const lines = exec.renderCall!(args, theme, { state: {}, expanded: false } as any).render(width);
      expect(lines).toHaveLength(1);
      expect(visibleWidth(lines[0])).toBeLessThanOrEqual(width);
      expect(lines[0]).not.toContain("\x1b[0m");
    }
  }
});

test("event reads preserve the remainder unless flush is explicit", async () => {
  const { hooks, tools } = setup();
  await hooks.get("session_start")!({}, { hasUI: false });
  const events = tools.get("isaac_events")!;
  const emit = (count: number) => {
    for (let i = 0; i < count; i++) {
      sockets[0].dispatchEvent(new MessageEvent("message", {
        data: JSON.stringify({ method: "event", params: { name: `entry_${i}`, payload: i } }),
      }));
    }
  };
  const read = (args: any = {}) => events.execute("events", args, undefined, undefined, {} as any);
  emit(5);
  const first = await read({ max: 2 });
  expect(first.details).toEqual({ count: 2, remaining: 3, flushed: 0 });
  expect((first.content[0] as any).text).toContain("entry_0");
  expect((first.content[0] as any).text).toContain("entry_1");
  expect((first.content[0] as any).text).not.toContain("entry_2");
  const next = await read({ max: 1, flush: false });
  expect((next.content[0] as any).text).toContain("entry_2");
  expect(next.details.remaining).toBe(2);
  const flushed = await read({ max: 1, flush: true });
  expect((flushed.content[0] as any).text).toContain("entry_3");
  expect((flushed.content[0] as any).text).not.toContain("entry_4");
  expect(flushed.details).toEqual({ count: 1, remaining: 0, flushed: 1 });
  expect((await read()).details.count).toBe(0);

  emit(501); // Capacity eviction is distinct from explicit flushing.
  const overflow = await read({ max: 1 });
  expect((overflow.content[0] as any).text).toContain("1 older notifications dropped");
  expect((overflow.content[0] as any).text).toContain("entry_1 ");
  expect(overflow.details.remaining).toBe(499);
  const defaultRead = await read();
  expect(defaultRead.details).toEqual({ count: 100, remaining: 399, flushed: 0 });
  expect((defaultRead.content[0] as any).text).not.toContain("older notifications dropped");
  expect((await read({ max: 1, flush: true })).details.flushed).toBe(398);
});

test("only opted-in terminal events wake the connected session; tracebacks are preserved", async () => {
  const owner = setup();
  const other = setup();
  await owner.hooks.get("session_start")!({}, { hasUI: false });
  await other.hooks.get("session_start")!({}, { hasUI: false });
  const socket = sockets[0];
  const emit = (name: string, payload: any, notify: boolean) => socket.dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ method: "event", params: { name, payload, notify } }),
  }));
  emit("progress", { label: "batch", status: "ok" }, true);
  emit("task.done", { label: "quiet", status: "ok" }, false);
  emit("task.done", { label: "not terminal", status: "running" }, true);
  expect(owner.messages).toHaveLength(0);
  for (const status of ["ok", "error", "cancelled"]) {
    emit("task.done", { label: "contact/release", status, traceback: status === "error" ? ["ValueError: boom\n"] : [] }, true);
  }
  expect(owner.messages).toHaveLength(3);
  expect(owner.messages[1].message.content[0].text).toContain("ValueError: boom");
  expect(owner.messages[0].options).toEqual({ triggerTurn: true, deliverAs: "steer" });
  expect(other.messages).toHaveLength(0);
  const result = await owner.tools.get("isaac_events")!.execute("events", {}, undefined, undefined, {} as any);
  expect(result.details.count).toBe(6); // Waking does not drain the event buffer.

  const traceback = "Frame 中文🚀\n".repeat(10_000);
  emit("task.done", { label: "large failure", status: "error", traceback: [traceback] }, true);
  const text = owner.messages[3].message.content[0].text;
  const path = text.match(/Full output saved to (.+)\]/)[1];
  expect(readFileSync(path, "utf8")).toContain(traceback.trimEnd());
  rmSync(dirname(path), { recursive: true });

  socket.close();
  await owner.exec.execute("reconnect", { code: "1" }, undefined, undefined, { cwd: home } as any);
  emit("task.done", { label: "old socket", status: "ok" }, true);
  expect(owner.messages).toHaveLength(4);
});

test.each(["open", "hello", "connected"])("shutdown fences late %s socket callbacks and stale contexts", async (phase) => {
  autoOpen = phase !== "open";
  autoReply = phase === "connected";
  const { hooks, tools, messages } = setup();
  let stale = false;
  const ctx = {
    get hasUI() {
      if (stale) throw new Error("stale extension context");
      return false;
    },
  };
  const starting = hooks.get("session_start")!({}, ctx);
  await new Promise(setImmediate);
  const registered = tools.get("isaac_exec");
  stale = true;
  hooks.get("session_shutdown")!({}, { hasUI: false });
  const socket = sockets[0];
  socket.dispatchEvent(new Event("open"));
  socket.dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ id: 1, result: { helperDocs: "late hello", server: {} } }),
  }));
  socket.dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ method: "timeline.changed", params: { playing: true, simTime: 1 } }),
  }));
  socket.dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ method: "event", params: { name: "task.done", notify: true, payload: { label: "late", status: "ok" } } }),
  }));
  socket.dispatchEvent(new Event("close"));
  await starting;
  expect(tools.get("isaac_exec")).toBe(registered);
  expect(sockets).toHaveLength(1);
  expect(messages).toHaveLength(0);
});

test.each(["open", "hello", "connected"])("SDK invalidation without shutdown closes %s sockets", async (phase) => {
  autoOpen = phase !== "open";
  autoReply = phase === "connected";
  const { hooks, exec, messages } = setup();
  let stale = false;
  const ctx = {
    get hasUI() {
      if (stale) throw new Error("This extension ctx is stale after session replacement or reload.");
      return false;
    },
  };
  const starting = hooks.get("session_start")!({}, ctx);
  await new Promise(setImmediate);
  stale = true; // AgentSession.dispose(): no session_shutdown event.
  const socket = sockets[0];
  if (phase === "open") socket.dispatchEvent(new Event("open"));
  else if (phase === "hello") socket.dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ id: 1, result: { helperDocs: "late hello", server: {} } }),
  }));
  else socket.dispatchEvent(new Event("close"));
  await starting;
  expect(socket.closed).toBe(true);
  await expect(exec.execute("late", { code: "1" }, undefined, undefined, { cwd: home } as any))
    .rejects.toThrow("shut down");
  socket.dispatchEvent(new MessageEvent("message", {
    data: JSON.stringify({ method: "event", params: { name: "task.done", notify: true, payload: { label: "late", status: "ok" } } }),
  }));
  expect(messages).toHaveLength(0);
  expect(sockets).toHaveLength(1);
});
