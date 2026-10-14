from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from claude_foundry import auth
from claude_foundry.config import export_client, write_json
from claude_foundry.models import plan_models, sync_models


def model(name, **kwargs):
    return {
        "name": name,
        "format": "Anthropic",
        "version": "1",
        "skus": [{"name": "GlobalStandard", "usageName": name}],
        **kwargs,
    }


def quota(name, used=0, limit=20):
    return {"name": {"value": name}, "currentValue": used, "limit": limit}


def test_model_plan_respects_quota_retirement_and_existing_capacity():
    catalog = [
        model("claude-sonnet-4-6"),
        model("claude-opus-4-6"),
        model("claude-fable-5"),
        model("claude-opus-4-1", deprecation={"inference": "2026-01-01T00:00:00Z"}),
    ]
    existing = [
        {
            "name": "custom-opus",
            "sku": {"capacity": 40},
            "properties": {
                "model": {"name": "claude-opus-4-6", "format": "Anthropic", "version": "1"},
                "provisioningState": "Succeeded",
            },
        }
    ]
    plan = {
        p["name"]: p
        for p in plan_models(
            catalog,
            [quota("claude-sonnet-4-6", used=17)],
            existing,
            now=datetime(2026, 10, 14, tzinfo=UTC),
        )
    }
    assert plan["claude-sonnet-4-6"]["capacity"] == 3
    assert plan["claude-opus-4-6"]["action"] == "keep"
    assert plan["claude-opus-4-6"]["deployment_name"] == "custom-opus"
    assert plan["claude-fable-5"]["reason"] == "no available quota"
    assert plan["claude-opus-4-1"]["reason"] == "retired"


def test_prefers_azure_hosted_default_variant():
    catalog = [
        model("m", version="old"),
        model("m", version="new", capabilities={"hostedOn": "azure"}, isDefaultVersion=True),
    ]
    assert plan_models(catalog, [quota("m")], [])[0]["version"] == "new"


def test_sync_excludes_failed_models_and_preserves_deployment_name(org):
    azure = Mock()
    azure.request.return_value = {
        "value": [
            {
                "name": "team-sonnet",
                "properties": {
                    "model": {"name": "claude-sonnet-4-6", "format": "Anthropic"},
                    "provisioningState": "Succeeded",
                },
            },
            {
                "name": "failed",
                "properties": {
                    "model": {"name": "claude-opus-4-6", "format": "Anthropic"},
                    "provisioningState": "Failed",
                },
            },
        ]
    }
    assert sync_models(org, azure)["models"][0]["name"] == "team-sonnet"
    assert len(org["models"]) == 1


def test_cli_helper_targets_tenant_without_changing_subscription(org, monkeypatch):
    az = Mock(return_value={"accessToken": "test-only"})
    monkeypatch.setattr(auth, "az_json", az)
    monkeypatch.setattr(
        auth.subprocess, "run", Mock(side_effect=AssertionError("No interactive calls"))
    )
    assert auth.credential(export_client(org)) == "test-only"
    assert "--tenant" in az.call_args.args
    assert org["tenant_id"] in az.call_args.args
    assert "--subscription" not in az.call_args.args


def test_msal_helper_never_opens_browser_when_silent_token_unavailable(org, monkeypatch, tmp_path):
    app = Mock()
    app.get_accounts.return_value = []
    monkeypatch.setattr(auth, "msal_app", lambda c: (app, tmp_path))
    c = export_client(org, "msal", org["tenant_id"])
    with pytest.raises(RuntimeError, match="Sign-in required"):
        auth.credential(c)
    app.acquire_token_interactive.assert_not_called()
    app.initiate_device_flow.assert_not_called()


def test_msal_selects_recorded_account_only(org, monkeypatch, tmp_path):
    app = Mock()
    account = {"home_account_id": "selected"}
    app.get_accounts.return_value = [{"home_account_id": "wrong"}, account]
    app.acquire_token_silent_with_error.return_value = {"access_token": "test-only"}
    write_json(tmp_path / "account.json", account)
    monkeypatch.setattr(auth, "msal_app", lambda c: (app, tmp_path))
    assert auth.credential(export_client(org, "msal", org["tenant_id"])) == "test-only"
    assert app.acquire_token_silent_with_error.call_args.kwargs["account"] == account
    app.acquire_token_interactive.assert_not_called()


def test_sync_defaults_use_model_version_not_custom_deployment_name(org):
    azure = Mock()
    azure.request.return_value = {
        "value": [
            {
                "name": "zzz-old",
                "properties": {
                    "model": {"name": "claude-sonnet-4-5", "format": "Anthropic"},
                    "provisioningState": "Succeeded",
                },
            },
            {
                "name": "team-current",
                "properties": {
                    "model": {"name": "claude-sonnet-4-6", "format": "Anthropic"},
                    "provisioningState": "Succeeded",
                },
            },
        ]
    }
    result = sync_models(org, azure)
    assert result["models"][0]["name"] == "team-current"
    assert result["models"][0]["isFamilyDefault"]
    org["excluded_deployments"] = ["zzz-old"]
    assert len(sync_models(org, azure)["models"]) == 1
