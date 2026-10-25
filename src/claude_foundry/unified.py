"""Server-owned provider catalogs and reviewed model bounds for shared administration."""

import copy
from urllib.parse import urlsplit

from .config import validate_client


def provider_configs(base, values):
    result = {}
    names = set()
    for client, value in values.items():
        if client not in {"claude", "codex"} or value.get("client_type", "claude") != client:
            raise ValueError("Unknown or mismatched portal provider")
        profile = copy.deepcopy(value)
        validate_client(profile)
        for field in (
            "tenant_id",
            "subscription_id",
            "apim_name",
            "apim_resource_group",
            "foundry_name",
            "foundry_resource_group",
        ):
            if profile.get(field) != base.get(field):
                raise ValueError("Portal providers must share the same tenant and foundation")
        if urlsplit(profile["gateway_url"]).netloc != urlsplit(base["gateway_url"]).netloc:
            raise ValueError("Portal provider gateway must use the same API Management host")
        if client == "codex" and not all(m.get("qualified") is True for m in profile["models"]):
            raise ValueError("Portal Codex models must be qualified")
        for model in profile["models"]:
            if model["name"] in names:
                raise ValueError("Provider deployment names must be unique")
            names.add(model["name"])
        result[client] = profile
    return result


def catalog(providers):
    return [
        dict(
            copy.deepcopy(m),
            provider=client,
            labelOverride=m.get("labelOverride", m["name"]),
            anthropicFamilyTier=m.get("anthropicFamilyTier", "openai"),
        )
        for client, profile in providers.items()
        for m in profile["models"]
    ]


def reporting_config(base, providers):
    return {
        **base,
        "reporting_apis": [
            {"api_id": p["api_id"], "gateway_url": p["gateway_url"]} for p in providers.values()
        ],
    }


def reviewed_deployments(providers, prices, deployed):
    """Only independently reviewed, currently pinned, qualified versions reach runtime."""
    from .portal.budget_pricing import BudgetPrice

    available = {m["name"]: client for client, p in providers.items() for m in p["models"]}
    actual = {d["name"]: d["properties"] for d in deployed}
    result = {}
    for value in prices:
        price = BudgetPrice.model_validate(value)
        price.check_current()
        model = actual.get(price.model, {})
        provider = "openai" if available.get(price.model) == "codex" else "anthropic"
        if (
            price.model not in available
            or price.model in result
            or price.provider != provider
            or model.get("provisioningState") != "Succeeded"
            or model.get("model", {}).get("version") != price.deployment_version
            or model.get("model", {}).get("format")
            != ("OpenAI" if provider == "openai" else "Anthropic")
            or model.get("versionUpgradeOption") != "NoAutoUpgrade"
        ):
            raise ValueError(
                "Reviewed model policy must match unique qualified, succeeded, pinned deployments"
            )
        result[price.model] = {
            "provider": provider,
            "version": price.deployment_version,
            "max_input_tokens": price.max_input_tokens,
            "max_output_tokens": price.max_output_tokens,
            "supports_reasoning": not price.model.startswith("gpt-4")
            and price.model != "gpt-chat-latest",
        }
    return result
