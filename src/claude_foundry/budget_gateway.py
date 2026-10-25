"""Standard APIM policy for the separately deployed, fail-closed budget service."""

import re
import xml.etree.ElementTree as ET
from uuid import UUID

from .config import TOKEN_RESOURCE
from .gateway import element


def policy_xml(tenant_id, budget_url, audience, provider):
    tenant_id = str(UUID(tenant_id))
    if provider not in {"anthropic", "openai"}:
        raise ValueError("Unsupported provider")
    if not re.fullmatch(r"https://[a-z0-9-]+\.azurewebsites\.net", budget_url):
        raise ValueError("Use the fixed HTTPS budget Function endpoint")
    if not re.fullmatch(r"api://[0-9a-f-]{36}", audience):
        raise ValueError("Use a dedicated budget API application audience")
    root = ET.Element("policies")
    inbound = element(root, "inbound")
    # Deliberately no inherited global policy that could route around admission.
    auth = element(
        inbound,
        "validate-azure-ad-token",
        **{
            "tenant-id": tenant_id,
            "header-name": "Authorization",
            "failed-validation-httpcode": "401",
            "failed-validation-error-message": "A valid organization token is required",
            "output-token-variable-name": "callerJwt",
        },
    )
    element(element(auth, "audiences"), "audience", TOKEN_RESOURCE)
    # Identity lookup and active membership are authoritative in the budget service.
    element(inbound, "set-header", name="x-budget-principal-id", **{"exists-action": "override"})
    element(
        inbound[-1],
        "value",
        '@(((Jwt)context.Variables["callerJwt"]).Claims.GetValueOrDefault("oid", ""))',
    )
    element(inbound, "set-header", name="x-budget-request-id", **{"exists-action": "override"})
    element(inbound[-1], "value", "@(context.RequestId.ToString())")
    for header in ["api-key", "x-api-key", "Ocp-Apim-Subscription-Key", "x-budget-group-id"]:
        element(inbound, "set-header", name=header, **{"exists-action": "delete"})
    element(inbound, "set-backend-service", **{"base-url": budget_url})
    element(
        inbound,
        "rewrite-uri",
        template=f'@("/{provider}/v1/" + context.Operation.Id)',
        **{"copy-unmatched-params": "false"},
    )
    element(inbound, "authentication-managed-identity", resource=audience)
    trace = element(
        inbound,
        "trace",
        source="claude-foundry" if provider == "anthropic" else "codex-foundry",
        severity="information",
    )
    element(trace, "message", "Budgeted inference request")
    element(
        trace,
        "metadata",
        name="model",
        value='@{ try { return (string)context.Request.Body.As<JObject>(preserveContent: true)["model"] ?? ""; } catch { return ""; } }',
    )
    element(
        trace,
        "metadata",
        name="principal_id",
        value='@(((Jwt)context.Variables["callerJwt"]).Claims.GetValueOrDefault("oid", ""))',
    )
    inference = element(
        element(inbound, "choose"),
        "when",
        condition='@(context.Operation.Id == "messages" || context.Operation.Id == "responses")',
    )
    metric = element(inference, "llm-emit-token-metric", namespace="ClaudeCodexFoundry")
    element(metric, "dimension", name="API ID")
    element(
        metric,
        "dimension",
        name="Model",
        value='@{ try { return (string)context.Request.Body.As<JObject>(preserveContent: true)["model"] ?? "Unknown"; } catch { return "Unknown"; } }',
    )
    backend = element(root, "backend")
    element(backend, "forward-request", timeout="240", **{"buffer-response": "false"})
    outbound = element(root, "outbound")
    attributed = element(
        element(outbound, "choose"),
        "when",
        condition='@(context.Response.Headers.ContainsKey("x-budget-group-id"))',
    )
    trace = element(attributed, "trace", source="budget-governance", severity="information")
    element(trace, "message", "Budget admission scope")
    element(
        trace,
        "metadata",
        name="group_id",
        value='@(context.Response.Headers.GetValueOrDefault("x-budget-group-id", ""))',
    )
    element(outbound, "set-header", name="Cache-Control", **{"exists-action": "override"})
    element(outbound[-1], "value", "no-store")
    element(root, "on-error")
    return ET.tostring(root, encoding="unicode")


def deploy(config, azure, budget_url, audience, provider, api_id, api_route):
    """Create a dedicated owned API; never replace baseline or portal APIs."""
    from .azure import AzureError, api_path

    if not re.fullmatch(r"[a-z0-9-]{3,80}", api_id) or not re.fullmatch(
        r"[a-z0-9-]{3,80}", api_route
    ):
        raise ValueError("Use a dedicated lowercase API ID and route")
    if api_id in {config["api_id"], "claude-foundry", "claude-foundry-preview", "codex-foundry"}:
        raise ValueError("Budget preview must use a new API ID")
    policy = policy_xml(config["tenant_id"], budget_url, audience, provider)
    base = api_path({**config, "api_id": api_id})
    marker = "Managed by claude-codex-foundry budget gateway preview."
    try:
        existing = azure.request("GET", base)
    except AzureError as error:
        if error.status != 404:
            raise
        existing = None
    if existing and existing.get("properties", {}).get("description") != marker:
        raise ValueError("Refusing to overwrite an API not owned by this budget deployment")
    properties = {
        "displayName": ("Claude" if provider == "anthropic" else "Codex")
        + " via Azure Foundry — budget preview",
        "description": marker,
        "path": api_route,
        "protocols": ["https"],
        "subscriptionRequired": True,
    }
    azure.request("PUT" if existing is None else "PATCH", base, {"properties": properties})
    azure.request(
        "PUT", base + "/policies/policy", {"properties": {"format": "rawxml", "value": policy}}
    )
    operation = "messages" if provider == "anthropic" else "responses"
    operations = [
        (operation, "POST", "/v1/" + operation),
        ("models", "GET", "/v1/models"),
        ("budget", "GET", "/v1/budget"),
    ]
    if provider == "anthropic":
        operations.append(("count-tokens", "POST", "/v1/messages/count_tokens"))
    for name, method, url in operations:
        azure.request(
            "PUT",
            base + "/operations/" + name,
            {
                "properties": {
                    "displayName": name,
                    "method": method,
                    "urlTemplate": url,
                    "responses": [],
                }
            },
        )
    azure.request("PATCH", base, {"properties": {"subscriptionRequired": False}})
    return {"api_id": api_id, "path": api_route, "provider": provider}
