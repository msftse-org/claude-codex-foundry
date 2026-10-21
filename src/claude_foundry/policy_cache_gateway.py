"""Machine-only, version-keyed APIM snapshot caching; no admission decisions."""

import json
import re
import xml.etree.ElementTree as ET
from uuid import UUID

from .azure import api_path
from .gateway import element


def policy_xml(tenant, audience, reader_ids, origin_url, organization_id, caching_type="internal"):
    tenant, organization_id = str(UUID(tenant)), str(UUID(organization_id))
    reader_ids = [str(UUID(value)) for value in reader_ids]
    if not reader_ids or caching_type not in {"internal", "external", "prefer-external"}:
        raise ValueError("Configure trusted machine readers and a supported cache type")
    if not re.fullmatch(r"api://[0-9a-f-]{36}", audience):
        raise ValueError("Use the dedicated budget API audience")
    UUID(audience[6:])
    if not re.fullmatch(r"https://[a-z0-9-]+\.azurewebsites\.net", origin_url):
        raise ValueError("Use the fixed budget portal origin")
    root = ET.Element("policies")
    inbound = element(root, "inbound")
    auth = element(
        inbound,
        "validate-azure-ad-token",
        **{
            "tenant-id": tenant,
            "header-name": "Authorization",
            "output-token-variable-name": "readerJwt",
            "failed-validation-httpcode": "401",
            "failed-validation-error-message": "A trusted runtime identity is required",
        },
    )
    audiences = element(auth, "audiences")
    for value in [audience, audience[6:]]:
        element(audiences, "audience", value)
    claim = element(element(auth, "required-claims"), "claim", name="oid", match="any")
    for value in reader_ids:
        element(claim, "value", value)
    denied = element(
        element(inbound, "choose"),
        "when",
        condition='@(((Jwt)context.Variables["readerJwt"]).Claims.ContainsKey("scp"))',
    )
    element(
        element(denied, "return-response"),
        "set-status",
        code="401",
        reason="Machine identity required",
    )
    element(
        inbound,
        "set-variable",
        name="digest",
        value='@((string)context.Request.MatchedParameters["digest"])',
    )
    bad = element(
        element(inbound, "choose"),
        "when",
        condition='@(!System.Text.RegularExpressions.Regex.IsMatch((string)context.Variables["digest"], "^[0-9a-f]{64}$"))',
    )
    element(
        element(bad, "return-response"), "set-status", code="400", reason="Invalid policy digest"
    )
    prefix = "budget-policy-v1:" + tenant + ":" + organization_id + ":"
    element(
        inbound,
        "set-variable",
        name="policyCacheKey",
        value="@("
        + json.dumps(prefix)
        + ' + context.Deployment.ServiceName + ":" + context.Api.Id + ":" + (string)context.Variables["digest"])',
    )
    element(
        inbound,
        "cache-lookup-value",
        key='@((string)context.Variables["policyCacheKey"])',
        **{"variable-name": "policySnapshot", "caching-type": caching_type},
    )
    element(
        inbound,
        "rate-limit-by-key",
        calls="600",
        **{
            "renewal-period": "60",
            "counter-key": '@("policy-cache-" + ((Jwt)context.Variables["readerJwt"]).Claims.GetValueOrDefault("oid", ""))',
        },
    )
    hit = element(
        element(inbound, "choose"),
        "when",
        condition='@(context.Variables.ContainsKey("policySnapshot"))',
    )
    trace = element(hit, "trace", source="budget-policy-cache", severity="information")
    element(trace, "message", "Policy snapshot cache hit")
    response = element(hit, "return-response")
    element(response, "set-status", code="200", reason="OK")
    for header, value in [
        ("Content-Type", "application/json"),
        ("Cache-Control", "no-store"),
        ("x-budget-policy-cache", "hit"),
    ]:
        element(
            element(response, "set-header", name=header, **{"exists-action": "override"}),
            "value",
            value,
        )
    element(response, "set-body", '@((string)context.Variables["policySnapshot"])')
    element(inbound, "set-backend-service", **{"base-url": origin_url})
    element(
        inbound,
        "rewrite-uri",
        template='@("/api/internal/budget-policies/" + (string)context.Variables["digest"])',
        **{"copy-unmatched-params": "false"},
    )
    element(inbound, "authentication-managed-identity", resource=audience)
    element(
        element(root, "backend"), "forward-request", timeout="30", **{"buffer-response": "true"}
    )
    outbound = element(root, "outbound")
    ok = element(
        element(outbound, "choose"), "when", condition="@(context.Response.StatusCode == 200)"
    )
    element(
        ok,
        "cache-store-value",
        key='@((string)context.Variables["policyCacheKey"])',
        value="@(context.Response.Body.As<string>(preserveContent: true))",
        duration="3600",
        **{"caching-type": caching_type},
    )
    trace = element(ok, "trace", source="budget-policy-cache", severity="information")
    element(trace, "message", "Policy snapshot cache miss; store requested")
    element(
        element(
            outbound, "set-header", name="x-budget-policy-cache", **{"exists-action": "override"}
        ),
        "value",
        "miss",
    )
    element(
        element(outbound, "set-header", name="Cache-Control", **{"exists-action": "override"}),
        "value",
        "no-store",
    )
    element(root, "on-error")
    return ET.tostring(root, encoding="unicode")


def deploy(
    config,
    azure,
    *,
    api_id,
    origin_url,
    audience,
    reader_ids,
    organization_id,
    caching_type="internal",
):
    if not re.fullmatch(r"[a-z0-9-]{3,80}", api_id):
        raise ValueError("Use a valid owned budget API ID")
    xml = policy_xml(
        config["tenant_id"], audience, reader_ids, origin_url, organization_id, caching_type
    )
    base = api_path({**config, "api_id": api_id})
    existing = azure.request("GET", base)
    if (
        existing.get("properties", {}).get("description")
        != "Managed by claude-codex-foundry budget gateway preview."
    ):
        raise ValueError("Use an existing owned budget API")
    service = base.split("/apis/")[0]
    tier = azure.request("GET", service)["sku"]["name"]
    if caching_type == "internal" and tier == "Consumption":
        raise ValueError("Consumption requires a configured external cache")
    if (
        caching_type in {"external", "prefer-external"}
        and not azure.request("GET", service + "/caches")["value"]
        and (caching_type == "external" or tier == "Consumption")
    ):
        raise ValueError("Configure an external cache first")
    for operation, path, label in [
        (
            "policy-snapshot",
            "/v1/policy-snapshots/{digest}",
            "Policy snapshot cache (runtime identities only)",
        ),
        (
            "cache-status",
            "/v1/policy-cache/status",
            "Policy cache diagnostics (owners and administrators)",
        ),
    ]:
        azure.request(
            "PUT",
            base + "/operations/" + operation,
            {
                "properties": {
                    "displayName": label,
                    "method": "GET",
                    "urlTemplate": path,
                    "templateParameters": [{"name": "digest", "required": True, "type": "string"}]
                    if operation == "policy-snapshot"
                    else [],
                    "responses": [],
                }
            },
        )
    azure.request(
        "PUT",
        base + "/operations/policy-snapshot/policies/policy",
        {"properties": {"format": "rawxml", "value": xml}},
    )
    return {"api_id": api_id, "operation": "policy-snapshot", "caching_type": caching_type}
