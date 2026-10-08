#!/usr/bin/env bash
# Publish only a new source version after the caller's complete CI gate.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${GITHUB_REPOSITORY:?}"
: "${GITHUB_SHA:?}"
test "$(git rev-parse HEAD)" = "$GITHUB_SHA"
version=$(python3 - <<'PY'
import ast
from pathlib import Path
for node in ast.parse(Path('src/codexdeck/config.py').read_text()).body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets
    ):
        print(ast.literal_eval(node.value))
        break
else:
    raise SystemExit('Source version not found')
PY
)
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
tag="v$version"
notes="docs/releases/$tag.md"
released=$(gh release list --repo "$GITHUB_REPOSITORY" --limit 100 \
  --json tagName,isDraft --jq '.[] | select(.isDraft == false) | .tagName')
if grep -Fxq "$tag" <<< "$released"; then
  echo "$tag already published; no release needed."
  exit 0
fi
test -s "$notes"
grep -Fx "# CodexDeck $version" "$notes"
# Existing tags are immutable: resume only when they identify this exact commit.
remote_tag=$(git ls-remote --tags origin "refs/tags/$tag" "refs/tags/$tag^{}")
if [[ -n "$remote_tag" ]]; then
  commit=$(awk '/\^\{\}$/ {print $1}' <<< "$remote_tag")
  if [[ -z "$commit" ]]; then commit=$(awk 'NR == 1 {print $1}' <<< "$remote_tag"); fi
  test "$commit" = "$GITHUB_SHA"
fi
uv sync --locked
uv build
wheel="dist/codexdeck-$version-py3-none-any.whl"
sdist="dist/codexdeck-$version.tar.gz"
test -f "$sdist"
uv run python - "$wheel" <<'PY'
import sys
from zipfile import ZipFile
with ZipFile(sys.argv[1]) as archive:
    names = set(archive.namelist())
    required = {
        'codexdeck/config.py', 'codexdeck/engine_sampling.py',
        'codexdeck/codex/terminal.py', 'codexdeck/presentation/tui/startup.py',
        'codexdeck/presentation/tui/codexdeck.tcss',
    }
    assert required <= names, f'Missing wheel files: {required - names}'
    assert not ({'config.py', 'engine.py'} & names), 'Unexpected top-level modules'
PY
uv venv --clear build/release-smoke
uv pip install --python build/release-smoke/bin/python "$wheel"
test "$(build/release-smoke/bin/codexdeck --version)" = "codexdeck $version"
build/release-smoke/bin/codexdeck --help >/dev/null
build/release-smoke/bin/codexdeck doctor --help >/dev/null
build/release-smoke/bin/codexdeck monitor --help >/dev/null
(
  cd dist
  sha256sum "$(basename "$wheel")" > "$(basename "$wheel").sha256"
  sha256sum -c "$(basename "$wheel").sha256"
)
if [[ -z "$remote_tag" ]]; then
  gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs" \
    -f ref="refs/tags/$tag" -f sha="$GITHUB_SHA" >/dev/null
fi
# Keep incomplete uploads private; retries repair the same draft, never a published release.
drafts=$(gh release list --repo "$GITHUB_REPOSITORY" --limit 100 \
  --json tagName,isDraft --jq '.[] | select(.isDraft == true) | .tagName')
if ! grep -Fxq "$tag" <<< "$drafts"; then
  gh release create "$tag" --repo "$GITHUB_REPOSITORY" --verify-tag --draft \
    --title "CodexDeck $version" --notes-file "$notes"
fi
gh release upload "$tag" "$wheel" "$wheel.sha256" "$sdist" \
  --repo "$GITHUB_REPOSITORY" --clobber
gh release edit "$tag" --repo "$GITHUB_REPOSITORY" --draft=false \
  --title "CodexDeck $version" --notes-file "$notes" --latest
gh release view "$tag" --repo "$GITHUB_REPOSITORY" --json url,assets,isDraft
