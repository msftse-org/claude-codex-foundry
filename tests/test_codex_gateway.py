import xml.etree.ElementTree as ET

import pytest

from claude_foundry.codex_gateway import catalog_policy, deploy, policy_xml
from claude_foundry.codex_models import initialize
from claude_foundry.config import export_client


@pytest.fixture
def codex(org):
    c = initialize(org)
    c.update(default_model="gpt-5.4", models=[{"name": "gpt-5.4", "model": "gpt-5.4"}])
    return c


def test_enforces_identity_model_allowlist_statelessness_and_streaming(codex):
    root = ET.fromstring(policy_xml(codex))
    jwt = root.find("inbound/validate-jwt")
    assert jwt.get("require-signed-tokens") == "true"
    assert (
        jwt.find('required-claims/claim[@name="oid"]/value').text == codex["allowed_object_ids"][0]
    )
    assert jwt.find("audiences/audience").text == "https://cognitiveservices.azure.com"
    bodies = [x.text for x in root.findall(".//set-body")]
    assert any('b["store"] = false' in t for t in bodies)
    guards = [x.get("condition", "") for x in root.findall(".//when")]
    assert any(
        "previous_response_id" in t and "conversation" in t and "background" in t for t in guards
    )
    assert any("gpt-5.4" in t for t in guards)
    assert root.find("backend/forward-request").get("buffer-response") == "false"
    assert root.find(".//llm-emit-token-metric").get("namespace") == "CodexFoundry"
    assert not root.findall(".//authentication-managed-identity")
    assert not root.findall(".//log-to-eventhub")


def test_catalog_authenticates_before_returning_models(codex):
    inbound = ET.fromstring(catalog_policy(codex)).find("inbound")
    assert [x.tag for x in inbound] == ["base", "return-response"]


def test_cannot_replace_claude_api_or_export_unqualified_models(codex):
    codex["api_id"] = "claude-foundry"
    with pytest.raises(ValueError, match="separate"):
        deploy(codex, None)
    with pytest.raises(ValueError, match="Qualify"):
        export_client(codex)


def test_initialization_keeps_foundation_and_removes_anthropic_settings(org):
    org["model_provider_data"] = {"organizationName": "Example"}
    org["conversation_import"] = {"enabled": True}
    c = initialize(org)
    assert c["gateway_url"] == "https://test-apim.azure-api.net/codex/v1"
    assert c["foundry_url"] == "https://test-foundry.openai.azure.com/openai"
    assert c["allowed_object_ids"] == org["allowed_object_ids"]
    assert "conversation_import" not in c and "model_provider_data" not in c
    assert org["api_id"] == "claude-foundry"


def test_initialization_uses_advertised_custom_domain(org):
    c = initialize(org, "https://custom-subdomain.openai.azure.com/")
    assert c["foundry_url"] == "https://custom-subdomain.openai.azure.com/openai"
