"""Release gates must be idempotent and fail before any publication on invalid input."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def run_fixture(self, *, published: bool = False, stale_tag: bool = False,
                    auth_failure: bool = False) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tools").mkdir()
            (root / "src/codexdeck").mkdir(parents=True)
            (root / "src/codexdeck/config.py").write_text('__version__ = "9.8.7"\n')
            shutil.copy(ROOT / "tools/release.sh", root / "tools/release.sh")
            binary = root / "bin"
            binary.mkdir()
            git = binary / "git"
            git.write_text(
                '#!/bin/sh\nif [ "$1" = rev-parse ]; then echo fixture-sha; '
                'elif [ "$STALE_TAG" = 1 ]; then printf "old-sha\\trefs/tags/v9.8.7\\n"; fi\n'
            )
            gh = binary / "gh"
            gh.write_text(
                '#!/bin/sh\n[ "$AUTH_FAILURE" = 1 ] && exit 1\n'
                '[ "$PUBLISHED" = 1 ] && echo v9.8.7\nexit 0\n'
            )
            for command in (git, gh):
                command.chmod(0o755)
            if stale_tag:
                (root / "docs/releases").mkdir(parents=True)
                (root / "docs/releases/v9.8.7.md").write_text("# CodexDeck 9.8.7\n")
            result = subprocess.run(
                ["bash", str(root / "tools/release.sh")],
                env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}",
                     "GITHUB_REPOSITORY": "owner/project", "GITHUB_SHA": "fixture-sha",
                     "PUBLISHED": str(int(published)), "STALE_TAG": str(int(stale_tag)),
                     "AUTH_FAILURE": str(int(auth_failure))},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            return result.returncode, result.stdout

    def test_published_version_skips_build_and_missing_notes(self) -> None:
        code, output = self.run_fixture(published=True)
        self.assertEqual(code, 0, output)
        self.assertIn("already published", output)

    def test_missing_notes_stops_release(self) -> None:
        code, _ = self.run_fixture()
        self.assertNotEqual(code, 0)

    def test_existing_tag_on_other_commit_stops_release(self) -> None:
        code, _ = self.run_fixture(stale_tag=True)
        self.assertNotEqual(code, 0)

    def test_auth_error_is_not_mistaken_for_missing_release(self) -> None:
        code, _ = self.run_fixture(auth_failure=True)
        self.assertNotEqual(code, 0)

    def test_ci_requires_all_gates_before_reusable_publication(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn(
            "needs: [lint-types, test, latest-smoke, performance-pty, supply-chain, build]", ci,
        )
        self.assertIn("uses: ./.github/workflows/release.yml", ci)
        release = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("ref: ${{ github.sha }}", release)
        self.assertIn("cancel-in-progress: false", release)
        self.assertIn("github.ref == 'refs/heads/main'", release)
        self.assertIn("--status completed", release)
        self.assertIn('length == 6 and all(.[]; .conclusion == "success")', release)
        self.assertNotIn("pull_request_target", release)
