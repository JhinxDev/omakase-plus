"""Fresh source releases must not mistake a real output module for generated files."""

import subprocess
from pathlib import Path


def test_gitignore_keeps_runtime_output_package_but_excludes_local_artifacts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / ".gitignore").write_bytes((root / ".gitignore").read_bytes())
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    for name, ignored in [
        ("src/omakase/output/__init__.py", False),
        ("src/omakase/output/terminal.py", False),
        ("output/generated.png", True),
        (".env.local.secrets", True),
        ("src/omakase/__pycache__/module.pyc", True),
    ]:
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "--quiet", name], check=False
        )
        assert result.returncode == int(not ignored), name
