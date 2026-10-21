#!/usr/bin/env python3
"""Add an authenticated APIM L2 cache operation to an owned budget preview API."""

import argparse
import json

from claude_foundry.azure import Azure
from claude_foundry.config import load_org
from claude_foundry.policy_cache_gateway import deploy

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--api-id", default="claude-budget-preview")
    parser.add_argument("--origin-url", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--reader-object-id", action="append", required=True)
    parser.add_argument("--organization-id", required=True)
    parser.add_argument(
        "--caching-type", choices=["internal", "external", "prefer-external"], default="internal"
    )
    args = parser.parse_args()
    config = load_org(args.config)
    print(
        json.dumps(
            deploy(
                config,
                Azure(config["subscription_id"]),
                api_id=args.api_id,
                origin_url=args.origin_url,
                audience=args.audience,
                reader_ids=args.reader_object_id,
                organization_id=args.organization_id,
                caching_type=args.caching_type,
            )
        )
    )
