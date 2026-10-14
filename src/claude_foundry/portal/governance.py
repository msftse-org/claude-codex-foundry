"""Compile reviewed portal drafts to APIM policy, outside the inference path."""

import hashlib
import json
import xml.etree.ElementTree as ET

from ..config import TOKEN_RESOURCE
from ..gateway import catalog_policy, element, policy_xml


def deny(parent, condition, message):
    branch = element(element(parent, "choose"), "when", condition=condition)
    response = element(branch, "return-response")
    element(response, "set-status", code="403", reason="Access denied")
    element(
        element(response, "set-header", name="Content-Type", **{"exists-action": "override"}),
        "value",
        "application/json",
    )
    element(
        response,
        "set-body",
        json.dumps({"type": "error", "error": {"type": "permission_error", "message": message}}),
    )


def compile_policy(config, state):
    c = {**config, "allowed_object_ids": []}
    root = ET.fromstring(policy_xml(c))
    inbound = root.find("inbound")
    fragment = ET.Element("inbound")
    # Only policy data, never names or email addresses, is embedded in APIM.
    group_keys = {
        g["id"]: str(i) for i, g in enumerate(sorted(state["groups"], key=lambda g: g["id"]))
    }
    groups = {
        group_keys[g["id"]]: {
            "id": g["id"],
            "m": sorted(set(g["model_ids"])),
            "q": g["monthly_tokens"],
            "d": g["default_model"],
        }
        for g in sorted(state["groups"], key=lambda g: g["id"])
    }
    # Compact references keep a 2,000-person organization within APIM's policy size.
    # Quota counters still use the stable group UUID, never the serialization index.
    members = {
        m["id"]: [group_keys[m["group_id"]], m["monthly_tokens"], sorted(set(m["extra_models"]))]
        for m in sorted(state["members"], key=lambda m: m["id"])
        if m["status"] == "active"
    }
    encoded = json.dumps({"g": groups, "m": members}, separators=(",", ":"))
    element(fragment, "set-variable", name="governance", value="@(" + json.dumps(encoded) + ")")
    element(
        fragment,
        "set-variable",
        name="callerId",
        value='@(((Jwt)context.Variables["callerJwt"]).Claims.GetValueOrDefault("oid", ""))',
    )
    deny(
        fragment,
        '@(JObject.Parse((string)context.Variables["governance"])["m"][(string)context.Variables["callerId"]] == null)',
        "Your organization has not granted access, or your access is suspended.",
    )
    element(
        fragment,
        "set-variable",
        name="memberPolicy",
        value='@(JObject.Parse((string)context.Variables["governance"])["m"][(string)context.Variables["callerId"]].ToString(Newtonsoft.Json.Formatting.None))',
    )
    element(
        fragment,
        "set-variable",
        name="groupKey",
        value='@((string)JArray.Parse((string)context.Variables["memberPolicy"])[0])',
    )
    element(
        fragment,
        "set-variable",
        name="groupPolicy",
        value='@(JObject.Parse((string)context.Variables["governance"])["g"][(string)context.Variables["groupKey"]].ToString(Newtonsoft.Json.Formatting.None))',
    )
    element(
        fragment,
        "set-variable",
        name="groupId",
        value='@((string)JObject.Parse((string)context.Variables["groupPolicy"])["id"])',
    )
    element(
        fragment,
        "set-variable",
        name="allowedModels",
        value='@{ var g = (JArray)JObject.Parse((string)context.Variables["groupPolicy"])["m"]; var m = (JArray)JArray.Parse((string)context.Variables["memberPolicy"])[2]; return new JArray(g.Concat(m).Select(x => (string)x).Distinct()).ToString(Newtonsoft.Json.Formatting.None); }',
    )
    posts = element(
        element(fragment, "choose"), "when", condition='@(context.Request.Method == "POST")'
    )
    deny(
        posts,
        '@{ try { var model = (string)context.Request.Body.As<JObject>(preserveContent: true)["model"]; return !JArray.Parse((string)context.Variables["allowedModels"]).Values<string>().Contains(model); } catch { return true; } }',
        "This model is not allowed for your group. Request access from your organization administrator.",
    )
    settings = state["settings"]
    if settings["member_requests_per_minute"]:
        element(
            posts,
            "rate-limit-by-key",
            calls=str(settings["member_requests_per_minute"]),
            **{
                "renewal-period": "60",
                "counter-key": '@("claude-requests:" + context.Api.Id + ":" + (string)context.Variables["callerId"])',
            },
        )
    messages = element(
        element(posts, "choose"), "when", condition='@(context.Operation.Id == "messages")'
    )
    for scope, quota in [
        ("organization", str(settings["monthly_tokens"])),
        ("group", '@((long)JObject.Parse((string)context.Variables["groupPolicy"])["q"])'),
        ("member", '@((long)JArray.Parse((string)context.Variables["memberPolicy"])[1])'),
    ]:
        suffix = {
            "organization": '"organization"',
            "group": '(string)context.Variables["groupId"]',
            "member": '(string)context.Variables["callerId"]',
        }[scope]
        if scope == "organization" and not settings["monthly_tokens"]:
            continue
        parent = messages
        if scope != "organization":
            parent = element(
                element(messages, "choose"),
                "when",
                condition=quota[:-1] + " > 0)",
            )
        element(
            parent,
            "llm-token-limit",
            **{
                "counter-key": f'@("claude-quota:{scope}:" + context.Api.Id + ":" + {suffix})',
                "token-quota": quota,
                "token-quota-period": "Monthly",
                "estimate-prompt-tokens": "true",
            },
        )
    if settings["member_tokens_per_minute"]:
        element(
            messages,
            "llm-token-limit",
            **{
                "counter-key": '@("claude-tpm:" + context.Api.Id + ":" + (string)context.Variables["callerId"])',
                "tokens-per-minute": str(settings["member_tokens_per_minute"]),
                "estimate-prompt-tokens": "true",
            },
        )
    element(fragment, "authentication-managed-identity", resource=TOKEN_RESOURCE)
    for metric in root.findall(".//llm-emit-token-metric"):
        element(
            metric,
            "dimension",
            name="Principal ID",
            value='@((string)context.Variables["callerId"])',
        )
        element(
            metric, "dimension", name="Group ID", value='@((string)context.Variables["groupId"])'
        )
    # Authentication executes before governance; governance precedes all backend calls.
    index = next(i for i, e in enumerate(inbound) if e.tag == "set-backend-service")
    for item in reversed(list(fragment)):
        inbound.insert(index, item)
    catalog = ET.fromstring(catalog_policy(c))
    body = catalog.find("inbound/return-response/set-body")
    literal = json.dumps(body.text)
    body.text = (
        "@{ var catalog = JObject.Parse("
        + literal
        + '); var allowed = JArray.Parse((string)context.Variables["allowedModels"]).Values<string>().ToArray(); var models = new JArray(((JArray)catalog["data"]).Where(m => allowed.Contains((string)m["id"]))); catalog["data"] = models; catalog["first_id"] = models.Count > 0 ? models[0]["id"] : null; catalog["last_id"] = models.Count > 0 ? models[models.Count - 1]["id"] : null; return catalog.ToString(Newtonsoft.Json.Formatting.None); }'
    )
    policy = ET.tostring(root, encoding="unicode")
    if len(policy.encode()) > 250_000:
        raise ValueError(
            "This policy exceeds the portal's 250 KB safety limit. Split the gateway scope before adding more members."
        )
    return policy, ET.tostring(catalog, encoding="unicode")


def impact(config, state):
    policy, catalog = compile_policy(config, state)
    return {
        "revision": state["revision"],
        "active_members": sum(m["status"] == "active" for m in state["members"]),
        "suspended_members": sum(m["status"] == "suspended" for m in state["members"]),
        "groups": len(state["groups"]),
        "policy_bytes": len(policy.encode()),
        "policy_hash": hashlib.sha256((policy + catalog).encode()).hexdigest(),
        "managed_identity": True,
        "monthly_tokens": state["settings"]["monthly_tokens"],
        "note": "Applies to new gateway requests after Azure propagates the policy. In-flight requests may finish. Token limits use estimates for streaming and can overshoot with concurrency. Direct Foundry access bypasses gateway policy.",
    }
