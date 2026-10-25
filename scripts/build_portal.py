#!/usr/bin/env python3
"""Build an allowlisted Azure Functions package; excludes customer configuration."""

import argparse
import subprocess
import zipfile
from pathlib import Path

from claude_foundry.client_bundle import build_template


def build(output, root):
    web = root / "portal/web"
    subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=web, check=True)
    subprocess.run(["npm", "run", "build"], cwd=web, check=True)
    requirements = subprocess.check_output(
        ["uv", "export", "--frozen", "--extra", "portal", "--no-dev", "--no-emit-project"], cwd=root
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("requirements.txt", requirements)
        template = zipfile.ZipInfo("client-bundle.zip")
        template.create_system = 3
        template.compress_type = zipfile.ZIP_DEFLATED
        template.external_attr = 0o100644 << 16
        bundle.writestr(template, build_template(root))
        for filename in ["function_app.py", "host.json"]:
            bundle.write(root / "portal/functions" / filename, filename)
        for path in sorted((root / "src/claude_foundry").rglob("*.py")):
            if path.is_symlink():
                raise ValueError(f"Symlinks are not accepted: {path}")
            bundle.write(path, str(path.relative_to(root / "src")))
        for path in sorted((web / "dist").rglob("*")):
            if path.is_file():
                if path.is_symlink() or path.suffix == ".map":
                    raise ValueError(f"Unexpected static asset: {path}")
                bundle.write(path, "static/" + str(path.relative_to(web / "dist")))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("dist/claude-foundry-admin-preview.zip")
    )
    args = parser.parse_args()
    print(build(args.output, Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    main()
