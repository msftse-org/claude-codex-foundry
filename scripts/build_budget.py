#!/usr/bin/env python3
"""Package the dedicated streaming budget runtime, excluding all customer files."""

import argparse
import subprocess
import zipfile
from pathlib import Path


def build(output, root):
    requirements = subprocess.check_output(
        ["uv", "export", "--frozen", "--extra", "portal", "--no-dev", "--no-emit-project"], cwd=root
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("requirements.txt", requirements)
        for filename in ["function_app.py", "host.json"]:
            bundle.write(root / "budget/functions" / filename, filename)
        for path in sorted((root / "src/claude_foundry").rglob("*.py")):
            if path.is_symlink():
                raise ValueError("Symlinks are not accepted")
            bundle.write(path, str(path.relative_to(root / "src")))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/claude-codex-budget-preview.zip"))
    print(build(parser.parse_args().output, Path(__file__).resolve().parents[1]))
