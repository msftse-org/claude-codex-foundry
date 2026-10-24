#!/usr/bin/env python3
"""Prepare the portal on the existing Claude route; isolation is explicitly optional."""

import argparse
from pathlib import Path

from prepare_portal_preview import prepare as prepare_isolated

from claude_foundry.azure import Azure
from claude_foundry.config import load_org, write_json
from claude_foundry.portal_setup import prepare_shared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--output", type=Path, default=Path(".local/portal/organization.json"))
    parser.add_argument(
        "--isolated", action="store_true", help="Use a separate preview API for testing"
    )
    args = parser.parse_args()
    if Path(args.organization).resolve() == args.output.resolve():
        raise ValueError(
            "Use a separate portal configuration output; preserve the source organization"
        )
    config = load_org(args.organization)
    azure = Azure(config["subscription_id"])
    result = (
        prepare_isolated(config, "claude-foundry-preview", "claude-preview", azure)
        if args.isolated
        else prepare_shared(config, azure)
    )
    write_json(args.output, result)
    print(
        f"Portal target: {result['gateway_url']}. Saved {args.output}. No governance was published."
    )


if __name__ == "__main__":
    main()
