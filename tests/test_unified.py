import copy
from datetime import UTC, datetime, timedelta

import pytest

from claude_foundry.unified import provider_configs, reviewed_deployments


def test_provider_foundation_and_qualification_cannot_be_mixed(org):
    codex = {
        **org,
        "client_type": "codex",
        "default_model": "gpt-test",
        "gateway_url": "https://test-apim.azure-api.net/codex/v1",
        "models": [{"name": "gpt-test", "model": "gpt-test", "qualified": True}],
    }
    assert set(provider_configs(org, {"claude": org, "codex": codex})) == {"claude", "codex"}
    for change in [
        {"tenant_id": "00000000-0000-0000-0000-000000000005"},
        {"gateway_url": "https://other.example/codex/v1"},
        {"models": [{"name": "gpt-test", "model": "gpt-test"}]},
    ]:
        with pytest.raises(ValueError):
            provider_configs(org, {"codex": {**codex, **change}})


def test_reviewed_manifest_requires_the_actual_pinned_version_and_current_prices(org):
    name = org["models"][0]["name"]
    price = {
        "model": name,
        "provider": "anthropic",
        "deployment_version": "1",
        "max_input_tokens": 1000,
        "max_output_tokens": 4096,
        "default_output_tokens": 4096,
        "input_ceiling_per_million": "1",
        "output_ceiling_per_million": "3",
        "source_url": "https://example.com/pricing",
        "verified_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "valid_until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "note": "Synthetic reviewed test price",
    }
    deployment = {
        "name": name,
        "properties": {
            "provisioningState": "Succeeded",
            "model": {"version": "1", "format": "Anthropic"},
            "versionUpgradeOption": "NoAutoUpgrade",
        },
    }
    result = reviewed_deployments({"claude": org}, [price], [deployment])
    assert result[name]["version"] == "1" and result[name]["max_input_tokens"] == 1000
    for change in [
        {"versionUpgradeOption": "OnceNewDefaultVersionAvailable"},
        {"model": {"version": "2", "format": "Anthropic"}},
        {"provisioningState": "Creating"},
    ]:
        bad = copy.deepcopy(deployment)
        bad["properties"].update(change)
        with pytest.raises(ValueError):
            reviewed_deployments({"claude": org}, [price], [bad])
    with pytest.raises(ValueError):
        reviewed_deployments({"claude": org}, [price, price], [deployment])
