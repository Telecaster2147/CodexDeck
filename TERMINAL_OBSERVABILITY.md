# Terminal observability

Terminal evidence is a separate bounded domain. CodexDeck does not construct a pseudo-terminal by
concatenating event details, and it does not attach to or consume a Codex PTY.

## Capabilities

| Capability | Meaning |
| --- | --- |
| `FILE_TAIL` | child fd 1/2 points to an eligible regular file in the workspace or `/tmp` |
| `POLL_TRANSCRIPT` | persisted initial yield or later poll/write output grows the transcript |
| `FINAL_TRANSCRIPT` | only completion or aggregate output is available |
| `METADATA_ONLY` | command/process metadata exists without readable output |

`STREAMING` is reserved for a future official read-only subscription source. Rollout poll records do
not receive this label.

## Association

Initial exec, background yield, later `write_stdin`/poll, and completion records are associated by:

1. explicit process ID;
2. rollout-scoped call ID;
3. bounded invocation order when identity remains unambiguous.

Parallel processes remain separate. Reused process/call IDs are isolated by session and rollout
generation. Conflicting process and call identities fail closed. Public summaries report eligible,
associated, ambiguous, conflicting, unresolved, and private-state-dropped counts.

## Regular-file tail

Only child fd 1/2 are considered. The collector verifies process start time, descriptor identity,
regular-file type, allowed root, inode/generation, and path again before reading. PTYs, pipes, sockets,
character devices, unrelated files, and the observer's own process tree are skipped.

## Retention

| Scope | Bound |
| --- | ---: |
| one terminal | 2 MiB or 4,000 chunks |
| one session | 16 terminals |
| all sessions | 16 MiB |

Old eligible output is trimmed first. Upstream truncation and CodexDeck trimming are distinct, dropped
bytes remain visible, and a system marker explains the boundary. UTF-8 is decoded incrementally across
reads; control sequences are removed before rendering.

## Privacy and output surfaces

Known bearer/basic credentials, common tokens, AWS access keys, JWTs, credential-bearing URLs/DSNs,
PEM private keys, and cookie headers receive best-effort redaction. Custom or context-free secret
formats may remain, so local transcript data and screenshots require human review.

Transcript bodies appear only in the in-memory TUI Terminal view. Text, JSON/NDJSON, doctor, and export
contain bounded summaries and never transcript bodies. Search operates only over retained memory; follow
mode starts only from the end.

## Source-terminal clues

Diagnosis may show PID, parent PID, controlling TTY, current cwd, and explicit tmux pane, VS Code, or
SSH environment clues. `/proc/PID/cwd` is current cwd, not proven launch cwd. Missing evidence is shown
as insufficient clues; CodexDeck does not switch panes, write stdin, send signals, or claim exact
terminal ownership without evidence.
