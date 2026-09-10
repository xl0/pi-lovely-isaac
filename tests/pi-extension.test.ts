import { afterAll, afterEach, expect, mock, test } from "bun:test";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
mock.module("node:os", () => ({ ...os, homedir: () => home }));
const requests: any[] = [];
const sockets: Socket[] = [];
let autoOpen = true;
let autoReply = true;
let reply: any = { status: "ok", result: 42, stdout: "" };
class Socket extends EventTarget {
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
  close() { this.closed = true; this.dispatchEvent(new Event("close")); }
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
afterEach(() => {
  for (const stop of shutdowns.splice(0)) stop();
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
  expect(owner.messages[0].options).toEqual({ triggerTurn: true, deliverAs: "followUp" });
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
