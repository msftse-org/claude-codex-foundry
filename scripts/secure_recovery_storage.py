#!/usr/bin/env python3
"""Lock and verify recovery-container retention before enabling ledger writers."""

import argparse
import json

from claude_foundry.azure import Azure

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    azure = Azure(args.subscription)
    path = f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}/providers/Microsoft.Storage/storageAccounts/{args.account}/blobServices/default/containers/budget-recovery/immutabilityPolicies/default"
    policy = azure.request("GET", path, version="2023-05-01")
    if policy["properties"]["immutabilityPeriodSinceCreationInDays"] < 35:
        raise RuntimeError("Recovery retention must cover at least 35 days")
    if policy["properties"]["state"] != "Locked":
        azure.request(
            "POST", path + "/lock", version="2023-05-01", headers={"If-Match": policy["etag"]}
        )
    policy = azure.request("GET", path, version="2023-05-01")["properties"]
    if policy["state"] != "Locked":
        raise RuntimeError("Recovery retention is not locked; do not enable writers")
    print(
        json.dumps(
            {
                "state": policy["state"],
                "retention_days": policy["immutabilityPeriodSinceCreationInDays"],
            }
        )
    )
