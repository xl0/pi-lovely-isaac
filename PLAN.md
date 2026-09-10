# Plan

Implement docs/DESIGN.md: exec-only agent protocol for Isaac Sim (Kit WS server),
then clients: Node CLI, pi extension, MCP stdio adapter. Verify with live scenarios;
keep design doc in sync with reality. Intent: coding agents (pi, Claude Code) drive
the sim ergonomically — screenshots land in model context, telemetry streams, long
jobs don't block turns.

## Done

- [x] Kit extension server (exec/cancel/media/notifications/helpers incl.
      preview_asset), Node CLI, pi extension (tested with gpt-5.6-sol incl. an
      autonomous restitution experiment), MCP adapter (registered in .mcp.json,
      dogfooded via headless Claude Code).
- [x] Prior full live gate on Isaac 6.0.1; current changes pass 42 isolated Python
      cases and 19 Bun client tests, plus Ruff/TypeScript. Original live gate and
      earlier isolated helpers also verified against Isaac 5.1.
- [x] Adversarial review workflow: 22 confirmed findings fixed (cancel-burst race,
      guaranteed exec responses, NaN-JSON, malformed-request hardening, orchestrator
      wedge recovery, client reconnect/timeout robustness, etc.) + regression tests.
- [x] Scenarios: falling_cube, telemetry_bounce. DESIGN.md updated with [impl] notes
      incl. Isaac 6 compatibility.
- [x] Research launcher enables the server. Local `tools/launch_isaac6.sh` selects
      Isaac 6 with Jupyter, the agent server, 60 Hz cap, and RTX eco mode.
- [x] File exec, source snapshots, highlighted argument headers, full-output files,
      and error presentation in pi/MCP. Reload callbacks are lifecycle-fenced.
      Bounded event reads retain the remainder unless explicitly flushed.
- [x] Capture restores render/timeline settings on every exit, preview cleanup is
      ownership-scoped. Live camera cancel/playing checks pass. State discrepancy
      confirmed as USD layer masking; helper contract documents it.
- [x] Greptile cleanup-hang finding: shared deadline for asynchronous stop/restoration,
      independent settings snapshot, and ownership release on timeout. All 21 isolated
      helper cases pass, including stalled stop/restoration and repeated cancellation.
- [x] Local uv dev environment and tooling instructions. Idle render cap measured;
      60 Hz did not improve CPU, so no speculative tuning was retained.
- [x] Fin Ray feedback: aspect-preserving active screenshots, read-only timeout
      diagnostics, shared-view/Kit-frame recipes and timeline-clock semantics.
      `agent.watch` adds opt-in, connection-scoped terminal wakeups with tracebacks;
      no task scheduler or replay. Workflow nudges remain in the external usage skill.
- [x] SDK disposal can skip shutdown events: async callback liveness checks tear down
      stale connections, covering the repeated pi crash and preventing stale wakeups.
- [x] User-reloaded live smoke: aspect-preserving shared screenshot, all three terminal
      task outcomes, retained traceback/result, and pi completion wakeup. Scene/camera
      preserved; temporary test tasks cleaned up.
- [x] Pi bounded waits: existing RPC retained after the 1000 ms default wait,
      short collision-checked run IDs, `isaac_result` retrieval/cancellation, private output archives, and scoped
      completion wakeups. Unknown outcomes are never resubmitted. Offline regressions
      and authorized real-WebSocket smoke passed; no server/reload or scene changes.
      After pi reload, direct tool waits/result retrieval/cancellation and actual
      in-session completion wakeup delivery also passed.
- [x] Exec/watch notifications use steers while busy and wake idle pi, rather than
      accumulating follow-ups until the entire agent run ends. Verified offline.

## Deferred

- [ ] Mirror bounded waits/result retrieval in MCP, keeping its execution timeout
      distinct from the tool wait budget. No server scheduler or script wrapping.
- [ ] pi TUI eyeball pass (footer indicator, /isaac command) — only print mode was
      testable here.
- [ ] Further idle CPU profiling if its resource cost interferes with normal use.
- [ ] By demonstrated need (v1 scope cuts): incremental stdout streaming, binary
      frames, in-Kit MCP, video.
