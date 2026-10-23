"""Attach portal administration to an existing Claude gateway without replacing policy."""

import copy
from uuid import NAMESPACE_URL, uuid5

from .azure import account_path, api_path
from .config import validate_client


def ensure_inference_identity(config, azure, apim):
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
        return
    azure.request(
        "PUT",
        scope
        + "/providers/Microsoft.Authorization/roleAssignments/"
        + str(uuid5(NAMESPACE_URL, scope + principal + role)),
        {
            "properties": {
                "principalId": principal,
                "principalType": "ServicePrincipal",
                "roleDefinitionId": role,
            }
        },
        version="2022-04-01",
    )


def prepare_shared(config, azure):
    result = copy.deepcopy(config)
    if result.get("client_type", "claude") != "claude":
        raise ValueError("The baseline portal manages a Claude API, not a Codex API")
    validate_client({**result, "auth_mode": "azure-cli"})
    base = api_path(result)
    api = azure.request("GET", base)["properties"]
    apim = azure.request("GET", base.split("/apis/")[0])
    expected_url = apim["properties"]["gatewayUrl"].rstrip("/") + "/" + result["api_path"]
    if (
        api["path"] != result["api_path"]
        or api["serviceUrl"].rstrip("/") != result["foundry_url"].rstrip("/")
        or expected_url != result["gateway_url"].rstrip("/")
    ):
        raise ValueError(
            "Organization API path, gateway URL and backend must match the existing API"
        )
    operations = {
        item["name"]: (item["properties"]["method"], item["properties"]["urlTemplate"])
        for item in azure.request("GET", base + "/operations")["value"]
    }
    required = {
        "messages": ("POST", "/v1/messages"),
        "count-tokens": ("POST", "/v1/messages/count_tokens"),
        "models": ("GET", "/v1/models"),
    }
    if any(operations.get(name) != route for name, route in required.items()):
        raise ValueError("The existing API must expose the expected Claude operations")
    diagnostic = azure.request("GET", base + "/diagnostics/applicationinsights")["properties"]
    if not diagnostic.get("loggerId"):
        raise ValueError(
            "Configure API Application Insights observability before attaching the portal"
        )
    ensure_inference_identity(result, azure, apim)
    return result
