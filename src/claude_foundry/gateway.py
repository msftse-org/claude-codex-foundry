"""APIM policy generation; never replace service-wide policies or diagnostics."""

import json
import xml.etree.ElementTree as ET

from .azure import AzureError, api_path
from .config import TOKEN_RESOURCE, validate_client


def element(parent, tag, text=None, **attributes):
    result = ET.SubElement(parent, tag, attributes)
    result.text = text
    return result


def policy_xml(c):
    validate_client({**c, "auth_mode": "azure-cli"})
    root = ET.Element("policies")
    inbound = element(root, "inbound")
    element(inbound, "base")
    jwt = element(
        inbound,
        "validate-jwt",
        **{
            "header-name": "Authorization",
            "require-scheme": "Bearer",
            "require-expiration-time": "true",
            "require-signed-tokens": "true",
            "failed-validation-httpcode": "401",
            "failed-validation-error-message": "A valid organizational access token is required.",
            "output-token-variable-name": "callerJwt",
        },
    )
    element(
        jwt,
        "openid-config",
        url=f"https://login.microsoftonline.com/{c['tenant_id']}/.well-known/openid-configuration",
    )
    element(element(jwt, "audiences"), "audience", TOKEN_RESOURCE)
    issuers = element(jwt, "issuers")
    element(issuers, "issuer", f"https://sts.windows.net/{c['tenant_id']}/")
    element(issuers, "issuer", f"https://login.microsoftonline.com/{c['tenant_id']}/v2.0")
    claims = element(jwt, "required-claims")
    element(element(claims, "claim", name="tid", match="all"), "value", c["tenant_id"])
    if c.get("allowed_object_ids"):
        ids = element(claims, "claim", name="oid", match="any")
        for oid in c["allowed_object_ids"]:
            element(ids, "value", oid)
    element(inbound, "set-backend-service", **{"base-url": c["foundry_url"]})
    for header in ("x-api-key", "api-key", "Ocp-Apim-Subscription-Key"):
        element(inbound, "set-header", name=header, **{"exists-action": "delete"})
    posts = element(
        element(inbound, "choose"), "when", condition='@(context.Request.Method == "POST")'
    )
    element(
        posts,
        "set-variable",
        name="model",
        value='@{ try { return (string)context.Request.Body.As<JObject>(preserveContent: true)["model"] ?? ""; } catch { return ""; } }',
    )
    allowed = ", ".join(json.dumps(m["name"]) for m in c["models"])
    denied = element(
        element(posts, "choose"),
        "when",
        condition="@(!(new string[] { "
        + allowed
        + ' }).Contains((string)context.Variables["model"]))',
    )
    response = element(denied, "return-response")
    element(response, "set-status", code="400", reason="Unsupported model")
    element(
        element(response, "set-header", name="Content-Type", **{"exists-action": "override"}),
        "value",
        "application/json",
    )
    element(
        response,
        "set-body",
        json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Select a model from /v1/models.",
                },
            }
        ),
    )
    if c.get("telemetry", {}).get("enabled", False):
        trace = element(posts, "trace", source="claude-foundry", severity="information")
        element(trace, "message", "Claude inference request")
        element(trace, "metadata", name="model", value='@((string)context.Variables["model"])')
        element(
            trace,
            "metadata",
            name="principal_id",
            value='@(((Jwt)context.Variables["callerJwt"]).Claims.GetValueOrDefault("oid", "unknown"))',
        )
        element(trace, "metadata", name="request_id", value="@(context.RequestId.ToString())")
    if c.get("telemetry", {}).get("token_metrics", False):
        messages = element(
            element(posts, "choose"), "when", condition='@(context.Operation.Id == "messages")'
        )
        metric = element(messages, "llm-emit-token-metric", namespace="ClaudeFoundry")
        element(metric, "dimension", name="API ID")
        element(metric, "dimension", name="Model", value='@((string)context.Variables["model"])')
    element(
        element(root, "backend"), "forward-request", timeout="240", **{"buffer-response": "false"}
    )
    outbound = element(root, "outbound")
    element(outbound, "base")
    element(
        element(outbound, "set-header", name="x-claude-gateway", **{"exists-action": "override"}),
        "value",
        c["api_id"],
    )
    element(element(root, "on-error"), "base")
    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


def catalog_policy(c):
    catalog = {
        "data": [
            {
                "id": m["name"],
                "type": "model",
                "display_name": m["labelOverride"],
                "created_at": m.get("created_at", "2026-01-01T00:00:00Z"),
                "anthropic_family_tier": m["anthropicFamilyTier"],
                "is_family_default": m.get("isFamilyDefault", False),
            }
            for m in c["models"]
        ],
        "has_more": False,
        "first_id": c["models"][0]["name"],
        "last_id": c["models"][-1]["name"],
    }
    root = ET.Element("policies")
    inbound = element(root, "inbound")
    element(inbound, "base")  # Authentication must run before the static catalog.
    response = element(inbound, "return-response")
    element(response, "set-status", code="200", reason="OK")
    element(
        element(response, "set-header", name="Content-Type", **{"exists-action": "override"}),
        "value",
        "application/json",
    )
    element(response, "set-body", json.dumps(catalog))
    for section in ("backend", "outbound", "on-error"):
        element(element(root, section), "base")
    return ET.tostring(root, encoding="unicode")


def deploy(c, azure):
    base = api_path(c)
    policy = policy_xml(c)  # Validate before changing Azure.
    properties = {
        "displayName": c.get("profile_name", "Claude on Azure"),
        "path": c["api_path"],
        "protocols": ["https"],
        "serviceUrl": c["foundry_url"],
        "description": "Claude Messages API with Entra authentication and Foundry RBAC.",
    }
    try:
        azure.request("GET", base)
    except AzureError as error:
        if error.status != 404:
            raise
        azure.request("PUT", base, {"properties": {**properties, "subscriptionRequired": True}})
    else:
        # PATCH leaves customer-managed API settings and diagnostics intact.
        azure.request("PATCH", base, {"properties": properties})
    azure.request(
        "PUT", base + "/policies/policy", {"properties": {"format": "rawxml", "value": policy}}
    )
    for name, method, url in [
        ("messages", "POST", "/v1/messages"),
        ("count-tokens", "POST", "/v1/messages/count_tokens"),
        ("models", "GET", "/v1/models"),
    ]:
        azure.request(
            "PUT",
            base + "/operations/" + name,
            {
                "properties": {
                    "displayName": name,
                    "method": method,
                    "urlTemplate": url,
                    "templateParameters": [],
                    "responses": [],
                }
            },
        )
    azure.request(
        "PUT",
        base + "/operations/models/policies/policy",
        {"properties": {"format": "rawxml", "value": catalog_policy(c)}},
    )
    azure.request("PATCH", base, {"properties": {"subscriptionRequired": False}})
    return {"gateway_url": c["gateway_url"], "models": len(c["models"])}


def configure_observability(c, azure, enable_dimensions=False):
    """Reuse the connected logger at API scope, without editing service diagnostics."""
    base = api_path(c)
    path = base + "/diagnostics/applicationinsights"
    try:
        current = azure.request("GET", path)["properties"]
    except AzureError as error:
        if error.status != 404:
            raise
        current = azure.request(
            "GET", base.split("/apis/")[0] + "/diagnostics/applicationinsights"
        )["properties"]
    properties = {k: v for k, v in current.items() if v is not None}
    properties.update(metrics=True, verbosity="information")
    azure.request("PUT", path, {"properties": properties})
    if enable_dimensions:
        logger = azure.request("GET", current["loggerId"])
        component_id = logger["properties"].get("resourceId")
        if not component_id:
            raise RuntimeError(
                "The connected logger has no Application Insights resourceId; enable metric dimensions in its portal settings."
            )
        component = azure.request("GET", component_id, version="2020-02-02")
        body = {
            key: component[key]
            for key in ("location", "kind", "tags", "properties")
            if key in component
        }
        body["properties"]["CustomMetricsOptedInType"] = "WithDimensions"
        azure.request("PUT", component_id, body, version="2020-02-02")
    return {
        "logger_id": current["loggerId"],
        "scope": "api",
        "metrics": True,
        "verbosity": "information",
    }
