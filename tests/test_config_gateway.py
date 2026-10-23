import json
import xml.etree.ElementTree as ET

import pytest

from claude_foundry.azure import AzureError
from claude_foundry.config import export_client, validate_client, write_json
from claude_foundry.gateway import catalog_policy, deploy, policy_xml


def test_export_excludes_admin_data(org):
    client = export_client(org)
    assert not {"subscription_id", "allowed_object_ids", "foundry_url", "apim_name"} & client.keys()
    assert client["models"] == org["models"]


@pytest.mark.parametrize(
    "url",
    ["http://host", "https://user:password@host", "https://host?key=x", "https://host#fragment"],
)
def test_reject_unsafe_gateway_url(org, url):
    org["gateway_url"] = url
    with pytest.raises(ValueError):
        export_client(org)


def test_reject_duplicate_model(org):
    org["models"] *= 2
    with pytest.raises(ValueError, match="Duplicate"):
        validate_client(org)


def test_private_atomic_config(tmp_path):
    path = tmp_path / "new" / "config.json"
    write_json(path, {"value": "one"})
    write_json(path, {"value": "two"})
    assert json.loads(path.read_text()) == {"value": "two"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".claude-foundry-*"))


def test_gateway_validates_identity_before_routing(org):
    policy = ET.fromstring(policy_xml(org))
    inbound = policy.find("inbound")
    jwt = inbound.find("validate-jwt")
    assert list(inbound).index(jwt) < list(inbound).index(inbound.find("set-backend-service"))
    assert jwt.get("require-signed-tokens") == "true"
    assert jwt.get("require-expiration-time") == "true"
    assert jwt.find("audiences/audience").text == "https://cognitiveservices.azure.com"
    assert jwt.find("required-claims/claim[@name='oid']/value").text == org["allowed_object_ids"][0]
    assert policy.find("backend/forward-request").get("buffer-response") == "false"
    assert policy.find(".//set-header[@name='x-api-key']").get("exists-action") == "delete"
    assert policy.find(".//set-status").get("code") == "400"
    assert policy.find(".//llm-emit-token-metric") is not None
    assert "Authorization" not in ET.tostring(policy.find(".//trace"), encoding="unicode")


def test_catalog_authentication_cannot_be_bypassed(org):
    inbound = ET.fromstring(catalog_policy(org)).find("inbound")
    assert [node.tag for node in inbound] == ["base", "return-response"]
    data = json.loads(inbound.find("return-response/set-body").text)
    assert data["data"][0]["id"] == org["models"][0]["name"]


class FakeAzure:
    def __init__(self, new=False, reject_policy=False):
        self.calls = []
        self.new = new
        self.reject_policy = reject_policy

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and self.new:
            raise AzureError(404, "missing")
        if self.reject_policy and path.endswith("/policies/policy"):
            raise AzureError(400, "invalid policy")
        if method == "GET" and path.endswith("/policies/policy"):
            return {"properties": {"value": "<policies><inbound><base /></inbound></policies>"}}
        return {}


def test_create_remains_subscription_protected_until_policy_installed(org):
    azure = FakeAzure(new=True)
    deploy(org, azure)
    assert azure.calls[1][2]["properties"]["subscriptionRequired"] is True
    assert azure.calls[-1][2]["properties"] == {"subscriptionRequired": False}
    assert not any("/diagnostics" in call[1] or "/loggers" in call[1] for call in azure.calls)


def test_failed_policy_never_disables_subscription_gate(org):
    azure = FakeAzure(new=True, reject_policy=True)
    with pytest.raises(AzureError):
        deploy(org, azure)
    assert not any(
        call[2] and call[2].get("properties") == {"subscriptionRequired": False}
        for call in azure.calls
    )


def test_existing_api_is_patched_without_replacing_diagnostics(org):
    azure = FakeAzure()
    deploy(org, azure)
    patch = next(call for call in azure.calls if call[0] == "PATCH")
    assert "subscriptionRequired" not in patch[2]["properties"]


def test_baseline_gateway_cannot_overwrite_managed_policy(org):
    from unittest.mock import Mock

    azure = Mock()
    azure.request.side_effect = [
        {},
        {
            "properties": {
                "value": '<policies><inbound><set-variable name="governance" value="x" /></inbound></policies>'
            }
        },
    ]
    with pytest.raises(ValueError, match="managed"):
        deploy(org, azure)
    assert all(call.args[0] == "GET" for call in azure.request.call_args_list)


def test_observability_reuses_logger_without_editing_global_diagnostics(org):
    from unittest.mock import Mock

    from claude_foundry.gateway import configure_observability

    azure = Mock()
    properties = {
        "loggerId": "/existing/logger",
        "sampling": {"percentage": 23},
        "frontend": {"request": {"body": {"bytes": 0}}},
    }
    azure.request.side_effect = [
        AzureError(404, "no API diagnostic"),
        {"properties": properties},
        {},
    ]
    configure_observability(org, azure)
    method, path, body = azure.request.call_args.args
    assert method == "PUT"
    assert "/apis/claude-foundry/diagnostics/" in path
    assert body["properties"]["loggerId"] == "/existing/logger"
    assert body["properties"]["sampling"]["percentage"] == 23
    assert body["properties"]["metrics"] is True
    assert body["properties"]["frontend"] == properties["frontend"]
