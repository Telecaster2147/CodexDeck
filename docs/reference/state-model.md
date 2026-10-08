# State model

CodexDeck deliberately avoids one overloaded health enum. A session is a product of independent axes,
attention priority, observation quality, and supporting evidence.

## Independent axes

| Axis | Representative values | Primary authority |
| --- | --- | --- |
| Lifecycle | idle, waiting response, generating, running tool, compacting, failed, ended | protocol phase |
| Attention | approval, permissions, user input, MCP elicitation, auth elicitation | typed action request and resolution |
| Failure/recovery | failed, suspect, reconnecting, fallback, recovered | ordered protocol and detector evidence |
| Network | unknown, idle, active, suspect, stalled, closed | aggregate process flows |
| Silence | normal, quiet active, waiting upstream, quiet unknown, stall suspect, observer blind | current observation probes |
| Alert | informational, warning, critical transitions | derived from the other axes |

Keepalive, token usage, model configuration, an older failure, or one isolated TCP flow does not become
the current lifecycle phase.

## Lifecycle ordering

The latest trusted phase-bearing protocol event wins. Model progress, response, tool, and compaction
events can establish a live phase even when the bounded tail did not include `TURN_STARTED`.
`COMPACTING` remains current until compact completion/failure/abort or a terminal turn boundary.
Late and duplicate records are adjudicated by source identity, generation, event time, and monotonic
clear rules.

## Attention

Approval, permission, user input, MCP elicitation, and authentication requests normalize to
`AttentionRequest`. Newer explicit resolution, trusted progress, terminal turn events, or process exit
clear the request. Older or delayed requests do not resurrect after a newer clear.

The presentation attention queue is a projection of the snapshot. It prioritizes:

1. direct user interaction;
2. current failure;
3. confirmed stall;
4. active observer blind state.

The queue powers both overview labels and `]` navigation; it does not mutate session state.

## Recovery and silence

Recovery state remains separate from lifecycle. A session can be generating while the transport is
recovering, or idle after a historical failure. Network progress after a suspect/reconnecting window
creates an explicit recovery edge.

Silence classification asks why direct semantic progress is absent. It distinguishes normal quiet,
active local work, upstream waiting, incomplete evidence, suspected stall, and observer blindness.
Confirmed network stall requires repeated abnormal windows and no recent protocol progress.

## Evidence gaps

Per-axis completeness is a guardrail on negative conclusions. A lifecycle gap does not automatically
make network incomplete. Ordinary terminal retention trimming does not invalidate current ownership;
ambiguous, conflicting, unresolved, or lost private association state does.

Cold-start tail recovery is progressive. A trusted lifecycle baseline can restore lifecycle while
attention remains conservative until its own baseline or clear arrives.

## Derivation pipeline

`SessionStateMachine.derive()` is orchestration over focused stages:

1. select trusted events and construct a turn context;
2. derive attention and lifecycle;
3. derive failure/recovery and protocol uncertainty;
4. build bounded turn/tool/agent/compaction summaries;
5. assess silence and alerts;
6. attach stable reasons and an evidence timeline.

Presentation modules only render this result. Changes to state meaning require a normalized fixture,
adjudicated expectation, and output-contract tests.
