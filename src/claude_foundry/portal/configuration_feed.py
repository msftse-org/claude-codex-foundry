"""Own-user, data-only projection of published shared-runtime configuration.

No routes, credentials, arbitrary settings or executable instructions cross this
boundary. Deployment metadata is supplied by the operator from the same reviewed
bounds as the budget runtime, never by the caller or an unpublished portal draft.
"""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .budget_pricing import BudgetPrice
from .client_configuration import project_profile

CONFIGURATION_PATH = "/api/v1/me/client-configuration"


class ReviewedDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["anthropic", "openai"]
    version: str = Field(min_length=1, max_length=80)
    max_input_tokens: int = Field(ge=1, le=10_000_000)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    supports_reasoning: bool = True


def deployment_bounds(values):
    if not isinstance(values, dict) or not values or len(values) > 100:
        raise ValueError("Configuration access requires reviewed deployment bounds")
    return {name: ReviewedDeployment.model_validate(value) for name, value in values.items()}


def client_configuration(providers, deployments, policy, organization_id, tenant_id, subject_id):
    """Build only the caller's current grants; raise PermissionError if revoked."""
    members = [m for m in policy["members"] if m["id"] == subject_id]
    if len(members) != 1 or members[0]["status"] != "active":
        raise PermissionError("Current published membership is required")
    member = members[0]
    if len([g for g in policy["groups"] if g["id"] == member["group_id"]]) != 1:
        raise ValueError("Published member group is invalid")
    clients = {}
    for client, config in providers.items():
        provider = "anthropic" if client == "claude" else "openai"
        available = []
        for model in config["models"]:
            if client == "codex" and model.get("qualified") is not True:
                continue
            deployment = deployments.get(model["name"])
            prices = [
                p
                for p in policy["budget_prices"]
                if p["model"] == model["name"] and p["provider"] == provider
            ]
            if deployment is None or len(prices) != 1:
                continue
            price = BudgetPrice.model_validate(prices[0])
            try:
                price.check_current()
            except ValueError:
                continue
            if (
                deployment.provider != provider
                or deployment.version != price.deployment_version
                or deployment.max_input_tokens != price.max_input_tokens
                or deployment.max_output_tokens != price.max_output_tokens
            ):
                continue
            available.append(model)
        projected = project_profile({**config, "models": available}, policy, member)
        models = projected["models"]
        if client == "claude":
            clients[client] = {
                "route_id": config["api_id"],
                "default_model": models[0]["name"] if models else "",
                "models": [
                    {
                        "name": model["name"],
                        "family_tier": model["anthropicFamilyTier"],
                        "family_default": model.get("isFamilyDefault", False),
                    }
                    for model in models
                ],
            }
        else:
            clients[client] = {
                "route_id": config["api_id"],
                "default_model": projected["default_model"],
                "models": [{"name": model["name"], "model": model["model"]} for model in models],
            }
    return {
        "schema_version": 1,
        "organization_id": str(UUID(organization_id)),
        "tenant_id": str(UUID(tenant_id)),
        "subject_id": str(UUID(subject_id)),
        "state": "active" if any(value["models"] for value in clients.values()) else "no_models",
        "clients": clients,
    }


def configuration_bytes(value):
    """Stable bytes: authorization is always checked before comparing this ETag."""
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return body, '"sha256:' + hashlib.sha256(body).hexdigest() + '"'
