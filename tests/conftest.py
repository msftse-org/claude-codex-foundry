import pytest


@pytest.fixture
def org():
    return {
        "schema_version": 1,
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "subscription_id": "00000000-0000-0000-0000-000000000002",
        "allowed_object_ids": ["00000000-0000-0000-0000-000000000003"],
        "apim_name": "test-apim",
        "apim_resource_group": "rg-test",
        "foundry_name": "test-foundry",
        "foundry_resource_group": "rg-test",
        "api_id": "claude-foundry",
        "api_path": "claude",
        "gateway_url": "https://test-apim.azure-api.net/claude",
        "foundry_url": "https://test-foundry.services.ai.azure.com/anthropic",
        "telemetry": {"enabled": True, "token_metrics": True},
        "models": [
            {
                "name": "claude-sonnet-4-6",
                "labelOverride": "Sonnet 4.6",
                "anthropicFamilyTier": "sonnet",
                "isFamilyDefault": True,
            }
        ],
    }
