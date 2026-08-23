# CodexDeck architecture and ownership

CodexDeck keeps collection, interpretation, publication, and presentation separate. The published
`MonitorSnapshot` is the boundary: readers and stores remain mutable and bounded, while every value
reachable from an older snapshot remains stable.

`SnapshotPublisher` and the fast-refresh publication path both call the same structural freezer.
Mutable collector/state-machine builders are cloned; reachable sequences become tuples, mappings
become mutation-blocking dict-compatible values, and mutable domain DTOs reject field assignment
after publication. Already-published unchanged branches are reused by identity, so freezing enforces
the contract without turning every incremental collector cache into an immutable structure.

## Runtime state ownership

`src/codexdeck/engine_state.py` is the checked ownership registry for every field created by
`MonitorEngine.__init__`. Each field has one owner, one lifecycle, and one publication rule.
The host-collector, per-instance sampling, and fast-refresh mixins declare the state they consume;
they do not create caches. `SnapshotPublisher` remains the only full-snapshot publisher.

The full-sample pipeline has explicit stages:

1. `engine_collectors.py` publishes bounded discovery and socket stage results;
2. `engine_sampling.py` resolves instance identity, enriches processes, reads logs, collects rollout
   and terminal evidence, derives sessions, and assembles one `InstanceSnapshot`;
3. `engine.py` retains exited sessions, prunes private state, and hands all instances to the publisher;
4. `engine_refresh.py` performs rollout-only copy-on-write refreshes between full samples.

Stage results are short-lived builders. None are published directly or own long-lived mutable state.

## Model layers

`src/codexdeck/models.py` remains the stable import facade, but its values fall into four reviewed layers:

1. **Domain identities and state:** instance/session/process/rollout/terminal/socket identities,
   lifecycle, attention, recovery, silence, confidence, completeness, provenance.
2. **Collector DTOs:** process, socket, thread, log, rollout activity and adapter health values.
   These may be assembled incrementally and must be copied before publication.
3. **Published contracts:** `SessionHealth`, `InstanceSnapshot`, `MonitorSnapshot`, temporal cut,
   observer health, diagnostics and bounded terminal summaries.
4. **Presentation projections:** navigation, attention queue and rendered text belong under
   `src/codexdeck/presentation/`; they are derived from snapshots and do not write domain state.

New presentation-only fields belong in a projection module rather than `models.py`. New collector
fields enter a snapshot only through an explicit publisher or `replace()` copy-on-write step.

`ProcessInfo` carries bounded collector evidence for source-terminal location (controlling TTY,
tmux pane, terminal program and SSH TTY). `presentation/source_location.py` is the sole projection
that interprets those values. Current cwd is not labeled as launch cwd, parent PID is only a clue,
and absent evidence produces an explicit insufficient-clues result.

| Candidate clue | Decision |
| --- | --- |
| PID / workspace / current cwd | Show; already authoritative process/workspace evidence |
| Controlling TTY | Show when `ps` provides a concrete value |
| Parent terminal | Do not assign separately; show parent PID and the child controlling TTY because the parent may have exited or changed |
| tmux pane | Show only an explicit bounded `TMUX_PANE` value |
| VS Code Remote / SSH | Label only explicit `TERM_PROGRAM=vscode` / `SSH_TTY` evidence |
| Launch cwd | Do not claim; `/proc/PID/cwd` is the current cwd, not retained launch history |

## Codex protocol boundaries

- `codex/events.py`: rollout record-family normalization.
- `codex/log_events.py`: supporting SQLite/SSE log families.
- `codex/rollout.py`: bounded incremental reader orchestration.
- `codex/rollout_types.py`: cursor lifecycle and read-result DTOs.
- `codex/protocol_families.py`: bounded observed/unknown family counters.
- `codex/terminal_protocol.py`: tool-call parsing and `TerminalUpdate` production.
- `codex/terminal.py`: association, retention, reconciliation and summary publication.
- `codex/file_tail.py`: regular-file eligibility, identity verification and tail reads.

Protocol changes start with an anonymous compatibility fixture and production replay. Splits must
preserve the same reader, normalizer, state-machine and TerminalStore path used in production.
