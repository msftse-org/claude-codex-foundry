import copy
import importlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from claude_foundry.azure import AzureError, api_path
from claude_foundry.portal_setup import prepare_shared


def shared_azure(org):
    api = api_path(org)
    service = api.split("/apis/")[0]
    responses = {
        api: {"properties": {"path": org["api_path"], "serviceUrl": org["foundry_url"]}},
        service: {
            "properties": {"gatewayUrl": org["gateway_url"].rsplit("/", 1)[0]},
            "identity": {"principalId": "apim-identity"},
        },
        api + "/operations": {
            "value": [
                {"name": name, "properties": {"method": method, "urlTemplate": url}}
                for name, method, url in [
                    ("messages", "POST", "/v1/messages"),
                    ("count-tokens", "POST", "/v1/messages/count_tokens"),
                    ("models", "GET", "/v1/models"),
                ]
            ]
        },
        api + "/diagnostics/applicationinsights": {"properties": {"loggerId": "/existing/logger"}},
    }
    azure = Mock()
    azure.request.side_effect = lambda method, path, *args, **kwargs: (
        responses[path] if path in responses else {"value": []}
    )
    return azure, responses


def test_shared_setup_preserves_route_and_never_writes_api(org):
    before = copy.deepcopy(org)
    azure, _ = shared_azure(org)
    result = prepare_shared(org, azure)
    assert result == before == org
    assert result is not org
    writes = [c for c in azure.request.call_args_list if c.args[0] != "GET"]
    assert len(writes) == 1
    assert "/providers/Microsoft.Authorization/roleAssignments/" in writes[0].args[1]
    assert not any("/apis/" in c.args[1] for c in writes)


def test_shared_setup_is_idempotent_for_existing_inference_role(org):
    azure, responses = shared_azure(org)
    from claude_foundry.azure import account_path

    responses[account_path(org) + "/providers/Microsoft.Authorization/roleAssignments"] = {
        "value": [
            {
                "properties": {
                    "principalId": "apim-identity",
                    "roleDefinitionId": f"/subscriptions/{org['subscription_id']}/providers/Microsoft.Authorization/roleDefinitions/a97b65f3-24c7-4388-baec-2e87135dc908",
                }
            }
        ]
    }
    prepare_shared(org, azure)
    prepare_shared(org, azure)
    assert all(c.args[0] == "GET" for c in azure.request.call_args_list)


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_path", "different"),
        ("gateway_url", "https://another.azure-api.net/claude"),
        ("foundry_url", "https://another.services.ai.azure.com/anthropic"),
    ],
)
def test_shared_setup_rejects_mismatched_target_before_writing(org, field, value):
    azure, _ = shared_azure(org)
    org[field] = value
    with pytest.raises(ValueError, match="match"):
        prepare_shared(org, azure)
    assert all(c.args[0] == "GET" for c in azure.request.call_args_list)


def test_shared_setup_rejects_missing_operation(org):
    azure, responses = shared_azure(org)
    responses[api_path(org) + "/operations"]["value"].pop()
    with pytest.raises(ValueError, match="operations"):
        prepare_shared(org, azure)
    assert all(c.args[0] == "GET" for c in azure.request.call_args_list)


def test_shared_setup_requires_api_observability_before_writing(org):
    azure, _ = shared_azure(org)
    diagnostic = api_path(org) + "/diagnostics/applicationinsights"
    respond = azure.request.side_effect

    def without_diagnostic(method, path, *args, **kwargs):
        if path == diagnostic:
            raise AzureError(404, "missing")
        return respond(method, path, *args, **kwargs)

    azure.request.side_effect = without_diagnostic
    with pytest.raises(ValueError, match="admin observability"):
        prepare_shared(org, azure)
    assert all(c.args[0] == "GET" for c in azure.request.call_args_list)


def test_shared_setup_surfaces_azure_errors_without_writes(org):
    azure = Mock()
    azure.request.side_effect = AzureError(403, "denied")
    with pytest.raises(AzureError, match="denied"):
        prepare_shared(org, azure)
    assert len(azure.request.call_args_list) == 1


@pytest.fixture
def setup_scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("prepare_portal"), importlib.import_module(
        "prepare_portal_preview"
    )


@pytest.mark.parametrize("isolated", [False, True])
def test_setup_command_defaults_to_shared_with_explicit_isolation(
    org, monkeypatch, tmp_path, setup_scripts, isolated
):
    script, _ = setup_scripts
    monkeypatch.setattr(script, "load_org", lambda path: org)
    monkeypatch.setattr(script, "Azure", Mock())
    shared = Mock(return_value=org)
    preview = Mock(return_value=org)
    monkeypatch.setattr(script, "prepare_shared", shared)
    monkeypatch.setattr(script, "prepare_isolated", preview)
    args = [
        "prepare_portal.py",
        "--organization",
        "source.json",
        "--output",
        str(tmp_path / "result.json"),
    ]
    monkeypatch.setattr("sys.argv", args + (["--isolated"] if isolated else []))
    script.main()
    assert shared.call_count == int(not isolated)
    assert preview.call_count == int(isolated)


def test_preview_preparation_cannot_target_shared_api(org, setup_scripts):
    _, preview = setup_scripts
    azure = Mock()
    with pytest.raises(ValueError, match="separate"):
        preview.prepare(org, org["api_id"], org["api_path"], azure)
    azure.request.assert_not_called()


def test_destructive_live_tests_refuse_shared_route(org, setup_scripts, tmp_path, monkeypatch):
    script = importlib.import_module("verify_portal_preview")
    azure = Mock()
    monkeypatch.setattr(script, "PreviewAzure", azure)
    with pytest.raises(ValueError, match="separate preview API"):
        script.verify(org, {}, tmp_path / "results.json")
    azure.assert_not_called()
