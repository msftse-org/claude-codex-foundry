import xml.etree.ElementTree as ET
from uuid import uuid4

import pytest

from claude_foundry.policy_cache_gateway import deploy, policy_xml


def test_cache_policy_authenticates_before_lookup_and_caches_only_snapshots(org):
    reader = str(uuid4())
    root = ET.fromstring(
        policy_xml(
            org["tenant_id"],
            "api://" + str(uuid4()),
            [reader],
            "https://test.azurewebsites.net",
            org["subscription_id"],
        )
    )
    inbound = root.find("inbound")
    assert inbound[0].tag == "validate-azure-ad-token"
    assert (
        inbound.find("validate-azure-ad-token/required-claims/claim[@name='oid']/value").text
        == reader
    )
    assert inbound.find("cache-lookup-value").get("caching-type") == "internal"
    assert inbound.find("rate-limit-by-key") is not None
    assert not root.findall(".//base")
    assert not any("semantic" in e.tag for e in root.iter())
    assert (
        root.find("outbound/choose/when").get("condition")
        == "@(context.Response.StatusCode == 200)"
    )
    assert root.find(".//cache-store-value").get("duration") == "3600"
    assert org["tenant_id"] in inbound.find("set-variable[@name='policyCacheKey']").get("value")
    assert org["subscription_id"] in inbound.find("set-variable[@name='policyCacheKey']").get(
        "value"
    )
    assert "scp" in ET.tostring(root, encoding="unicode")


@pytest.mark.parametrize("mode", ["internal", "external", "prefer-external"])
def test_explicit_cache_backend_configuration(org, mode):
    root = ET.fromstring(
        policy_xml(
            org["tenant_id"],
            "api://" + str(uuid4()),
            [str(uuid4())],
            "https://test.azurewebsites.net",
            org["subscription_id"],
            mode,
        )
    )
    assert all(
        e.get("caching-type") == mode
        for e in root.iter()
        if e.tag in {"cache-lookup-value", "cache-store-value"}
    )


@pytest.mark.parametrize(
    "tier,mode,external,owned",
    [
        ("Consumption", "internal", False, True),
        ("Consumption", "prefer-external", False, True),
        ("BasicV2", "external", False, True),
        ("BasicV2", "internal", False, False),
    ],
)
def test_deploy_rejects_unavailable_cache_and_unowned_api_before_writes(
    org, tier, mode, external, owned
):
    writes = []

    class Azure:
        def request(self, method, path, body=None):
            if method != "GET":
                writes.append(path)
                return {}
            if path.endswith("/caches"):
                return {"value": [{}] if external else []}
            if "/apis/" in path:
                return {
                    "properties": {
                        "description": "Managed by claude-codex-foundry budget gateway preview."
                        if owned
                        else "Other API"
                    }
                }
            return {"sku": {"name": tier}}

    with pytest.raises(ValueError):
        deploy(
            org,
            Azure(),
            api_id="claude-budget-preview",
            origin_url="https://test.azurewebsites.net",
            audience="api://" + str(uuid4()),
            reader_ids=[str(uuid4())],
            organization_id=org["subscription_id"],
            caching_type=mode,
        )
    assert writes == []


def test_cache_deployment_adds_only_its_operations_and_defines_digest_parameter(org):
    writes = []

    class Azure:
        def request(self, method, path, body=None):
            if method == "PUT":
                writes.append((path, body))
                return {}
            if "/apis/" in path:
                return {
                    "properties": {
                        "description": "Managed by claude-codex-foundry budget gateway preview."
                    }
                }
            return {"sku": {"name": "BasicV2"}}

    deploy(
        org,
        Azure(),
        api_id="claude-budget-preview",
        origin_url="https://test.azurewebsites.net",
        audience="api://" + str(uuid4()),
        reader_ids=[str(uuid4())],
        organization_id=org["subscription_id"],
    )
    assert len(writes) == 3 and all("/operations/" in path for path, _ in writes)
    assert writes[0][1]["properties"]["templateParameters"] == [
        {"name": "digest", "required": True, "type": "string"}
    ]
