"""Discover actual regional availability; deployment errors remain explicit."""

import re
import time
from datetime import UTC, datetime

from .azure import AzureError, account_path, az_json
from .config import write_json

CS_VERSION = "2025-10-01-preview"


def plan_models(
    catalog,
    quotas,
    deployments,
    capacity=10,
    now=None,
    *,
    sku_name="GlobalStandard",
    model_names=None,
):
    now = now or datetime.now(UTC)
    quota_map = {q["name"]["value"]: q for q in quotas}
    existing = {
        d["properties"]["model"]["name"]: d
        for d in deployments
        if d["properties"]["model"].get("format") == "Anthropic"
    }
    names = sorted({m["name"] for m in catalog if m.get("format") == "Anthropic"})
    if model_names:
        unknown = set(model_names) - set(names)
        if unknown:
            raise ValueError(f"Models not in the regional catalog: {', '.join(sorted(unknown))}")
        names = [name for name in names if name in model_names]
    plan = []
    for name in names:
        variants = [m for m in catalog if m["name"] == name and m.get("format") == "Anthropic"]
        supported = [m for m in variants if any(s["name"] == sku_name for s in m.get("skus", []))]
        model = max(
            supported or variants,
            key=lambda m: (
                m.get("capabilities", {}).get("hostedOn") == "azure",
                bool(m.get("isDefaultVersion")),
            ),
        )
        item = {
            "name": name,
            "version": model["version"],
            "sku": sku_name,
            "lifecycle": model.get("lifecycleStatus"),
            "hosted_on": model.get("capabilities", {}).get("hostedOn", "anthropic"),
        }
        expiry = model.get("deprecation", {}).get("inference")
        if model.get("lifecycleStatus") == "Deprecated" or (
            expiry and datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= now
        ):
            item.update(action="skip", reason="retired")
        elif name in existing:
            dep = existing[name]
            item.update(
                action="keep",
                deployment_name=dep["name"],
                version=dep["properties"]["model"]["version"],
                state=dep["properties"]["provisioningState"],
                sku=dep.get("sku", {}).get("name", "unknown"),
                capacity=dep.get("sku", {}).get("capacity"),
            )
        elif not supported:
            item.update(action="skip", reason=f"{sku_name} is not supported")
        else:
            sku = next(s for s in model["skus"] if s["name"] == sku_name)
            q = quota_map.get(sku["usageName"], {}) if sku else {}
            free = int(q.get("limit", 0) - q.get("currentValue", 0))
            if free <= 0:
                item.update(action="skip", reason="no available quota")
            else:
                item.update(action="deploy", capacity=min(capacity, free), deployment_name=name)
        plan.append(item)
    return plan


def discover(c, azure, capacity=10, *, sku_name="GlobalStandard", model_names=None):
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
        catalog, quota, deployed, capacity, sku_name=sku_name, model_names=model_names
    )


def apply(c, azure, plan, report_path, wait_seconds=600):
    report = []
    for item in plan:
        result = dict(item)
        if item["action"] == "deploy":
            body = {
                "sku": {"name": item["sku"], "capacity": item["capacity"]},
                "properties": {
                    "model": {
                        "format": "Anthropic",
                        "name": item["name"],
                        "version": item["version"],
                    }
                },
            }
            if c.get("model_provider_data"):
                body["properties"]["modelProviderData"] = c["model_provider_data"]
            try:
                # Concurrent provisioning inside the resource provider can return
                # an ETag conflict even though this client sends no If-Match header.
                # Retry the same named PUT; never create a second deployment.
                for attempt in range(4):
                    try:
                        response = azure.request(
                            "PUT",
                            account_path(c) + "/deployments/" + item["deployment_name"],
                            body,
                            version=CS_VERSION,
                        )
                        break
                    except AzureError as error:
                        if error.status != 412 or attempt == 3:
                            raise
                        print(f"{item['name']}: retrying Azure provisioning conflict", flush=True)
                        time.sleep(5 * (attempt + 1))
                result["state"] = response["properties"]["provisioningState"]
            except AzureError as error:
                result.update(state="Failed", error=str(error))
        report.append(result)
        write_json(report_path, report)
        print(
            f"{item['name']}: {result.get('state', result.get('reason', item['action']))}",
            flush=True,
        )
    deadline = time.monotonic() + wait_seconds

    def pending():
        return [r for r in report if r.get("state") in {"Creating", "Updating", "Accepted"}]

    while pending() and time.monotonic() < deadline:
        time.sleep(10)
        for item in pending():
            dep = azure.request(
                "GET",
                account_path(c) + "/deployments/" + item["deployment_name"],
                version=CS_VERSION,
            )
            item["state"] = dep["properties"]["provisioningState"]
            print(f"{item['name']}: {item['state']}", flush=True)
        write_json(report_path, report)
    return report


def sync_models(c, azure):
    deployments = azure.request("GET", account_path(c) + "/deployments", version=CS_VERSION)[
        "value"
    ]
    entries = []
    for deployment in deployments:
        if deployment["name"] in c.get("excluded_deployments", []):
            continue
        p = deployment["properties"]
        if p["model"].get("format") != "Anthropic" or p.get("provisioningState") != "Succeeded":
            continue
        tier = next(
            (
                t
                for t in ("sonnet", "opus", "haiku", "fable", "mythos")
                if f"-{t}-" in p["model"]["name"]
            ),
            None,
        )
        if not tier:
            continue
        model = p["model"]["name"]
        version = ".".join(re.findall(r"\d+", model))
        entries.append(
            {
                "name": deployment["name"],
                "labelOverride": f"Claude {tier.title()} {version}",
                "anthropicFamilyTier": tier,
                "isFamilyDefault": False,
                "_sort_version": tuple(int(v) for v in re.findall(r"\d+", model)),
                "created_at": deployment.get("systemData", {}).get(
                    "createdAt", "2026-01-01T00:00:00Z"
                ),
            }
        )
    entries.sort(key=lambda m: m.pop("_sort_version"), reverse=True)
    seen = set()
    for m in entries:
        tier = m["anthropicFamilyTier"]
        if tier not in seen:
            m["isFamilyDefault"] = True
            seen.add(tier)
    entries.sort(
        key=lambda m: (not (m["anthropicFamilyTier"] == "sonnet" and m["isFamilyDefault"]),)
    )
    if not entries:
        raise RuntimeError("No succeeded Anthropic deployments found; gateway was not changed")
    c["models"] = entries
    return c
