"""Dedicated Responses API on standard APIM. Never edit the Claude API."""

import json
import xml.etree.ElementTree as ET

from .azure import AzureError, api_path
from .config import validate_client
from .gateway import element
from .gateway import policy_xml as claude_policy


def policy_xml(c):
    validate_client(c)
    if c.get("client_type") != "codex":
        raise ValueError("A Codex organization profile is required")
    root = ET.fromstring(
        claude_policy(
            {
                **c,
                "telemetry": {
                    "enabled": c.get("telemetry", {}).get("enabled", False),
                    "token_metrics": False,
                },
            }
        )
    )
    for trace in root.findall(".//trace"):
        trace.set("source", "codex-foundry")
        trace.find("message").text = "Codex inference request"
    error = root.find(".//return-response/set-body")
    error.text = json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "code": "model_not_allowed",
                "message": "Select a published Codex model.",
            }
        }
    )
    header = root.find("outbound/set-header[@name='x-claude-gateway']")
    header.set("name", "x-codex-gateway")
    posts = root.find("inbound/choose/when")
    # The baseline is stateless. Do not permit user input to enable Azure response
    # storage/background jobs or use response IDs owned by a different caller.
    guard = element(
        element(posts, "choose"),
        "when",
        condition='@{ try { var b = context.Request.Body.As<JObject>(preserveContent: true); return (b["previous_response_id"] != null && b["previous_response_id"].Type != JTokenType.Null) || (b["conversation"] != null && b["conversation"].Type != JTokenType.Null) || (bool?)b["background"] == true; } catch { return true; } }',
    )
    response = element(guard, "return-response")
    element(response, "set-status", code="400", reason="Stateless Responses required")
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
                "error": {
                    "type": "invalid_request_error",
                    "message": "Use replayed input with store=false; stored conversations and background requests are not enabled.",
                }
            }
        ),
    )
    no_reasoning = ", ".join(
        json.dumps(m["name"])
        for m in c["models"]
        if m["model"].startswith("gpt-4") or m["model"] == "gpt-chat-latest"
    )
    element(
        posts,
        "set-body",
        '@{ var b = context.Request.Body.As<JObject>(preserveContent: true); b["store"] = false; if ((new string[] { '
        + no_reasoning
        + ' }).Contains((string)b["model"])) { b.Remove("reasoning"); } return b.ToString(); }',
    )
    if c.get("telemetry", {}).get("token_metrics", False):
        metric = element(posts, "llm-emit-token-metric", namespace="CodexFoundry")
        element(metric, "dimension", name="API ID")
        element(metric, "dimension", name="Model", value='@((string)context.Variables["model"])')
    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


def catalog_policy(c):
    root = ET.Element("policies")
    inbound = element(root, "inbound")
    element(inbound, "base")
    response = element(inbound, "return-response")
    element(response, "set-status", code="200", reason="OK")
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
                "object": "list",
                "data": [
                    {"id": m["name"], "object": "model", "owned_by": "organization"}
                    for m in c["models"]
                ],
            }
        ),
    )
    for section in ("backend", "outbound", "on-error"):
        element(element(root, section), "base")
    return ET.tostring(root, encoding="unicode")


def deploy(c, azure):
    policy = policy_xml(c)
    if c["api_id"] in {"claude-foundry", "claude-foundry-preview"} or c["api_path"] in {
        "claude",
        "claude-preview",
    }:
        raise ValueError("Codex requires a separate API ID and path")
    base = api_path(c)
    properties = {
        "displayName": c.get("api_display_name", "Codex via Azure Foundry"),
        "path": c["api_path"],
        "protocols": ["https"],
        "serviceUrl": c["foundry_url"],
        "description": "Stateless OpenAI Responses API for Codex with Entra authentication and Foundry RBAC.",
    }
    try:
        existing = azure.request("GET", base)
    except AzureError as error:
        if error.status != 404:
            raise
        azure.request("PUT", base, {"properties": {**properties, "subscriptionRequired": True}})
    else:
        if existing.get("properties", {}).get("description") != properties["description"]:
            raise ValueError("Existing API is not owned by Codex onboarding; choose a new API ID")
        azure.request("PATCH", base, {"properties": properties})
    azure.request(
        "PUT", base + "/policies/policy", {"properties": {"format": "rawxml", "value": policy}}
    )
    for name, method, path in [
        ("responses", "POST", "/v1/responses"),
        ("models", "GET", "/v1/models"),
    ]:
        azure.request(
            "PUT",
            base + "/operations/" + name,
            {
                "properties": {
                    "displayName": name,
                    "method": method,
                    "urlTemplate": path,
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
