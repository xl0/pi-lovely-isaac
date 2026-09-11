// lovely-isaac: pi extension giving the agent a live Isaac Sim via the isaac-agent protocol.
// Discovers ~/.isaac-agent/<port>.lock, connects over WS+JSON-RPC, registers isaac_exec
// (+ isaac_result / isaac_events). Media from exec results lands in model context as images.

import { readFileSync, readdirSync, writeFileSync, mkdtempSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { highlightCode, keyHint, truncateHead, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { ImageContent, TextContent } from "@earendil-works/pi-ai";
import { Text, truncateToWidth } from "@earendil-works/pi-tui";
import { Type } from "typebox";
// pi ships undici; the browser-API global WebSocket cannot set the auth header
import { WebSocket } from "undici";

const LOCK_DIR = join(homedir(), ".isaac-agent");
const STATUS_KEY = "lovely-isaac";
const RECONNECT_MAX_MS = 30_000;
const CONNECT_TIMEOUT_MS = 5_000;
const MAX_TEXT_BYTES = 50 * 1024;
const EVENT_BUFFER_MAX = 500;
const DEFAULT_WAIT_MS = 1_000;
const MAX_WAIT_MS = 600_000;

const EXEC_DESCRIPTION_INTRO = `Execute Python inside the running Isaac Sim (persistent \
namespace shared across calls, top-level await, notebook-style last-expression result). \
Waits up to waitMs (default 1000) after submission, then returns a run ID if unfinished. \
Use isaac_result to retrieve output/images or request cancellation; do not resubmit \
the code. waitMs=0 detaches immediately. Detached completion wakes this session unless \
notify=false. Aborting this initial wait requests cancellation at the next await point. \
This frees pi, not Kit's main loop or the global exec FIFO. Pending may mean queued. \
Run IDs are local to this extension instance, not recoverable after reload/restart. \
Disconnect makes pending outcomes unknown; code is never retried automatically.

Supply exactly one of code or path. Files are \
read by pi (UTF-8, relative to the session working directory) and executed in the same \
namespace: no __main__, __file__, working-directory or import-path changes. Text over \
2000 lines or 50 KiB is previewed with a full-output file path.

`;

const EXEC_DESCRIPTION_OFFLINE = `(Isaac Sim is not currently reachable — the tool will try to connect on demand. \
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

interface ExecDetails {
  status?: "pending" | "ok" | "error" | "lost";
  id?: string;
  cancelRequested?: boolean;
  outputPath?: string;
  ename?: string;
  source?: { path: string; code: string };
}

interface ExecRun {
  id: string;
  rpcId: number;
  ws: WebSocket;
  label: string;
  status: NonNullable<ExecDetails["status"]>;
  notify: boolean;
  detached: boolean;
  cancelRequested: boolean;
  outputPath: string;
  source?: ExecDetails["source"];
  archiveError?: string;
  waiters: Set<() => void>;
}

export default function lovelyIsaac(pi: ExtensionAPI) {
  let currentCtx: ExtensionContext | undefined;
  let conn: Connection | undefined;
  let socket: WebSocket | undefined; // Includes a socket whose handshake is still pending.
  let connecting: Promise<Connection> | undefined;
  let generation = 0;
  let disposed = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let reconnectDelay = 1_000;
  let wantConnection = true;
  let nextId = 1;
  const pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void }>();
  const events: { t: number; method: string; params: any }[] = [];
  let eventsDropped = 0;
  const timeline = { playing: false, simTime: 0 };
  const runs = new Map<string, ExecRun>();
  let runDir: string | undefined;

  function isAlive(): boolean {
    if (disposed) return false;
    try {
      // SDK dispose() can invalidate the runner without emitting session_shutdown.
      // There is no public lifetime signal; probe before any asynchronous callback.
      void currentCtx?.hasUI;
      return true;
    } catch (error) {
      if (!(error instanceof Error) || !error.message.startsWith("This extension ctx is stale")) throw error;
      disposed = true;
      currentCtx = undefined;
      disconnect();
      return false;
    }
  }

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

  function rpc<T = any>(method: string, params: unknown, timeoutMs?: number): { id: number; result: Promise<T>; ws: WebSocket } {
    if (!conn) throw new Error("not connected to Isaac Sim");
    const ws = conn.ws;
    const id = nextId++;
    const result = new Promise<T>((resolve, reject) => {
      pending.set(id, { resolve, reject });
      try {
        ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      } catch (error) {
        pending.delete(id);
        reject(error);
        return;
      }
      if (timeoutMs) {
        setTimeout(() => {
          if (pending.delete(id)) reject(new Error(`${method} timed out after ${timeoutMs} ms`));
        }, timeoutMs);
      }
    });
    return { id, result, ws };
  }

  function cancelRun(run: ExecRun) {
    run.notify = false; // An explicit stop must not restart an idle agent.
    if (run.status !== "pending" || run.cancelRequested) return;
    if (run.ws !== conn?.ws || run.ws.readyState !== WebSocket.OPEN || !pending.has(run.rpcId)) return;
    run.ws.send(JSON.stringify({ jsonrpc: "2.0", method: "cancel", params: { id: run.rpcId } }));
    run.cancelRequested = true; // Request sent, not proof of cancellation.
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
      // The server targets watch completions to the registering connection.
      // Socket/generation fencing above prevents delivery into a replacement session.
      const p = msg.params?.payload;
      if (msg.method === "event" && msg.params?.name === "task.done" && msg.params.notify === true &&
          typeof p?.label === "string" && ["ok", "error", "cancelled"].includes(p.status)) {
        const traceback = Array.isArray(p.traceback) && p.traceback.every((line: unknown) => typeof line === "string")
          ? p.traceback.join("") : "";
        pi.sendMessage({
          customType: "isaac-task",
          content: [textBlock(`Isaac task ${JSON.stringify(p.label)}: ${p.status}\n${traceback}`.trimEnd())],
          display: true,
        }, { triggerTurn: true, deliverAs: "steer" });
      }
    }
  }

  async function dial(lock: Lock): Promise<Connection> {
    const url = `ws://127.0.0.1:${lock.port}`;
    const ws = new WebSocket(url, { headers: { "X-Isaac-Agent-Authorization": lock.token } });
    socket = ws;
    const epoch = generation;
    const isCurrent = () => isAlive() && epoch === generation && socket === ws;
    try {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("connect timed out")), CONNECT_TIMEOUT_MS);
        ws.addEventListener("open", () => {
          clearTimeout(timer);
          if (isCurrent()) resolve();
          else reject(new Error("connection canceled"));
        }, { once: true });
        ws.addEventListener("error", () => {
          clearTimeout(timer);
          reject(new Error(`connect failed (port ${lock.port})`));
        }, { once: true });
        ws.addEventListener("close", () => {
          clearTimeout(timer);
          reject(new Error("connection closed"));
        }, { once: true });
      });
      if (!isCurrent()) throw new Error("connection canceled");
      ws.addEventListener("message", (ev) => {
        if (isCurrent()) handleMessage(String(ev.data));
      });
      ws.addEventListener("close", () => {
        if (!isCurrent()) return;
        socket = undefined;
        conn = undefined;
        for (const p of pending.values()) p.reject(new Error("connection to Isaac Sim lost"));
        pending.clear();
        updateStatus();
        scheduleReconnect();
      });
      const candidate = { ws, lock, isaacVersion: lock.isaac_version ?? "?", stagePath: "" };
      conn = candidate;
      const hello = await rpc<any>("hello", {
        protocolVersion: 1,
        client: { name: "pi-lovely-isaac", version: "0.1.0", pid: process.pid },
        subscriptions: ["log", "timeline", "event"],
      }, CONNECT_TIMEOUT_MS).result;
      if (!isCurrent()) throw new Error("connection canceled");
      candidate.isaacVersion = hello.server?.isaacVersion ?? candidate.isaacVersion;
      candidate.stagePath = hello.stage?.path ?? "";
      registerExecTool(hello.helperDocs);
      return candidate;
    } catch (e) {
      if (socket === ws) {
        socket = undefined;
        conn = undefined;
      }
      ws.close();
      throw e;
    }
  }

  async function ensureConnected(): Promise<Connection> {
    if (!isAlive()) throw new Error("Isaac extension has shut down");
    if (connecting) return connecting;
    if (conn) return conn;
    const epoch = generation;
    const attempt = (async () => {
      const locks = discoverLocks();
      if (!locks.length) {
        throw new Error(
          `no live isaac-agent lockfile in ${LOCK_DIR} — start Isaac Sim with the xl0.lovely.isaac extension`,
        );
      }
      const errors: string[] = [];
      for (const lock of locks) {
        if (!isAlive() || epoch !== generation) throw new Error("connection canceled");
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
    connecting = attempt;
    try {
      return await attempt;
    } finally {
      if (connecting === attempt) connecting = undefined;
    }
  }

  function scheduleReconnect() {
    if (!isAlive() || !wantConnection || reconnectTimer || !currentCtx) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined;
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      ensureConnected().catch(() => scheduleReconnect());
    }, reconnectDelay);
  }

  function disconnect() {
    generation++;
    wantConnection = false;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = undefined;
    const oldSocket = socket;
    socket = undefined;
    conn = undefined;
    connecting = undefined;
    for (const p of pending.values()) p.reject(new Error("Isaac connection closed"));
    pending.clear();
    oldSocket?.close();
    updateStatus();
  }

  // ---------------------------------------------------------------- footer

  function updateStatus() {
    if (!isAlive() || !currentCtx?.hasUI) return;
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
    const preview = truncateHead(text, { maxBytes: MAX_TEXT_BYTES, maxLines: 2000 });
    if (preview.truncated) {
      const path = join(mkdtempSync(join(tmpdir(), "isaac-agent-output-")), "output.txt");
      writeFileSync(path, text, { encoding: "utf8", mode: 0o600 });
      text = `${preview.content}\n[Output truncated (${preview.totalLines} lines, ${preview.totalBytes} bytes). Full output saved to ${path}]`;
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
        const dir = mkdtempSync(join(tmpdir(), "isaac-agent-media-"));
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

  function finishRun(run: ExecRun, result: ExecResult, lost = false) {
    run.status = lost ? "lost" : result.status;
    try {
      writeFileSync(run.outputPath, JSON.stringify({ result, source: run.source }), { mode: 0o600 });
    } catch (error) {
      run.status = "error";
      run.archiveError = `Could not save Isaac exec ${run.id}: ${error}. Output is unavailable.\n` +
        (lost ? "Execution outcome is unknown." : `Execution returned ${result.status}.`) +
        " Do not automatically resubmit the code.";
    }
    run.source = undefined;
    for (const wake of run.waiters) wake();
    if (run.detached && run.notify && isAlive() && wantConnection) {
      pi.sendMessage({
        customType: "isaac-exec",
        content: `Isaac exec ${run.id} (${JSON.stringify(run.label)}): ${run.status}.\n` +
          (run.archiveError ?? `Raw response saved to ${run.outputPath}.\n` +
            `Read output/images with isaac_result({"id":"${run.id}"}).`) +
          (lost ? "\nExecution outcome is unknown. Inspect state before considering a retry." : ""),
        display: true,
      }, { triggerTurn: true, deliverAs: "steer" });
    }
  }

  function waitLimit(value: number | undefined, fallback: number) {
    const ms = value ?? fallback;
    if (!Number.isInteger(ms) || ms < 0 || ms > MAX_WAIT_MS) {
      throw new Error(`waitMs must be an integer between 0 and ${MAX_WAIT_MS}`);
    }
    return ms;
  }

  async function waitRun(run: ExecRun, ms: number, signal?: AbortSignal, cancelOnAbort = false) {
    if (!signal?.aborted && run.status === "pending" && ms > 0) {
      let wake!: () => void;
      const waiting = new Promise<void>((resolve) => { wake = resolve; });
      run.waiters.add(wake);
      const timer = setTimeout(wake, ms);
      signal?.addEventListener("abort", wake, { once: true });
      try {
        await waiting;
      } finally {
        clearTimeout(timer);
        run.waiters.delete(wake);
        signal?.removeEventListener("abort", wake);
      }
    }
    if (signal?.aborted) {
      if (cancelOnAbort) {
        cancelRun(run);
        return; // Keep the ID visible even if native cancellation is slow to acknowledge.
      }
      signal.throwIfAborted();
    }
  }

  function runSnapshot(run: ExecRun) {
    const details: ExecDetails = {
      id: run.id, status: run.status, cancelRequested: run.cancelRequested, source: run.source,
    };
    if (run.status === "pending") {
      return {
        content: [textBlock(
          `Isaac exec ${run.id} (${JSON.stringify(run.label)}) is pending (queued or running).` +
          (run.cancelRequested ? "\nCancellation requested; awaiting Isaac's response." : "") +
          `\nUse isaac_result({"id":"${run.id}"}) to retrieve output; do not resubmit the code.`,
        )],
        details,
      };
    }
    if (run.archiveError) throw new Error(run.archiveError);
    const saved: { result: ExecResult; source?: ExecDetails["source"] } = JSON.parse(readFileSync(run.outputPath, "utf8"));
    return {
      content: renderResult(saved.result),
      details: { ...details, ename: saved.result.ename, source: saved.source, outputPath: run.outputPath },
    };
  }

  function registerExecTool(helperDocs?: string) {
    pi.registerTool({
      name: "isaac_exec",
      label: "Isaac Sim exec",
      description: EXEC_DESCRIPTION_INTRO + (helperDocs ?? EXEC_DESCRIPTION_OFFLINE),
      promptSnippet: "isaac_exec — run Python inside the live Isaac Sim (scene, physics, screenshots)",
      parameters: Type.Object({
        code: Type.Optional(Type.String({ description: "Python source; supply exactly one of code or path" })),
        path: Type.Optional(Type.String({ minLength: 1, description: "UTF-8 script read by pi, relative to the session cwd; mutually exclusive with code" })),
        waitMs: Type.Optional(Type.Integer({ minimum: 0, maximum: MAX_WAIT_MS, description: "Wait after submission before detaching (default 1000 ms; 0 returns immediately)" })),
        label: Type.Optional(Type.String({ minLength: 1, maxLength: 200, description: "Short label for this run and its completion notification" })),
        notify: Type.Optional(Type.Boolean({ description: "Wake this session when a detached run finishes (default true); inline completions do not notify" })),
      }),
      renderCall(args, theme, context) {
        return {
          render(width) {
            const details = context.state as ExecDetails;
            const header = theme.fg("toolTitle", theme.bold("isaac_exec")) +
              (args.path === undefined ? "" : theme.fg("muted", ` path=${JSON.stringify(args.path)}`)) +
              theme.fg("muted", ` waitMs=${args.waitMs ?? DEFAULT_WAIT_MS}`) +
              (args.notify === undefined ? "" : theme.fg("muted", ` notify=${args.notify}`)) +
              (args.label === undefined ? "" : theme.fg("muted", ` label=${JSON.stringify(args.label)}`)) +
              (["error", "lost"].includes(details.status ?? "") ? theme.fg("error", ` ✗ ${details.ename ?? "Execution error"}`) : "");
            if (context.expanded) {
              // Never reread a path while painting: show the exact source submitted.
              const code = details.source?.code ?? args.code;
              const body = code === undefined ? "" : "\n" + highlightCode(code, "python").join("\n");
              return new Text(header + body, 0, 0).render(width);
            }
            const preview = header + (args.code === undefined ? "" :
              theme.fg("muted", ` code=${JSON.stringify(args.code)}`));
            // Preserve the surrounding tool background when truncation resets styles.
            return [truncateToWidth(preview, width).replaceAll("\x1b[0m", "\x1b[22;39m")];
          },
          invalidate() {},
        };
      },
      renderResult(result, { expanded }, theme, context) {
        Object.assign(context.state, result.details);
        const lines = result.content.filter((p): p is TextContent => p.type === "text")
          .map((p) => p.text).join("\n").split("\n");
        const text = (expanded ? lines : lines.slice(0, 10))
          .map((line) => theme.fg("toolOutput", line)).join("\n");
        const hint = !expanded && lines.length > 10
          ? `\n${theme.fg("muted", `... (${lines.length - 10} more lines, `)}${keyHint("app.tools.expand", "to expand")}${theme.fg("muted", ")")}`
          : "";
        return new Text(text + hint, 0, 0);
      },
      async execute(_toolCallId, params, signal, onUpdate, ctx) {
        if ((params.code === undefined) === (params.path === undefined)) {
          throw new Error("Supply exactly one of code or path");
        }
        const waitMs = waitLimit(params.waitMs, DEFAULT_WAIT_MS);
        signal?.throwIfAborted();
        const path = params.path === undefined ? undefined : resolve(ctx.cwd, params.path);
        const code = path === undefined ? params.code! : await readFile(path, "utf8");
        const source = path === undefined ? undefined : { path, code };
        if (source) onUpdate?.({ content: [], details: { source } });
        await ensureConnected();
        signal?.throwIfAborted();
        runDir ??= mkdtempSync(join(tmpdir(), "isaac-agent-runs-"));
        let id: string;
        do { id = `i_${randomUUID().slice(0, 8)}`; } while (runs.has(id));
        const request = rpc<ExecResult>("exec", { code });
        const run: ExecRun = {
          id, rpcId: request.id, ws: request.ws, label: params.label ?? "Isaac exec",
          status: "pending", notify: params.notify ?? true, detached: false, cancelRequested: false,
          outputPath: join(runDir, `${id}.json`), source, waiters: new Set(),
        };
        runs.set(id, run);
        // Own the pending RPC independently of this tool's bounded wait.
        void request.result.then(
          (r) => finishRun(run, r),
          (error) => finishRun(run, {
            status: "error", ename: "IsaacRpcError", stdout: "",
            evalue: `${error.message ?? error}. Execution outcome is unknown; do not blindly resubmit.`,
          }, true),
        );
        onUpdate?.({ content: [], details: { id, status: "pending", source } });
        try {
          await waitRun(run, waitMs, signal, true);
          return runSnapshot(run);
        } finally {
          run.detached = run.status === "pending";
        }
      },
    });

    pi.registerTool({
      name: "isaac_result",
      label: "Isaac Sim result",
      description: "Read the original output/images of an isaac_exec run without re-executing code. " +
        "waitMs defaults to 0; cancel=true requests cancellation on the original connection, not proof it stopped. " +
        "Aborting this tool only stops waiting. Explicit cancellation suppresses the run's automatic wakeup. " +
        "Completed reads are repeatable; IDs live only until this client reloads/restarts. " +
        "Connection loss is reported as an unknown outcome, never retried.",
      parameters: Type.Object({
        id: Type.String({ description: "Run ID returned by isaac_exec" }),
        waitMs: Type.Optional(Type.Integer({ minimum: 0, maximum: MAX_WAIT_MS, description: "Wait up to this many milliseconds for the existing run (default 0)" })),
        cancel: Type.Optional(Type.Boolean({ description: "Request cooperative cancellation of the existing run (default false)" })),
      }),
      renderCall(args, theme) {
        return new Text(theme.fg("toolTitle", theme.bold("isaac_result")) +
          theme.fg("muted", ` id=${args.id ?? ""} waitMs=${args.waitMs ?? 0}${args.cancel === undefined ? "" : ` cancel=${args.cancel}`}`), 0, 0);
      },
      async execute(_toolCallId, params, signal) {
        const waitMs = waitLimit(params.waitMs, 0);
        signal?.throwIfAborted();
        if (!isAlive()) throw new Error("Isaac extension has shut down");
        const run = runs.get(params.id);
        if (!run) throw new Error(`Unknown Isaac run ${params.id}; run IDs do not survive client reload/restart`);
        if (params.cancel) cancelRun(run);
        await waitRun(run, waitMs, signal);
        return runSnapshot(run);
      },
    });

    pi.registerTool({
      name: "isaac_events",
      label: "Isaac Sim events",
      description:
        "Read and consume buffered Isaac Sim notifications, oldest first: carb log " +
        "warnings/errors, and events emitted by agent.emit() (telemetry from physics callbacks, " +
        "background-task progress). Unreturned entries stay buffered unless flush=true.",
      parameters: Type.Object({
        max: Type.Optional(Type.Integer({ minimum: 1, description: "Max entries to consume (default 100, oldest first)" })),
        flush: Type.Optional(Type.Boolean({ description: "Discard remaining buffered entries after this read (default false)" })),
      }),
      renderCall(args, theme) {
        return new Text(
          theme.fg("toolTitle", theme.bold("isaac_events")) +
          theme.fg("muted", ` max=${args.max ?? 100}${args.flush === undefined ? "" : ` flush=${args.flush}`}`),
          0, 0,
        );
      },
      async execute(_toolCallId, params) {
        const limit = params.max ?? 100;
        const drained = events.splice(0, limit);
        const flushed = params.flush ? events.splice(0, events.length).length : 0;
        const dropped = eventsDropped;
        eventsDropped = 0;
        const lines = drained.map((e) => {
          const t = new Date(e.t).toISOString().slice(11, 19);
          if (e.method === "log") {
            return `[${t}] ${e.params.severity} [${e.params.source}] ${e.params.message}`;
          }
          return `[${t}] event ${e.params.name} ${JSON.stringify(e.params.payload)}`;
        });
        const head: string[] = [];
        if (dropped) head.push(`(${dropped} older notifications dropped)`);
        if (flushed) head.push(`(${flushed} remaining notifications flushed)`);
        if (events.length) head.push(`(${events.length} notifications remain buffered)`);
        return {
          content: [textBlock([...head, ...lines].join("\n") || "no buffered notifications")],
          details: { count: drained.length, remaining: events.length, flushed },
        };
      },
    });
  }

  registerExecTool(); // offline placeholder; re-registered with helperDocs after hello
  pi.on("tool_result", (event) => {
    if (["isaac_exec", "isaac_result"].includes(event.toolName) &&
        ["error", "lost"].includes((event.details as ExecDetails | undefined)?.status ?? "")) {
      return { isError: true }; // Preserve stdout/media while marking Python failures as tool errors.
    }
  });

  // -------------------------------------------------------------- command

  pi.registerCommand("isaac", {
    description: "Isaac Sim connection: /isaac [connect|disconnect|status|docs]",
    handler: async (args, ctx) => {
      const cmd = args.trim() || "status";
      if (cmd === "connect") {
        wantConnection = true;
        try {
          const c = await ensureConnected();
          if (!isAlive()) return;
          ctx.ui.notify(`connected to Isaac ${c.isaacVersion} on port ${c.lock.port}`, "info");
        } catch (e: any) {
          if (isAlive()) ctx.ui.notify(e.message, "error");
        }
      } else if (cmd === "disconnect") {
        disconnect();
        ctx.ui.notify("Isaac connection closed", "info");
      } else if (cmd === "docs") {
        try {
          await ensureConnected();
          if (!isAlive()) return;
          const r = await rpc<ExecResult>("exec", { code: "print(agent.docs())" }).result;
          if (!isAlive()) return;
          ctx.ui.notify(r.stdout.slice(0, 2000), "info");
        } catch (e: any) {
          if (isAlive()) ctx.ui.notify(e.message, "error");
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

  pi.on("session_shutdown", (_event, ctx) => {
    // Fence callbacks before touching UI or closing sockets. A dial/hello can
    // finish after reload has invalidated the old pi context.
    if (disposed) return;
    disposed = true;
    currentCtx = undefined;
    disconnect();
    if (ctx.hasUI) ctx.ui.setStatus(STATUS_KEY, undefined);
  });
}
