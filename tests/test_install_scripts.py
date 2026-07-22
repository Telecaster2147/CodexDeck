from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

from config import VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InstallScriptTests(unittest.TestCase):
    def test_scripts_are_executable_and_valid_posix_shell(self) -> None:
        for name in ("install.sh", "uninstall.sh"):
            path = PROJECT_ROOT / name
            self.assertTrue(os.access(path, os.X_OK), name)
            result = subprocess.run(
                ["sh", "-n", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_documents_install_and_uninstall_boundaries(self) -> None:
        install = subprocess.run(
            [str(PROJECT_ROOT / "install.sh"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        uninstall = subprocess.run(
            [str(PROJECT_ROOT / "uninstall.sh"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("--checksum", install)
        self.assertIn("--install-root", install)
        self.assertIn("--purge-config", uninstall)

        script = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
        link_position = script.index('ln -sfn "$INSTALL_ROOT/current/bin/codexdeck"')
        final_check_position = script.index('"$BIN_DIR/codexdeck" --version')
        self.assertLess(link_position, final_check_position)

    def test_readme_and_package_metadata_publish_mit_install_contract(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("## 许可证", readme)
        self.assertIn(f"codexdeck-{VERSION}-py3-none-any.whl.sha256", readme)
        self.assertNotIn("sha256sum dist/codexdeck-VERSION", readme)
        self.assertNotIn("## 开发与验证", readme)
        self.assertIn('license = "MIT"', pyproject)
        self.assertIn('license-files = ["LICENSE"]', pyproject)

    def test_relative_install_paths_keep_venv_entrypoint_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            wheel = root / f"codexdeck-{VERSION}-py3-none-any.whl"
            wheel.write_bytes(b"fixture wheel")
            checksum = root / f"{wheel.name}.sha256"
            checksum.write_text(
                f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n",
                encoding="ascii",
            )

            fake_python = fake_bin / "python3"
            fake_python.write_text(
                """#!/bin/sh
if [ "$1" = "-c" ]; then
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
    chmod +x "$3/bin/python"
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
    command_path="$(dirname "$0")/codexdeck"
    printf '#!%s\n' "$0" > "$command_path"
    printf 'fixture\n' >> "$command_path"
    chmod +x "$command_path"
    exit 0
fi
case "$1" in
    */codexdeck) printf 'codexdeck VERSION_PLACEHOLDER\n'; exit 0 ;;
esac
exit 1
""".replace("VERSION_PLACEHOLDER", VERSION),
                encoding="ascii",
            )
            fake_python.chmod(0o755)

            environment = {
                **os.environ,
                "HOME": str(home),
                "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "config"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
            }
            install = subprocess.run(
                [
                    str(PROJECT_ROOT / "install.sh"),
                    "--wheel",
                    str(wheel),
                    "--checksum",
                    str(checksum),
                    "--install-root",
                    "install-data",
                    "--bin-dir",
                    "command-bin",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                cwd=root,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            command = root / "command-bin" / "codexdeck"
            version = subprocess.run(
                [str(command), "--version"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), f"codexdeck {VERSION}")

            uninstall = subprocess.run(
                [
                    str(PROJECT_ROOT / "uninstall.sh"),
                    "--install-root",
                    "install-data",
                    "--bin-dir",
                    "command-bin",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                cwd=root,
            )
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertFalse(command.exists())

    def test_built_artifacts_include_license_and_source_installers(self) -> None:
        wheel = PROJECT_ROOT / "dist" / f"codexdeck-{VERSION}-py3-none-any.whl"
        source = PROJECT_ROOT / "dist" / f"codexdeck-{VERSION}.tar.gz"
        if not wheel.is_file() or not source.is_file():
            self.skipTest("build artifacts are not present")

        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith("/licenses/LICENSE") for name in names))
            metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
            metadata = archive.read(metadata_name).decode("utf-8")
            self.assertIn("License-Expression: MIT", metadata)

        with tarfile.open(source, "r:gz") as archive:
            basenames = {Path(name).name for name in archive.getnames()}
            self.assertIn("install.sh", basenames)
            self.assertIn("uninstall.sh", basenames)
            self.assertIn("LICENSE", basenames)


if __name__ == "__main__":
    unittest.main()
