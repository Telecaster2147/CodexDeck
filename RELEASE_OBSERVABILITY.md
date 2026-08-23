# CodexDeck 0.3.x release observability

CodexDeck does not collect telemetry. Release health is reviewed from opt-in, manually redacted
Doctor reports, GitHub issue forms, CI artifacts and the deterministic local performance gate.
This keeps the read-only and local-first product boundary intact while making each release signal
traceable.

## Review cadence

For every 0.3.x release, the maintainer records a release-health issue at 24 hours, 72 hours, and
weekly for the first four weeks. Each review records the denominator, observation window, source
query/artifact, result, and follow-up issue. An empty issue count is recorded as “no reports”, not
as a 100% success claim.

## Signal map

| Signal | Evidence source | Review |
| --- | --- | --- |
| Install, dependency and upgrade outcomes | `installation.yml` stage/result plus release asset download/check results | Count success reports and failures by stage; reproduce every distinct failure |
| discovered candidate / confirmed / rejected / unresolved | Doctor JSON `discovery` | Aggregate only reports that include a reviewed Doctor payload |
| unknown record family rate | Doctor `instances[].unknown_events` and `protocol_compatibility` | Group by observed Codex minor; open a compatibility fixture issue for every new family |
| protocol-uncertain sessions | Doctor `instances[].protocol_uncertainty` | Sessions with uncertainty / all reported sessions |
| incomplete completeness axes | Doctor `instances[].state_completeness` | Count session-axis samples and group their bounded reasons |
| Terminal eligible / associated / ambiguous / conflicting / unresolved | Runtime issue Doctor payload and public snapshot `terminal_association` | Compare counts by Codex minor; any conflict gets a replay fixture |
| false attention, false stall and missed signal | `runtime.yml` outcome category | Require observed/expected state and a minimal reproduction |
| full sample p50/p95/p99, memory and budget exceeded | CI `performance-pty` artifact from `tools/check_performance.py` | Compare against `tools/performance_thresholds.json` and the previous release |
| fast backlog, skipped/coalesced ticks and snapshot stale | Benchmark artifact plus Doctor `observer`/`rollout.activity` | Investigate threshold failure or two independent freeze reports |
| TUI freeze, crash, focus, scroll, resize and search | PTY artifact plus `runtime.yml` | Preserve terminal dimensions and frontend environment in reproduction |
| Codex-version correlation | issue `codex_version` plus Doctor observed versions | Group all compatibility/runtime findings by current and previous minor |
| CLI migration and removed-feature demand | runtime issue category | Count distinct users and workflows; do not infer demand from reactions alone |

CI checks the current 20/50-session benchmark, rollout burst, Terminal churn, file tail and the
wide/narrow/resize/scroll/focus/search PTY matrix. Release review links the exact CI run rather than
copying untraceable numbers.

## Triage rules

1. A new unknown family, association conflict, false attention or false confirmed stall gets an
   anonymous fixture before a parser/state change.
2. An install or upgrade failure blocking a supported Python/Linux combination is release-blocking
   for the next patch.
3. A performance threshold failure blocks publishing. User-reported freezes are reproduced with
   the same terminal dimensions and Codex minor.
4. Removed features return only after a concrete workflow and evidence from distinct users; raw
   request count alone is not a design decision.
5. Reports are manually checked for paths, identifiers, task text, endpoints and secrets before
   excerpts become fixtures or release-health records.
