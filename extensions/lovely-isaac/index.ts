// lovely-isaac: pi extension giving the agent a live Isaac Sim via the isaac-agent protocol.
// Discovers ~/.isaac-agent/<port>.lock, connects over WS+JSON-RPC, registers isaac_exec
// (+ isaac_events). Media from exec results lands in model context as images.

import { readFileSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { ImageContent, TextContent } from "@earendil-works/pi-ai";
import { Type } from "typebox";
// pi ships undici; the browser-API global WebSocket cannot set the auth header
import { WebSocket } from "undici";

const LOCK_DIR = join(homedir(), ".isaac-agent");
const STATUS_KEY = "lovely-isaac";
const RECONNECT_MAX_MS = 30_000;
const CONNECT_TIMEOUT_MS = 5_000;
const MAX_TEXT_BYTES = 50_000;
const EVENT_BUFFER_MAX = 500;

const EXEC_DESCRIPTION_INTRO = `Execute Python inside the running Isaac Sim (persistent \
namespace shared across calls, top-level await, notebook-style last-expression result). \
Images attached via agent.image() land directly in your context. Canceled (at the next \
await point) if you abort the tool call.

`;

const EXEC_DESCRIPTION_OFFLINE = `Execute Python inside a running Isaac Sim. \
(Isaac Sim is not currently reachable — the tool will try to connect on demand. \
Once connected, an injected \`agent\` helper provides viewport capture, media attach, \
timeline control, state queries, logs, and events; print(agent.docs()) for details.)`;

interface Lock {
  protocol: string;
  version: number;
  port: number;
  token: string;
  pid?: number;
  isaac_version?: string;
}

interface ExecResult {
  status: "ok" | "error";
  result?: unknown;
  stdout: string;
  stderr?: string;
  ename?: string;
  evalue?: string;
  traceback?: string[];
  media?: { mimeType: string; data: string; name?: string | null }[];
}

interface Connection {
  ws: WebSocket;
  lock: Lock;
  isaacVersion: string;
  stagePath: string;
}

export default function lovelyIsaac(pi: ExtensionAPI) {
  let currentCtx: ExtensionContext | undefined;
  let conn: Connection | undefined;
  let connecting: Promise<Connection> | undefined;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let reconnectDelay = 1_000;
  let wantConnection = true;
  let nextId = 1;
  const pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void }>();
  const events: { t: number; method: string; params: any }[] = [];
  let eventsDropped = 0;
  const timeline = { playing: false, simTime: 0 };

  // ------------------------------------------------------------- discovery

  function discoverLocks(): Lock[] {
    let files: string[];
    try {
      files = readdirSync(LOCK_DIR).filter((f) => f.endsWith(".lock"));
    } catch {
      return [];
    }
    const locks: Lock[] = [];
    for (const f of files) {
      try {
        const lock = JSON.parse(readFileSync(join(LOCK_DIR, f), "utf8"));
        if (lock.protocol !== "isaac-agent" || lock.version !== 1) continue;
        if (Number.isInteger(lock.pid)) {
          try {
            process.kill(lock.pid, 0);
          } catch (e: any) {
            if (e.code !== "EPERM") continue; // dead pid: stale lock
          }
        }
        locks.push(lock);
      } catch {
        /* unreadable lock: skip */
      }
    }
    return locks.sort((a: any, b: any) => (b.started_at || "").localeCompare(a.started_at || ""));
  }

  // ------------------------------------------------------------ connection

  function rpc<T = any>(method: string, params: unknown, timeoutMs?: number): { id: number; result: Promise<T> } {
    if (!conn) throw new Error("not connected to Isaac Sim");
    const id = nextId++;
    const result = new Promise<T>((resolve, reject) => {
      pending.set(id, { resolve, reject });
      conn!.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      if (timeoutMs) {
        setTimeout(() => {
          if (pending.delete(id)) reject(new Error(`${method} timed out after ${timeoutMs} ms`));
        }, timeoutMs);
      }
    });
    return { id, result };
  }

  function sendCancel(id: number) {
    conn?.ws.send(JSON.stringify({ jsonrpc: "2.0", method: "cancel", params: { id } }));
  }

  function handleMessage(raw: string) {
    let msg: any;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    if (msg.id != null && pending.has(msg.id)) {
      const p = pending.get(msg.id)!;
      pending.delete(msg.id);
      if (msg.error) p.reject(new Error(`Isaac rpc error ${msg.error.code}: ${msg.error.message}`));
      else p.resolve(msg.result);
      return;
    }
    if (msg.method === "timeline.changed") {
      timeline.playing = !!msg.params?.playing;
      timeline.simTime = msg.params?.simTime ?? 0;
      updateStatus();
      return;
    }
    if (msg.method === "log" || msg.method === "event") {
      if (events.length >= EVENT_BUFFER_MAX) {
        events.shift();
        eventsDropped++;
      }
      events.push({ t: Date.now(), method: msg.method, params: msg.params });
    }
  }

  async function dial(lock: Lock): Promise<Connection> {
    const url = `ws://127.0.0.1:${lock.port}`;
    const ws = new WebSocket(url, { headers: { "X-Isaac-Agent-Authorization": lock.token } });
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("connect timed out")), CONNECT_TIMEOUT_MS);
      ws.addEventListener("open", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
      ws.addEventListener("error", () => {
        clearTimeout(timer);
        reject(new Error(`connect failed (port ${lock.port})`));
      }, { once: true });
    });
    ws.addEventListener("message", (ev) => handleMessage(String(ev.data)));
    ws.addEventListener("close", () => {
      if (conn?.ws === ws) {
        conn = undefined;
        for (const p of pending.values()) p.reject(new Error("connection to Isaac Sim lost"));
        pending.clear();
        updateStatus();
        scheduleReconnect();
      }
    });
    conn = { ws, lock, isaacVersion: lock.isaac_version ?? "?", stagePath: "" };
    try {
      const hello = await rpc<any>("hello", {
        protocolVersion: 1,
        client: { name: "pi-lovely-isaac", version: "0.1.0", pid: process.pid },
        subscriptions: ["log", "timeline", "event"],
      }, CONNECT_TIMEOUT_MS).result;
      conn.isaacVersion = hello.server?.isaacVersion ?? conn.isaacVersion;
      conn.stagePath = hello.stage?.path ?? "";
      registerExecTool(hello.helperDocs);
      return conn;
    } catch (e) {
      ws.close();
      conn = undefined;
      throw e;
    }
  }

  async function ensureConnected(): Promise<Connection> {
    if (conn) return conn;
    if (connecting) return connecting;
    connecting = (async () => {
      const locks = discoverLocks();
      if (!locks.length) {
        throw new Error(
          `no live isaac-agent lockfile in ${LOCK_DIR} — start Isaac Sim with the xl0.lovely.isaac extension`,
        );
      }
      const errors: string[] = [];
      for (const lock of locks) {
        try {
          const c = await dial(lock);
          reconnectDelay = 1_000;
          updateStatus();
          return c;
        } catch (e: any) {
          errors.push(e.message);
        }
      }
      throw new Error(`could not connect to Isaac Sim: ${errors.join("; ")}`);
    })();
    try {
      return await connecting;
    } finally {
      connecting = undefined;
    }
  }

  function scheduleReconnect() {
    if (!wantConnection || reconnectTimer || !currentCtx) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined;
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      ensureConnected().catch(() => scheduleReconnect());
    }, reconnectDelay);
  }

  function disconnect() {
    wantConnection = false;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = undefined;
    conn?.ws.close();
    conn = undefined;
    updateStatus();
  }

  // ---------------------------------------------------------------- footer

  function updateStatus() {
    if (!currentCtx?.hasUI) return;
    const th = currentCtx.ui.theme;
    if (!conn) {
      currentCtx.ui.setStatus(
        STATUS_KEY,
        wantConnection ? th.fg("error", "○ Isaac disconnected") : th.fg("muted", "○ Isaac off"),
      );
      return;
    }
    const sim = timeline.playing ? ` ▶ ${timeline.simTime.toFixed(1)}s` : "";
    currentCtx.ui.setStatus(STATUS_KEY, `${th.fg("success", "● Isaac")} ${conn.isaacVersion}${sim}`);
  }

  // ----------------------------------------------------------------- tools

  function textBlock(text: string): TextContent {
    if (text.length > MAX_TEXT_BYTES) {
      text = `${text.slice(0, MAX_TEXT_BYTES)}\n... [truncated ${text.length - MAX_TEXT_BYTES} bytes]`;
    }
    return { type: "text", text };
  }

  function renderResult(r: ExecResult): (TextContent | ImageContent)[] {
    const parts: string[] = [];
    if (r.stdout) parts.push(r.stdout.replace(/\n$/, ""));
    if (r.stderr) parts.push(`stderr:\n${r.stderr.replace(/\n$/, "")}`);
    if (r.status === "ok") {
      if (r.result !== null && r.result !== undefined) {
        parts.push(`result: ${typeof r.result === "string" ? r.result : JSON.stringify(r.result, null, 2)}`);
      } else if (!parts.length && !r.media?.length) {
        parts.push("ok (no output)");
      }
    } else {
      parts.push((r.traceback ?? []).join("").replace(/\n$/, "") || `${r.ename}: ${r.evalue}`);
    }
    const content: (TextContent | ImageContent)[] = [];
    const mediaNotes: string[] = [];
    let mediaIdx = 0;
    for (const m of r.media ?? []) {
      if (m.mimeType.startsWith("image/")) {
        content.push({ type: "image", data: m.data, mimeType: m.mimeType });
      } else {
        const dir = join(tmpdir(), "isaac-agent-media");
        mkdirSync(dir, { recursive: true });
        const path = join(dir, `${(m.name || "media").replace(/[^\w.-]+/g, "_")}-${Date.now()}-${mediaIdx++}.bin`);
        writeFileSync(path, Buffer.from(m.data, "base64"));
        mediaNotes.push(`media ${m.name ?? "?"} (${m.mimeType}) saved to ${path}`);
      }
    }
    if (mediaNotes.length) parts.push(mediaNotes.join("\n"));
    if (events.length) parts.push(`(${events.length} notifications buffered — drain with isaac_events)`);
    content.unshift(textBlock(parts.join("\n\n")));
    return content;
  }

  function registerExecTool(helperDocs?: string) {
    pi.registerTool({
      name: "isaac_exec",
      label: "Isaac Sim exec",
      description: helperDocs ? EXEC_DESCRIPTION_INTRO + helperDocs : EXEC_DESCRIPTION_OFFLINE,
      promptSnippet: "isaac_exec — run Python inside the live Isaac Sim (scene, physics, screenshots)",
      parameters: Type.Object({
        code: Type.String({ description: "Python source to execute in Isaac Sim" }),
      }),
      async execute(_toolCallId, params, signal) {
        await ensureConnected();
        const { id, result } = rpc<ExecResult>("exec", { code: params.code });
        const onAbort = () => sendCancel(id);
        signal?.addEventListener("abort", onAbort, { once: true });
        try {
          const r = await result;
          return { content: renderResult(r), details: { status: r.status } };
        } finally {
          signal?.removeEventListener("abort", onAbort);
        }
      },
    });

    pi.registerTool({
      name: "isaac_events",
      label: "Isaac Sim events",
      description:
        "Drain notifications buffered from Isaac Sim since the last drain: carb log " +
        "warnings/errors, and events emitted by agent.emit() (telemetry from physics callbacks, " +
        "background-task progress).",
      parameters: Type.Object({
        max: Type.Optional(Type.Integer({ minimum: 1, description: "Max entries (default 100, newest kept)" })),
      }),
      async execute(_toolCallId, params) {
        const limit = params.max ?? 100;
        const drained = events.splice(0, events.length);
        const dropped = eventsDropped;
        eventsDropped = 0;
        const lines = drained.slice(-limit).map((e) => {
          const t = new Date(e.t).toISOString().slice(11, 19);
          if (e.method === "log") {
            return `[${t}] ${e.params.severity} [${e.params.source}] ${e.params.message}`;
          }
          return `[${t}] event ${e.params.name} ${JSON.stringify(e.params.payload)}`;
        });
        const head: string[] = [];
        if (dropped) head.push(`(${dropped} older notifications dropped)`);
        if (drained.length > limit) head.push(`(showing newest ${limit} of ${drained.length})`);
        return {
          content: [textBlock([...head, ...lines].join("\n") || "no buffered notifications")],
          details: { count: drained.length },
        };
      },
    });
  }

  registerExecTool(); // offline placeholder; re-registered with helperDocs after hello

  // -------------------------------------------------------------- command

  pi.registerCommand("isaac", {
    description: "Isaac Sim connection: /isaac [connect|disconnect|status|docs]",
    handler: async (args, ctx) => {
      const cmd = args.trim() || "status";
      if (cmd === "connect") {
        wantConnection = true;
        try {
          const c = await ensureConnected();
          ctx.ui.notify(`connected to Isaac ${c.isaacVersion} on port ${c.lock.port}`, "info");
        } catch (e: any) {
          ctx.ui.notify(e.message, "error");
        }
      } else if (cmd === "disconnect") {
        disconnect();
        ctx.ui.notify("Isaac connection closed", "info");
      } else if (cmd === "docs") {
        try {
          await ensureConnected();
          const r = await rpc<ExecResult>("exec", { code: "print(agent.docs())" }).result;
          ctx.ui.notify(r.stdout.slice(0, 2000), "info");
        } catch (e: any) {
          ctx.ui.notify(e.message, "error");
        }
      } else {
        ctx.ui.notify(
          conn
            ? `connected: Isaac ${conn.isaacVersion}, port ${conn.lock.port}, stage ${conn.stagePath || "?"}, ` +
              `${timeline.playing ? `playing at ${timeline.simTime.toFixed(2)} s` : "stopped"}`
            : "not connected (/isaac connect)",
          conn ? "info" : "warning",
        );
      }
    },
  });

  // ------------------------------------------------------------- lifecycle

  pi.on("session_start", async (_event, ctx) => {
    currentCtx = ctx;
    wantConnection = true;
    updateStatus();
    try {
      await ensureConnected();
    } catch {
      scheduleReconnect();
    }
  });

  pi.on("session_shutdown", () => {
    if (currentCtx?.hasUI) currentCtx.ui.setStatus(STATUS_KEY, undefined);
    disconnect();
    currentCtx = undefined;
  });
}
