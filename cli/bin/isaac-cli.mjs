#!/usr/bin/env node
// isaac-cli: drive a running Isaac Sim over the isaac-agent protocol (WS + JSON-RPC).
// Zero dependencies; Node >= 22 (global WebSocket).

import { readFileSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import process from "node:process";
import { WebSocket } from "undici"; // browser-API global WebSocket cannot set the auth header

const LOCK_DIR = join(homedir(), ".isaac-agent");
const EXT_BY_MIME = {
  "image/png": ".png", "image/jpeg": ".jpg", "application/json": ".json",
  "application/octet-stream": ".bin", "text/plain": ".txt", "application/x-npz": ".npz",
};

// ---------------------------------------------------------------- discovery

function pidAlive(pid) {
  if (!Number.isInteger(pid)) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; }
}

function discover() {
  const explicit = process.env.ISAAC_AGENT_LOCK;
  const files = explicit
    ? [explicit]
    : (() => {
        try { return readdirSync(LOCK_DIR).filter((f) => f.endsWith(".lock")).map((f) => join(LOCK_DIR, f)); }
        catch { return []; }
      })();
  const locks = [];
  for (const f of files) {
    try {
      const data = JSON.parse(readFileSync(f, "utf8"));
      if (data.protocol === "isaac-agent" && pidAlive(data.pid)) locks.push(data);
    } catch { /* unreadable lock: skip */ }
  }
  return locks.sort((a, b) => (b.started_at || "").localeCompare(a.started_at || ""));
}

// ---------------------------------------------------------------- rpc client

class Rpc {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.onNotification = null;
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id != null && this.pending.has(msg.id)) {
        const { resolve: res, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(`rpc ${msg.error.code}: ${msg.error.message}`));
        else res(msg.result);
      } else if (msg.method) {
        this.onNotification?.(msg.method, msg.params ?? {});
      }
    });
    ws.addEventListener("close", () => {
      for (const { reject } of this.pending.values()) reject(new Error("connection closed"));
      this.pending.clear();
    });
  }

  call(method, params) {
    const id = this.nextId++;
    return new Promise((res, reject) => {
      if (this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("connection to Isaac Sim lost"));
        return;
      }
      this.pending.set(id, { resolve: res, reject });
      this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    });
  }

  notify(method, params) {
    this.ws.send(JSON.stringify({ jsonrpc: "2.0", method, params }));
  }

  lastExecId() { return this.nextId - 1; }
}

async function connect(subscriptions) {
  const locks = discover();
  if (!locks.length) die(2, `no live isaac-agent lockfile in ${LOCK_DIR} — is Isaac Sim running with xl0.lovely.isaac?`);
  const errors = [];
  for (const lock of locks) {
    const url = `ws://127.0.0.1:${lock.port}`;
    try {
      const ws = await new Promise((res, reject) => {
        const s = new WebSocket(url, { headers: { "X-Isaac-Agent-Authorization": lock.token } });
        const timer = setTimeout(() => {
          s.close();
          reject(new Error(`connect timed out: port ${lock.port}`));
        }, 5000);
        s.addEventListener("open", () => { clearTimeout(timer); res(s); }, { once: true });
        s.addEventListener("error", () => {
          clearTimeout(timer);
          reject(new Error(`connect failed: port ${lock.port}`));
        }, { once: true });
      });
      const rpc = new Rpc(ws);
      const hello = await Promise.race([
        rpc.call("hello", {
          protocolVersion: 1,
          client: { name: "isaac-cli", version: "0.1.0", pid: process.pid },
          subscriptions: subscriptions ?? [],
        }),
        new Promise((_, reject) => setTimeout(() => reject(new Error(`hello timed out: port ${lock.port}`)), 5000)),
      ]);
      return { rpc, ws, hello, lock };
    } catch (e) {
      errors.push(e.message); // stale lock (dead server, live pid) — try next
    }
  }
  die(2, `could not connect to any isaac-agent server:\n  ${errors.join("\n  ")}`);
}

function die(code, msg) {
  console.error(msg);
  process.exit(code);
}

// ---------------------------------------------------------------- rendering

const tty = process.stderr.isTTY;
const dim = (s) => (tty ? `\x1b[2m${s}\x1b[0m` : s);
const red = (s) => (tty ? `\x1b[31m${s}\x1b[0m` : s);
const yellow = (s) => (tty ? `\x1b[33m${s}\x1b[0m` : s);

function saveMedia(media, dir) {
  const paths = [];
  mkdirSync(dir, { recursive: true });
  const used = new Set();
  media.forEach((m, i) => {
    const ext = EXT_BY_MIME[m.mimeType] ?? ".bin";
    let base = (m.name || `media${i}`).replace(/[^\w.-]+/g, "_");
    let path = join(dir, base + ext);
    for (let n = 2; used.has(path); n++) path = join(dir, `${base}-${n}${ext}`);
    used.add(path);
    writeFileSync(path, Buffer.from(m.data, "base64"));
    paths.push(path);
  });
  return paths;
}

function printExecResult(result, opts) {
  if (opts.json) {
    console.log(JSON.stringify(result, null, 2));
    return result.status === "ok" ? 0 : 1;
  }
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(red(result.stderr));
  if (result.media?.length) {
    const paths = saveMedia(result.media, opts.mediaDir ?? ".");
    for (const p of paths) console.error(dim(`media saved: ${p}`));
  }
  if (result.status === "ok") {
    if (result.result !== null && result.result !== undefined) {
      const v = result.result;
      console.log(typeof v === "string" ? v : JSON.stringify(v, null, 2));
    }
    return 0;
  }
  process.stderr.write((result.traceback || []).join(""));
  console.error(red(`${result.ename}: ${result.evalue}`));
  return 1;
}

function formatNotification(method, params) {
  const t = new Date().toISOString().slice(11, 19);
  if (method === "log") {
    const sev = params.severity === "error" || params.severity === "fatal" ? red(params.severity) : yellow(params.severity);
    const dropped = params.dropped ? dim(` (+${params.dropped} dropped)`) : "";
    return `${dim(t)} ${sev} [${params.source}] ${params.message}${dropped}`;
  }
  if (method === "timeline.changed")
    return `${dim(t)} timeline ${params.playing ? "playing" : "stopped"} simTime=${params.simTime.toFixed(3)}`;
  if (method === "event")
    return `${dim(t)} event ${params.name} ${JSON.stringify(params.payload)}`;
  return `${dim(t)} ${method} ${JSON.stringify(params)}`;
}

// ---------------------------------------------------------------- exec

async function runExec(code, opts) {
  const { rpc, ws } = await connect(["event"]);
  rpc.onNotification = (method, params) => {
    if (method === "event") console.error(formatNotification(method, params));
  };
  const id = rpc.nextId; // exec request will take this id
  let timer;
  if (opts.timeout) timer = setTimeout(() => rpc.notify("cancel", { id }), opts.timeout * 1000);
  let interrupts = 0;
  const onInt = () => {
    if (++interrupts >= 2) {
      console.error(dim("^C^C — giving up (the exec keeps running in Isaac until its next await point)"));
      process.exit(130);
    }
    console.error(dim("^C — canceling exec (takes effect at next await point; ^C again to quit now)"));
    rpc.notify("cancel", { id });
  };
  process.on("SIGINT", onInt);
  try {
    const result = await rpc.call("exec", { code });
    return printExecResult(result, opts);
  } finally {
    clearTimeout(timer);
    process.off("SIGINT", onInt);
    ws.close();
  }
}

// ---------------------------------------------------------------- commands

function parseArgs(argv, flags) {
  // flags: {name: "bool"|"value"}; returns {_: positionals, ...flags}
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const name = a.slice(2);
      if (!(name in flags)) die(2, `unknown flag --${name}`);
      out[name] = flags[name] === "bool" ? true : argv[++i];
    } else if (a === "-f" && "file" in flags) {
      out.file = argv[++i];
    } else if (a === "-n" && "n" in flags) {
      out.n = argv[++i];
    } else {
      out._.push(a);
    }
  }
  return out;
}

const commands = {
  async exec(argv) {
    const o = parseArgs(argv, { file: "value", timeout: "value", "media-dir": "value", json: "bool" });
    let code;
    if (o.file) code = readFileSync(o.file === "-" ? 0 : o.file, "utf8");
    else if (o._.length) code = o._.join(" ");
    else code = readFileSync(0, "utf8");
    process.exit(await runExec(code, { timeout: o.timeout ? Number(o.timeout) : 0, mediaDir: o["media-dir"], json: o.json }));
  },

  async status() {
    const { rpc, ws, hello, lock } = await connect([]);
    const r = await rpc.call("exec", { code: "agent.status()" });
    ws.close();
    console.log(`server  ws://127.0.0.1:${lock.port} (pid ${lock.pid})`);
    console.log(`isaac   ${hello.server.isaacVersion}  kit ${hello.server.kitVersion}  ext ${hello.server.extensionVersion}`);
    if (r.status === "ok") for (const [k, v] of Object.entries(r.result)) console.log(`${k.padEnd(9)} ${JSON.stringify(v)}`);
    else process.exit(printExecResult(r, {}));
  },

  async screenshot(argv) {
    const o = parseArgs(argv, { width: "value", height: "value", camera: "value" });
    const out = resolve(o._[0] ?? "screenshot.png");
    const kw = [];
    if (o.width) kw.push(`width=${Number(o.width)}`);
    if (o.height) kw.push(`height=${Number(o.height)}`);
    if (o.camera) kw.push(`camera=${JSON.stringify(o.camera)}`);
    const { rpc, ws } = await connect([]);
    const r = await rpc.call("exec", { code: `agent.image(await agent.viewport(${kw.join(", ")}), name="screenshot")` });
    ws.close();
    if (r.status !== "ok") process.exit(printExecResult(r, {}));
    writeFileSync(out, Buffer.from(r.media[0].data, "base64"));
    console.log(out);
  },

  async logs(argv) {
    const o = parseArgs(argv, { n: "value", "min-severity": "value" });
    const { rpc, ws } = await connect([]);
    const r = await rpc.call("exec", {
      code: `agent.logs(${Number(o.n ?? 50)}, min_severity=${JSON.stringify(o["min-severity"] ?? "warning")})`,
    });
    ws.close();
    if (r.status !== "ok") process.exit(printExecResult(r, {}));
    for (const e of r.result) {
      const t = new Date(e.t * 1000).toISOString().slice(11, 19);
      console.log(`${dim(t)} ${yellow(e.severity)} [${e.source}] ${e.message}`);
    }
  },

  async watch(argv) {
    const kinds = argv.length ? argv : ["log", "timeline", "event"];
    const { rpc, ws, lock } = await connect(kinds);
    console.error(dim(`watching ${kinds.join(", ")} on port ${lock.port} — Ctrl-C to stop`));
    rpc.onNotification = (m, p) => console.log(formatNotification(m, p));
    await new Promise((_, reject) =>
      ws.addEventListener("close", () => reject(new Error("connection to Isaac Sim closed")), { once: true }),
    );
  },

  async ping() {
    const t0 = performance.now();
    const { rpc, ws, lock } = await connect([]);
    await rpc.call("ping", {});
    console.log(`pong from port ${lock.port} in ${(performance.now() - t0).toFixed(1)} ms`);
    ws.close();
  },

  async docs() {
    const { ws, hello } = await connect([]);
    ws.close();
    console.log(hello.helperDocs);
  },

  async repl() {
    const { rpc, ws, hello, lock } = await connect(["event", "timeline"]);
    console.error(dim(`isaac ${hello.server.isaacVersion} @ port ${lock.port} — .help for commands`));
    rpc.onNotification = (m, p) => { if (m === "event") console.error(formatNotification(m, p)); };
    const rl = createInterface({ input: process.stdin, output: process.stderr, prompt: "isaac> " });
    let buffer = [];
    let pendingId = null;
    rl.on("SIGINT", () => {
      if (pendingId != null) {
        console.error(dim("^C — canceling"));
        rpc.notify("cancel", { id: pendingId });
      } else if (buffer.length) {
        buffer = [];
        rl.setPrompt("isaac> ");
        console.error(dim("(block discarded)"));
        rl.prompt();
      } else {
        rl.close();
      }
    });
    const send = async (code) => {
      pendingId = rpc.nextId;
      try {
        const result = await rpc.call("exec", { code });
        printExecResult(result, { mediaDir: "." });
      } catch (e) {
        console.error(red(e.message));
      } finally {
        pendingId = null;
      }
    };
    rl.prompt();
    for await (const line of rl) {
      if (buffer.length) {
        if (line.trim() === "") {
          const code = buffer.join("\n");
          buffer = [];
          rl.setPrompt("isaac> ");
          await send(code);
        } else {
          buffer.push(line);
        }
        rl.prompt();
        continue;
      }
      const trimmed = line.trim();
      if (trimmed === ".help") {
        console.error("  .file <path>  send a python file\n  .docs         print helper docs\n  block mode: end a line with ':' then finish with an empty line\n  Ctrl-C cancels a running exec");
      } else if (trimmed.startsWith(".file ")) {
        try {
          await send(readFileSync(trimmed.slice(6).trim(), "utf8"));
        } catch (e) {
          console.error(red(e.message));
        }
      } else if (trimmed === ".docs") {
        console.log(hello.helperDocs);
      } else if (trimmed.endsWith(":") || trimmed.endsWith("\\")) {
        buffer.push(line);
        rl.setPrompt(dim("....> "));
      } else if (trimmed !== "") {
        await send(line);
      }
      rl.prompt();
    }
    ws.close();
  },
};

const [cmd, ...rest] = process.argv.slice(2);
if (!cmd || cmd === "--help" || cmd === "-h" || !(cmd in commands)) {
  console.error(`usage: isaac-cli <command>

commands:
  exec [code] [-f file|-] [--timeout s] [--media-dir d] [--json]   run python in Isaac Sim
  repl                                                            interactive session
  status                                                          server + sim status
  screenshot [out.png] [--width w] [--height h] [--camera path]   capture viewport
  logs [-n 50] [--min-severity warning]                           recent carb logs
  watch [log timeline event]                                      stream notifications
  ping                                                            round-trip check
  docs                                                            print agent helper docs

discovery: newest live lockfile in ~/.isaac-agent (override: ISAAC_AGENT_LOCK=<file>)`);
  process.exit(cmd && cmd !== "--help" && cmd !== "-h" ? 2 : 0);
}
commands[cmd](rest).catch((e) => die(2, e.message));
