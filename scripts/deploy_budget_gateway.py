#!/usr/bin/env python3
"""Deploy dedicated budget preview APIs. Existing client endpoints are preserved."""

import argparse
import json

from claude_foundry.azure import Azure
from claude_foundry.budget_gateway import deploy
from claude_foundry.config import load_org

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--budget-url", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--claude-api", default="claude-budget-preview")
    parser.add_argument("--codex-api", default="codex-budget-preview")
    args = parser.parse_args()
    config = load_org(args.config)
    azure = Azure(config["subscription_id"])
    for provider, name in [("anthropic", args.claude_api), ("openai", args.codex_api)]:
        print(
            json.dumps(deploy(config, azure, args.budget_url, args.audience, provider, name, name))
        )
