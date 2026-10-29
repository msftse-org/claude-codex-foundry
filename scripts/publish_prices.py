#!/usr/bin/env python3
"""Apply public catalog prices to live, pinned deployments and publish the policy.

Uses the portal's audited API as the signed-in owner (Azure CLI). By default only
models without a reviewed price are added; --replace also refreshes existing ones.
Run from a deployment checkout: reads .local/deploy-state.json and .local/portal/.
"""

import argparse
import sys
from pathlib import Path

from claude_foundry.azure import Azure, account_path
from claude_foundry.config import read_json
from claude_foundry.models import CS_VERSION
from claude_foundry.portal_client import PortalClient, PortalError
from claude_foundry.price_catalog import catalog_prices

COMPARED = (
    "deployment_version",
    "max_input_tokens",
    "max_output_tokens",
    "input_ceiling_per_million",
    "output_ceiling_per_million",
    "rates",
    "long_context",
)


def plan(state, prices, replace):
    """Return the catalog prices that should be written to the portal draft."""
    current = {p["model"]: p for p in state.get("budget_prices", [])}
    changes = []
    for price in prices:
        existing = current.get(price["model"])
        if existing is None or (replace and any(existing.get(k) != price.get(k) for k in COMPARED)):
            changes.append(price)
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", type=Path, default=Path(".local"))
    parser.add_argument("--replace", action="store_true", help="Refresh existing reviewed prices")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan; change nothing")
    args = parser.parse_args()
    state_file = read_json(args.local / "deploy-state.json")
    org = read_json(args.local / "portal/organization.json")
    entra = read_json(args.local / "portal/entra.json")
    portal = PortalClient(state_file["portal_url"], entra["client_id"], org["tenant_id"])
    try:
        state = portal.state()
        if state["viewer"]["role"] != "owner":
            print("Stopped: only the portal owner can change enforcement prices.", file=sys.stderr)
            return 1
        deployed = Azure(org["subscription_id"]).request(
            "GET", account_path(org) + "/deployments", version=CS_VERSION
        )["value"]
        providers = {}
        for model in state["models"]:
            providers.setdefault(model["provider"], {"models": []})["models"].append(model)
        prices, skipped = catalog_prices(providers, deployed)
        changes = plan(state, prices, args.replace)
        for name, reason in skipped.items():
            print(f"skip    {name}: {reason}")
        for price in changes:
            print(f"price   {price['model']} ({price['note'][:60]})")
        if args.dry_run or not changes:
            print("No changes made." if args.dry_run else "Prices are already current.")
            return 0
        revision = state["revision"]
        for price in changes:
            revision = portal.change(revision, "budget_pricing", price)["revision"]
        result = portal.publish_budgets()
        print(f"Published {len(changes)} price(s). Active policy: {result['policy_hash']}")
        return 0
    except PortalError as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
