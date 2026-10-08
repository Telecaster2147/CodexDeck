# Network model

Network observation supports diagnosis but never replaces structured protocol lifecycle evidence.

## Sources and aggregation

`ss` and `/proc` provide connection state, queue sizes, bytes sent/received/acknowledged, retransmits,
and progress deltas. Socket evidence is aggregated across all flows belonging to the same stable
process identity. One idle or abnormal flow cannot override another flow that is progressing.

`ss` has no portable PID-only source contract used by this project, so the collector reads its stream
under hard byte, line, retained-record, and wall-time budgets, then keeps only target Codex PID records.
A timeout, overflow, non-zero exit, or stderr warning marks the stage incomplete and retains the last
complete snapshot with explicit stale age.

## Classification

Representative states are `UNKNOWN`, `IDLE`, `ACTIVE`, `SUSPECT`, `STALLED`, and `CLOSED`.

A confirmed stall requires two consecutive abnormal full-sample windows and no recent trusted protocol
progress. Queue growth, missing ACK progress, retransmits, and closed flows are supporting evidence;
none alone proves that the model lifecycle failed. Fresh activity on any relevant flow resets the stall
window and may generate a recovery edge.

## Sampling and freshness

Socket collection belongs only to the default two-second full sample. The 100 ms rollout refresh does
not call `ss` and preserves the previous socket observation window. A stale socket snapshot lowers only
network- and silence-dependent conclusions.

## Boundary

CodexDeck does not inspect packets, parse TLS ClientHello, proxy traffic, decrypt TLS, or capture
application payloads. Endpoints in interactive or structured output are treated as potentially
sensitive diagnostic data and should be reviewed before sharing.
