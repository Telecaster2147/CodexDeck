# Testing and release-quality gates

CodexDeck uses standard-library `unittest`, Textual Pilot tests, anonymous replay fixtures, real PTY
fixtures, static checks, bounded performance gates, and packaging smoke tests.

## Fast development loop

Choose checks by the behavior changed:

```bash
git diff --check
uvx ruff check CHANGED_PATHS
uv run python -m unittest tests.test_RELEVANT -v
```

Run state/temporal/replay contracts for evidence semantics, terminal/privacy contracts for association
or redaction, and Textual Pilot tests for navigation, focus, scroll, layout, sampling, or follow mode.

## Full release-candidate gate

```bash
uvx ruff check src tests tools
uvx mypy
uv run coverage run -m unittest discover -s tests -v
uv run coverage report --fail-under=85
uv run python tools/check_performance.py
uv run python tools/verify_pty.py
uv lock --check --offline
uv build
```

Inspect wheel/sdist contents and install the wheel into a clean environment before release. The command
smoke must cover `codexdeck --version`, `--help`, one-shot text/JSON, doctor, and import of split runtime
modules.

## Adjudicated ground truth

`tests/fixtures/ground_truth_manifest.json` contains independently classified positive, negative,
ambiguous, and unresolved cases across lifecycle, attention, terminal association, observer quality,
stall/silence, discovery, network, and recovery. Recognition changes begin with a redacted fixture and
an adjudicated expectation.

## Replay and repeatability

Replay reuses the production incremental rollout reader, normalizer, state machine, and terminal store.
Fixtures cover chunked append, partial records, invalid UTF-8, replace/copy-truncate, retention, known
families, and conservative unknown handling. Semantic repeatability and zero omitted updates are
correctness gates, not only performance measurements.

## Performance

`tools/benchmark_core.py` produces exploratory local measurements. `tools/check_performance.py` applies
versioned regression thresholds to 20/50-session sampling, fast refresh, rollout bursts, terminal churn,
and regular-file tail. Results distinguish runtime from `tracemalloc` overhead and report actual bytes
read, ingress ticks, read amplification, backlog, and omitted updates.

## Real PTY fixtures

`tools/verify_pty.py` exercises representative wide, narrow, resize, scroll, focus, search, and follow
flows under a real PTY. Generated JSON reports are CI artifacts and stay out of source control.

## Quality budgets

Ruff includes import/order, modernization, bugbear, simplification, security, and cyclomatic-complexity
rules. Mypy checks all runtime modules. `tools/quality_baseline.json` records reviewed remaining complex
functions and large-module limits; refactors should shrink rather than silently expand this baseline.
