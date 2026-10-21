"""Plan OpenAI Responses deployments without consuming provisioned capacity."""

import copy
import math
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from .azure import account_path, az_json
from .config import https_url
from .models import CS_VERSION


def initialize(c, openai_endpoint=None):
    """Derive an independent API profile from an existing customer foundation."""
    result = copy.deepcopy(c)
    for key in ("model_provider_data", "conversation_import"):
        result.pop(key, None)
    result.update(
        client_type="codex",
        profile_name="Codex on Azure",
        api_id="codex-foundry",
        api_display_name="Codex via Azure Foundry",
        api_path="codex",
        models=[],
    )
    result["gateway_url"] = urlunsplit(
        ("https", urlsplit(c["gateway_url"]).netloc, "/codex/v1", "", "")
    )
    endpoint = openai_endpoint or f"https://{c['foundry_name']}.openai.azure.com"
    result["foundry_url"] = https_url(endpoint, "OpenAI endpoint") + "/openai"
    return result


def sync_models(c, azure):
    plan = discover(c, azure)
    previous = {(m["name"], m.get("version")): m for m in c.get("models", [])}
    c["models"] = [
        dict(
            previous.get((p["deployment_name"], p["version"]), {}),
            name=p["deployment_name"],
            model=p["name"],
            version=p["version"],
        )
        for p in plan
        if p["action"] == "keep"
    ]
    if not c["models"]:
        raise ValueError("No succeeded text Responses deployments were found")
    names = {m["name"] for m in c["models"]}
    if c.get("default_model") not in names:
        c["default_model"] = next(
            (n for n in ("gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.4") if n in names),
            c["models"][0]["name"],
        )
    return c


def plan_models(
    catalog, quotas, deployments, capacity=10, now=None, model_names=None, sku_name=None
):
    if type(capacity) is not int or capacity < 1:
        raise ValueError("capacity must be a positive integer")
    if sku_name not in {None, "GlobalStandard", "DataZoneStandard", "Standard"}:
        raise ValueError("Codex deployment requires a metered Standard SKU")
    now = now or datetime.now(UTC)
    remaining = {q["name"]["value"]: max(0, q["limit"] - q["currentValue"]) for q in quotas}
    existing = {}
    occupied = {d["name"] for d in deployments}
    for d in deployments:
        if d["properties"]["model"].get("format") == "OpenAI":
            existing.setdefault(d["properties"]["model"]["name"], []).append(d)
    names = sorted({m["name"] for m in catalog if m.get("format") == "OpenAI"})
    if model_names:
        if set(model_names) - set(names):
            raise ValueError("Requested model is absent from the regional OpenAI catalog")
        names = [n for n in names if n in model_names]
    plan = []
    for name in names:
        variants = [m for m in catalog if m.get("format") == "OpenAI" and m["name"] == name]
        live = [
            m
            for m in variants
            if m.get("lifecycleStatus") != "Deprecated"
            and not (
                m.get("deprecation", {}).get("inference")
                and datetime.fromisoformat(m["deprecation"]["inference"].replace("Z", "+00:00"))
                <= now
            )
        ]
        supported = [
            m
            for m in live
            if str(m.get("capabilities", {}).get("responses")).lower() == "true"
            and (
                name.startswith(("gpt-4", "gpt-5", "gpt-6", "o1", "o3", "o4", "codex-mini"))
                or name == "gpt-chat-latest"
            )
        ]
        item = {"name": name, "format": "OpenAI", "action": "skip"}
        plan.append(item)
        if not supported:
            item["reason"] = "retired" if not live else "not a text Responses model"
            continue
        current = next(
            (
                d
                for d in existing.get(name, [])
                if d["properties"].get("provisioningState") == "Succeeded"
            ),
            None,
        )
        if current:
            item.update(
                action="keep",
                deployment_name=current["name"],
                version=current["properties"]["model"]["version"],
                state="Succeeded",
                sku=current["sku"]["name"],
                capacity=current["sku"]["capacity"],
            )
            continue
        if name in occupied:
            item["reason"] = "deployment name is already occupied; review it manually"
            continue
        # Newest available version, with explicit metered SKU preference. Never PTU or batch.
        for m in sorted(supported, key=lambda v: v["version"], reverse=True):
            for candidate_sku in (
                (sku_name,) if sku_name else ("GlobalStandard", "DataZoneStandard", "Standard")
            ):
                sku = next(
                    (
                        s
                        for s in m.get("skus", [])
                        if s["name"] == candidate_sku
                        and "finetune" not in s.get("usageName", "").lower()
                    ),
                    None,
                )
                if not sku:
                    continue
                free = int(remaining.get(sku["usageName"], 0))
                limits = sku.get("capacity") or {}
                minimum, step = limits.get("minimum") or 1, limits.get("step") or 1
                requested = max(minimum, math.ceil(capacity / step) * step)
                allocation = min(requested, free, limits.get("maximum") or requested)
                allocation = int(allocation // step * step)
                allowed = limits.get("allowedValues")
                if allowed:
                    allocation = max((v for v in allowed if v <= allocation), default=0)
                if allocation < minimum:
                    continue
                remaining[sku["usageName"]] -= allocation
                item.update(
                    action="deploy",
                    version=m["version"],
                    deployment_name=name,
                    sku=candidate_sku,
                    capacity=allocation,
                    lifecycle=m.get("lifecycleStatus"),
                )
                break
            if item["action"] == "deploy":
                break
        if item["action"] == "skip":
            item["reason"] = "no available metered quota in this resource region"
    return plan


def discover(c, azure, capacity=10, model_names=None, sku_name=None):
    account = azure.request("GET", account_path(c), version=CS_VERSION)
    catalog = az_json(
        "cognitiveservices",
        "account",
        "list-models",
        "-n",
        c["foundry_name"],
        "-g",
        c["foundry_resource_group"],
        "--subscription",
        c["subscription_id"],
    )
    quota = az_json(
        "cognitiveservices",
        "usage",
        "list",
        "--location",
        account["location"],
        "--subscription",
        c["subscription_id"],
    )
    deployed = azure.request("GET", account_path(c) + "/deployments", version=CS_VERSION)["value"]
    return plan_models(
        catalog, quota, deployed, capacity, model_names=model_names, sku_name=sku_name
    )
