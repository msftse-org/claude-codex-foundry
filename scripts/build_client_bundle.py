#!/usr/bin/env python3
"""Package an explicit allowlist of client files; never include local admin state."""

import argparse
from pathlib import Path

from claude_foundry.client_bundle import build_bundle, build_template
from claude_foundry.config import load_client


def build(profile, output, root):
    config = load_client(profile)
    content = build_bundle(config, build_template(root))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist/claude-foundry-macos.zip"))
    args = parser.parse_args()
    print(build(args.profile, args.output, Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    main()
