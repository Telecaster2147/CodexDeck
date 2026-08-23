# Codex protocol compatibility

Codex rollout, SQLite, and structured-log formats are upstream implementation surfaces. CodexDeck
supports observed families through production readers and conservative fallback behavior rather than
claiming that every future Codex release has a stable internal schema.

## Validated rolling window

The repository keeps two labeled fixture windows:

- current validated fixture minor: Codex CLI `0.145.x`;
- previous validated fixture minor: Codex CLI `0.144.x`.

"Current" means the current repository fixture window, not necessarily the newest upstream release.
Each window is exercised through the production incremental rollout reader, normalizer, replay, SQLite
reader, state machine, and terminal association store.

`codexdeck doctor` reports observed CLI versions, known/unknown family counts, schema capabilities, and
the repository's validated fixture minors.

## Family registry

Known record families have one production owner and at least one anonymous fixture. Diagnostic-only
handlers may count or summarize an unknown shape but do not claim lifecycle or ownership. Counters have
bounded cardinality and an explicit `other` bucket.

## Adding a new upstream shape

1. Record the producing Codex CLI version.
2. Reduce the sample to the smallest structural record.
3. Remove text, paths, endpoints, IDs, credentials, and local project names.
4. Add it to the compatibility manifest before changing the normalizer.
5. Adjudicate lifecycle, attention, terminal, and uncertainty effects.
6. Replay it through production incremental/chunked reading, including partial-line behavior.
7. Add downstream snapshot and machine-output expectations.
8. Update the family owner and rolling-window documentation.

Do not add ad-hoc string matching in engine or presentation layers.

## Compatibility policy

Core JSON/NDJSON uses `schema_version: 1`. Single-session export and doctor use their own schema
versions. Additive nullable fields with explicit privacy and bounds may retain a schema version.
Deleting, renaming, changing a type/nullability, or changing field meaning requires the relevant schema
version to advance.

Before 1.0, machine-output compatibility is promised within a CodexDeck minor line and breaking changes
are called out in release notes. Replay manifests are repository test assets rather than public APIs.

## Reporting a compatibility problem

Include the CodexDeck version, Codex CLI version, affected command/output surface, and a manually
reviewed/redacted `codexdeck doctor --format json` excerpt. Do not attach raw rollout files, transcript
bodies, tokens, endpoints, or unreviewed home paths.
