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
- [x] Gates: 60 Python tests (42 live + 18 isolated) on Isaac 6.0.1, plus 8 Bun
      client tests. Original live gate and current isolated helpers also verified
      against Isaac 5.1.
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

## Deferred

- [ ] pi TUI eyeball pass (footer indicator, /isaac command) — only print mode was
      testable here.
- [ ] Further idle CPU profiling if its resource cost interferes with normal use.
- [ ] Opt-in wake-up notifications (`agent.notify`) after more usage feedback.
- [ ] By demonstrated need (v1 scope cuts): incremental stdout streaming, binary
      frames, in-Kit MCP, video.
