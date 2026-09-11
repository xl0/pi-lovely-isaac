// Opt-in live client gate: bun tools/smoke_async_pi.ts
// Uses real WebSockets but no model/API calls. No scene, timeline or viewport changes.
import assert from "node:assert/strict";
import { rmSync } from "node:fs";
import { dirname } from "node:path";
import type { ExtensionAPI, ToolDefinition } from "@earendil-works/pi-coding-agent";
import lovelyIsaac from "../extensions/lovely-isaac/index.ts";

const tools = new Map<string, ToolDefinition<any, any>>();
const hooks = new Map<string, (...args: any[]) => any>();
const notices: any[] = [];
const dirs = new Set<string>();
const ctx = { cwd: process.cwd(), hasUI: false };
lovelyIsaac({
  registerTool: (tool: ToolDefinition<any, any>) => tools.set(tool.name, tool),
  registerCommand() {},
  on: (name: string, fn: (...args: any[]) => any) => hooks.set(name, fn),
  sendMessage: (message: any, options: any) => notices.push({ message, options }),
} as unknown as ExtensionAPI);

async function call(name: string, args: any) {
  const result = await tools.get(name)!.execute(name, args, undefined, undefined, ctx as any);
  if (result.details.outputPath) dirs.add(dirname(result.details.outputPath));
  return result;
}
try {
  await hooks.get("session_start")!({}, ctx);
  const started = performance.now();
  const pending = await call("isaac_exec", {
    code: "await asyncio.sleep(1.5)\nagent.image(np.zeros((8, 8, 3), dtype=np.uint8))\n42",
    label: "bounded-wait smoke",
  });
  const elapsed = performance.now() - started;
  assert.equal(pending.details.status, "pending");
  assert(elapsed < 1400, `default wait did not detach before completion: ${elapsed} ms`);
  const output = await call("isaac_result", { id: pending.details.id, waitMs: 3000 });
  assert.equal(output.details.status, "ok");
  assert(output.content.some((p: any) => p.type === "image"));
  assert(output.content[0].type === "text");
  assert.match(output.content[0].text, /result: 42/);
  assert.equal(notices.length, 1);
  assert.deepEqual(notices[0].options, { triggerTurn: true, deliverAs: "steer" });
  console.log(`Default wait detached after ${Math.round(elapsed)} ms; result/image received and wakeup requested.`);

  const slow = await call("isaac_exec", { code: "await asyncio.sleep(30)", waitMs: 0 });
  const cancelled = await call("isaac_result", { id: slow.details.id, cancel: true, waitMs: 3000 });
  assert.equal(cancelled.details.ename, "CancelledError");
  assert.equal(notices.length, 1); // Explicit cancellation does not wake the agent.
  console.log("Explicit cancellation acknowledged without an extra wakeup.");

  const fail = await call("isaac_exec", {
    code: "await asyncio.sleep(0.1)\nprint('partial output')\n1 / 0", waitMs: 0, notify: false,
  });
  const error = await call("isaac_result", { id: fail.details.id, waitMs: 3000 });
  assert.equal(error.details.ename, "ZeroDivisionError");
  assert(error.content[0].type === "text");
  assert.match(error.content[0].text, /partial output/);
  assert.equal(notices.length, 1);
  console.log("Detached Python error preserved partial output and traceback.");

  const first = await call("isaac_exec", { code: "await asyncio.sleep(0.3)\n'first'", waitMs: 0, notify: false });
  const second = await call("isaac_exec", { code: "'second'", waitMs: 0, notify: false });
  assert.equal(second.details.status, "pending");
  assert.equal((await call("isaac_result", { id: first.details.id, waitMs: 3000 })).details.status, "ok");
  assert.equal((await call("isaac_result", { id: second.details.id, waitMs: 3000 })).details.status, "ok");
  console.log("Existing exec FIFO preserved; queued requests retrieved without resubmission.");
} finally {
  hooks.get("session_shutdown")!({}, ctx);
  await new Promise(setImmediate);
  for (const dir of dirs) rmSync(dir, { recursive: true });
}
