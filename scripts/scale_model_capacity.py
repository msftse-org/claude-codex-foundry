#!/usr/bin/env python3
"""Review or maximize existing metered deployments using currently unallocated quota."""

import argparse
import json
import time

from claude_foundry.azure import Azure, AzureError, account_path, az_json
from claude_foundry.config import load_org
from claude_foundry.model_capacity import plan_capacity
from claude_foundry.models import CS_VERSION


def discover(config, azure):
    account = azure.request("GET", account_path(config), version=CS_VERSION)
    catalog = az_json(
        "cognitiveservices",
        "account",
        "list-models",
        "-n",
        config["foundry_name"],
        "-g",
        config["foundry_resource_group"],
        "--subscription",
        config["subscription_id"],
    )
    quotas = az_json(
        "cognitiveservices",
        "usage",
        "list",
        "--location",
        account["location"],
        "--subscription",
        config["subscription_id"],
    )
    deployments = azure.request("GET", account_path(config) + "/deployments", version=CS_VERSION)[
        "value"
    ]
    return plan_capacity(catalog, quotas, deployments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true", help="Apply capacity-only PATCH requests")
    args = parser.parse_args()
    config = load_org(args.config)
    azure = Azure(config["subscription_id"])
    plan = discover(config, azure)
    for item in plan:
        if args.apply and item["action"] == "scale":
            # PATCH preserves model version, upgrade policy, provider attestation and all
            # other deployment properties. Azure rejects concurrent quota conflicts.
            path = account_path(config) + "/deployments/" + item["name"]
            for attempt in range(4):
                try:
                    result = azure.request(
                        "PATCH",
                        path,
                        {"sku": {"name": item["sku"], "capacity": item["target"]}},
                        version=CS_VERSION,
                    )
                    break
                except AzureError as error:
                    if error.status != 412 or attempt == 3:
                        raise
                    time.sleep(5 * (attempt + 1))
            deadline = time.monotonic() + 600
            while result["properties"]["provisioningState"] in {"Creating", "Updating", "Accepted"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Reconcile {item['name']} before retrying")
                time.sleep(2)
                result = azure.request("GET", path, version=CS_VERSION)
            item["state"] = result["properties"]["provisioningState"]
            if item["state"] != "Succeeded" or result["sku"]["capacity"] != item["target"]:
                raise RuntimeError(f"Capacity update failed verification: {item['name']}")
        print(json.dumps(item), flush=True)


if __name__ == "__main__":
    main()
