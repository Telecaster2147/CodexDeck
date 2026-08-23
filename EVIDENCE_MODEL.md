# Evidence model

CodexDeck is an observer. It combines evidence already readable by the current Linux user without
turning any single weak signal into a stronger claim than it supports. This document owns evidence
authority, identity, freshness, completeness, reason codes, and publication rules.

## Authority order

1. Structured Codex protocol phases determine lifecycle.
2. Process identity and liveness determine whether a local command still exists.
3. Persisted terminal records determine transcript capability and ownership.
4. Socket counters and queues support network diagnosis; they do not define lifecycle.
5. SQLite and configuration supplement identity, title, model, reasoning effort, and history.

A missing, stale, truncated, conflicting, or unknown source means **not observed**. It is not a
confirmed negative result. Collectors degrade only the axes that depend on them.

## Compound identity

| Domain | Identity |
| --- | --- |
| Instance | `(CODEX_HOME, CODEX_SQLITE_HOME)` |
| Session | instance + thread/session ID + real workspace |
| Process | `(pid, kernel start_time)` |
| Rollout | `(path, device, inode, generation)` |
| Terminal operation | session + process/root call identity + invocation |
| Socket flow | pid + fd when available + local endpoint + peer endpoint |

Display IDs, names, argv fragments, and workspace labels are never sufficient cross-source identity.
Rollout replace, truncate, or anchor mismatch opens a new generation. PID reuse is isolated by kernel
start time.

## Observation and event time

Normalized events retain three clocks:

- `source_timestamp`: producer time, used within a trusted source window and for display;
- `observed_at`: when CodexDeck sampled the record, used for freshness;
- `adjudicated_at`: monotonic state ordering after bounded clock-skew handling.

A full snapshot is a documented composite interval, not a claim that every source was sampled at one
instant. Fast refresh updates rollout/terminal windows while retaining the last full process, SQLite,
and socket windows. Temporal skew is explicit and bounded.

## Per-axis completeness

Each session publishes independent completeness for:

- lifecycle;
- attention;
- failure/recovery;
- terminal ownership;
- network;
- silence.

A cold bounded tail starts as context-truncated. Trusted current baselines restore only the axes they
support. Oversize record skips, copy-truncate, source generation changes, collector blind periods,
and association conflicts remain explicit gaps. The public 500-event retention limit does not erase
current state: an internal bounded axis-baseline ledger retains authoritative current facts.

## Reasons and evidence timeline

Every operational conclusion has stable `reason.code`, bounded detail, source, confidence, and time.
Codes are derived from domain state, not presentation wording. TUI Diagnosis, text, JSON, doctor, and
single-session export use the same reason projection.

The evidence timeline is deterministic and bounded to the evidence relevant to the current conclusion.
It may contain protocol phases, completeness gaps, collector state, network support, and terminal
association summaries. It never contains terminal transcript bodies.

## Publication boundary

`MonitorSnapshot` is the published contract. `SnapshotPublisher` and fast refresh structurally freeze
the reachable object graph:

- sequences become immutable tuples;
- mappings reject mutation;
- published domain DTOs reject assignment;
- changed branches are copied;
- unchanged published branches may be reused by identity.

Collector cursors and state ledgers stay mutable behind this boundary. A later sample must not change
anything reachable from an older snapshot.

## Unknown protocol data

Unknown record families are counted with bounded cardinality. Diagnostic samples retain only redacted,
bounded shape summaries. Unknown semantic lifecycle, attention, or terminal records lower the related
confidence until newer known evidence restores it. Auxiliary unknown telemetry does not replace a
newer known phase.
