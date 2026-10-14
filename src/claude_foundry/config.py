"""Configuration validation and private, atomic local file writes."""

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

TOKEN_RESOURCE = "https://cognitiveservices.azure.com"
TIERS = {"sonnet", "opus", "haiku", "fable", "mythos"}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".claude-foundry-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path: Path, default=None):
    if not path.exists() and default is not None:
        return default
    return json.loads(path.read_text())


def https_url(value: str, field: str):
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"{field} must be an HTTPS URL without credentials, query or fragment")
    return value.rstrip("/")


def uuid_value(value, field):
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{field} must be a UUID") from None


def validate_client(c):
    uuid_value(c.get("tenant_id"), "tenant_id")
    c["gateway_url"] = https_url(c["gateway_url"], "gateway_url")
    if c.get("auth_mode", "azure-cli") not in {"azure-cli", "msal"}:
        raise ValueError("auth_mode must be azure-cli or msal")
    if c.get("auth_mode") == "msal":
        uuid_value(c.get("client_id"), "client_id")
    if c.get("token_resource", TOKEN_RESOURCE) != TOKEN_RESOURCE:
        raise ValueError("This release supports the Azure public-cloud Cognitive Services audience")
    if not c.get("models"):
        raise ValueError("models must contain at least one deployed model")
    seen = set()
    defaults = set()
    for model in c["models"]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model["name"]):
            raise ValueError("Invalid model deployment name")
        if model["name"] in seen:
            raise ValueError("Duplicate model deployment name")
        seen.add(model["name"])
        tier = model.get("anthropicFamilyTier")
        if tier not in TIERS:
            raise ValueError(f"Unknown model tier: {tier}")
        if model.get("isFamilyDefault"):
            if tier in defaults:
                raise ValueError(f"More than one default for {tier}")
            defaults.add(tier)
    return c


def load_client(path):
    return validate_client(read_json(Path(path)))


def load_org(path):
    c = read_json(Path(path))
    uuid_value(c.get("tenant_id"), "tenant_id")
    uuid_value(c.get("subscription_id"), "subscription_id")
    for field in (
        "apim_name",
        "apim_resource_group",
        "foundry_name",
        "foundry_resource_group",
        "api_id",
        "api_path",
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,89}", c.get(field, "")):
            raise ValueError(f"Invalid {field}")
    for oid in c.get("allowed_object_ids", []):
        uuid_value(oid, "allowed_object_ids")
    c["gateway_url"] = https_url(c["gateway_url"], "gateway_url")
    c["foundry_url"] = https_url(c["foundry_url"], "foundry_url")
    if c.get("token_resource", TOKEN_RESOURCE) != TOKEN_RESOURCE:
        raise ValueError("Unsupported token_resource")
    provider = c.get("model_provider_data")
    if provider:
        if not provider.get("organizationName") or not re.fullmatch(
            r"[A-Z]{2}", provider.get("countryCode", "")
        ):
            raise ValueError(
                "Marketplace attestation requires organizationName and ISO countryCode"
            )
        if provider.get("industry") not in {
            "technology",
            "finance",
            "healthcare",
            "education",
            "retail",
            "manufacturing",
            "government",
            "media",
            "other",
        }:
            raise ValueError("Invalid Marketplace industry")
    return c


def export_client(c, auth_mode="azure-cli", client_id=None):
    result = {k: c[k] for k in ("tenant_id", "gateway_url", "models")}
    result.update(
        schema_version=1,
        profile_name=c.get("profile_name", "Claude on Azure"),
        auth_mode=auth_mode,
        token_resource=TOKEN_RESOURCE,
    )
    if client_id:
        result["client_id"] = client_id
    return validate_client(result)
