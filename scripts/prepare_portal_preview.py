#!/usr/bin/env python3
"""Create an isolated APIM API for portal development; preserves the original API."""

import argparse
import copy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from claude_foundry.azure import Azure, AzureError, account_path, api_path
from claude_foundry.config import load_org, write_json
from claude_foundry.gateway import deploy


def prepare(config, api_id, api_route, azure):
    if not api_id.endswith("-preview") or not api_route.endswith("-preview"):
        raise ValueError("Use a separate API ID and route ending in -preview")
    if api_id == config["api_id"] or api_route == config["api_path"]:
        raise ValueError("The preview must not target the existing inference API")
    preview = copy.deepcopy(config)
    preview.update(
        api_id=api_id,
        api_path=api_route,
        api_display_name="Claude via Azure Foundry — Portal Preview",
        profile_name="Claude & Codex Preview",
        gateway_url=config["gateway_url"].rsplit("/", 1)[0] + "/" + api_route,
    )
    initialized = False
    try:
        existing = azure.request("GET", api_path(preview))
        if existing["properties"].get("displayName") != preview["api_display_name"]:
            raise ValueError("An unrelated API already uses this preview ID")
        # Never replace governance after the preview has already been initialized.
        initialized = True
    except AzureError as error:
        if error.status != 404:
            raise
    service = api_path(config).split("/apis/")[0]
    existing_apis = azure.request("GET", service + "/apis")["value"]
    if any(
        api["properties"]["path"] == api_route and api["name"] != api_id for api in existing_apis
    ):
        raise ValueError("This API route is already in use")
    if not initialized:
        deploy(preview, azure)
    diagnostics = azure.request("GET", api_path(config) + "/diagnostics/applicationinsights")
    azure.request(
        "PUT",
        api_path(preview) + "/diagnostics/applicationinsights",
        {"properties": diagnostics["properties"]},
    )
    apim = azure.request("GET", service)
    principal = apim.get("identity", {}).get("principalId")
    if not principal:
        raise ValueError("Enable the APIM service's system-assigned managed identity first")
    scope = account_path(config)
    role = f"/subscriptions/{config['subscription_id']}/providers/Microsoft.Authorization/roleDefinitions/a97b65f3-24c7-4388-baec-2e87135dc908"
    assignments = azure.request(
        "GET", scope + "/providers/Microsoft.Authorization/roleAssignments", version="2022-04-01"
    )["value"]
    if any(
        a["properties"]["principalId"] == principal
        and a["properties"]["roleDefinitionId"].lower() == role.lower()
        for a in assignments
    ):
        return preview
    assignment = str(uuid5(NAMESPACE_URL, scope + principal + role))
    azure.request(
        "PUT",
        scope + "/providers/Microsoft.Authorization/roleAssignments/" + assignment,
        {
            "properties": {
                "principalId": principal,
                "principalType": "ServicePrincipal",
                "roleDefinitionId": role,
            }
        },
        version="2022-04-01",
    )
    return preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--api-id", default="claude-foundry-preview")
    parser.add_argument("--api-route", default="claude-preview")
    parser.add_argument("--output", type=Path, default=Path(".local/portal/organization.json"))
    args = parser.parse_args()
    config = load_org(args.organization)
    preview = prepare(config, args.api_id, args.api_route, Azure(config["subscription_id"]))
    write_json(args.output, preview)
    print(f"Preview ready at {preview['gateway_url']}. Saved {args.output}.")


if __name__ == "__main__":
    main()
