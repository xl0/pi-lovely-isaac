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
- [x] Gates green: tests/test_server.py (33) + tests/test_mcp_adapter.py (7),
      both on Isaac 5.1.0 and on a fresh Isaac 6.0.1 env (`isaacsim6`, py3.12).
- [x] Adversarial review workflow: 22 confirmed findings fixed (cancel-burst race,
      guaranteed exec responses, NaN-JSON, malformed-request hardening, orchestrator
      wedge recovery, client reconnect/timeout robustness, etc.) + regression tests.
- [x] Scenarios: falling_cube, telemetry_bounce. DESIGN.md updated with [impl] notes
      incl. Isaac 6 compatibility.

## Remaining

- [ ] User-side: add `--ext-folder <repo>/exts --enable xl0.lovely.isaac` to the
      research-repo env.sh launcher (read-only from this sandbox).
- [ ] pi TUI eyeball pass (footer indicator, /isaac command) — only print mode was
      testable here.
- [ ] By demonstrated need (v1 scope cuts): incremental stdout streaming, binary
      frames, in-Kit MCP, video.
