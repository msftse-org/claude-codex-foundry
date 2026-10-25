#!/usr/bin/env python3
"""Package an explicit allowlist of client files; never include local admin state."""

import argparse
from pathlib import Path

from claude_foundry.client_bundle import build_bundle, build_template
from claude_foundry.config import load_client


def build(profiles, output, root, *, budgeted=False):
    if isinstance(profiles, (str, Path)):
        profiles = [profiles]
    configs = [load_client(profile) for profile in profiles]
    content = build_bundle(configs, build_template(root), budgeted=budgeted)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        action="append",
        required=True,
        help="Client profile; repeat once to combine a Claude and a Codex profile",
    )
    parser.add_argument("--output", type=Path, default=Path("dist/claude-foundry-macos.zip"))
    parser.add_argument(
        "--budgeted", action="store_true", help="Include shared-runtime verification instructions"
    )
    args = parser.parse_args()
    print(
        build(
            args.profile, args.output, Path(__file__).resolve().parents[1], budgeted=args.budgeted
        )
    )


if __name__ == "__main__":
    main()
