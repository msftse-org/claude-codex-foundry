import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from claude_foundry.config import export_client, write_json


def builder():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("bundle", root / "scripts/build_client_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build, root


def test_distribution_excludes_administrator_state(org, tmp_path):
    build, root = builder()
    profile = tmp_path / "client.json"
    write_json(profile, export_client(org))
    output = build(profile, tmp_path / "client.zip", root)
    with zipfile.ZipFile(output) as bundle:
        names = bundle.namelist()
        assert "claude-foundry/client.json" in names
        assert bundle.read("claude-foundry/README.md") == bundle.read(
            "claude-foundry/docs/macos.md"
        )
        assert not any(
            "/.local/" in name or "/.git/" in name or "/infra/" in name for name in names
        )
        assert bundle.getinfo("claude-foundry/Install.command").external_attr >> 16 & 0o111


def test_distribution_refuses_an_admin_profile(org, tmp_path):
    build, root = builder()
    profile = tmp_path / "organization.json"
    write_json(profile, org)
    with pytest.raises(ValueError, match="client profile only"):
        build(profile, tmp_path / "client.zip", root)
    assert not (tmp_path / "client.zip").exists()


def test_history_policy_reaches_customer_bundle_without_admin_data(org, tmp_path):
    build, root = builder()
    policy = {
        "enabled": True,
        "allowedOrganizationUuids": ["00000000-0000-0000-0000-000000000003"],
        "bannerBehavior": "show",
    }
    org["conversation_import"] = policy
    config = export_client(org)
    assert config["conversation_import"] == policy
    assert config["conversation_import"] is not policy
    profile = tmp_path / "client.json"
    write_json(profile, config)
    output = build(profile, tmp_path / "client.zip", root)
    with zipfile.ZipFile(output) as bundle:
        packaged = json.loads(bundle.read("claude-foundry/client.json"))
        assert packaged["conversation_import"] == policy
        assert "subscription_id" not in packaged
        assert "claude-foundry/docs/conversation-migration.md" in bundle.namelist()


def codex_profile(org):
    return export_client(
        {
            **org,
            "client_type": "codex",
            "gateway_url": "https://test-apim.azure-api.net/codex/v1",
            "models": [{"name": "gpt-5.3-codex", "model": "gpt-5.3-codex", "qualified": True}],
            "default_model": "gpt-5.3-codex",
        }
    )


def test_one_bundle_configures_claude_and_codex(org, tmp_path):
    build, root = builder()
    claude, codex = tmp_path / "client.json", tmp_path / "codex-client.json"
    write_json(claude, export_client(org))
    write_json(codex, codex_profile(org))
    output = build([codex, claude], tmp_path / "combined.zip", root)
    with zipfile.ZipFile(output) as bundle:
        assert json.loads(bundle.read("claude-foundry/client.json")).get("client_type") is None
        packaged = json.loads(bundle.read("claude-foundry/codex-client.json"))
        assert packaged["client_type"] == "codex"
        assert b"Claude & Codex on Azure" in bundle.read("claude-foundry/README.md")


@pytest.mark.parametrize("same_kind", [True, False])
def test_combined_bundle_requires_one_of_each_client_in_one_tenant(org, tmp_path, same_kind):
    build, root = builder()
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    write_json(first, export_client(org))
    other = dict(org, tenant_id="00000000-0000-0000-0000-000000000099")
    write_json(second, export_client(org) if same_kind else codex_profile(other))
    with pytest.raises(ValueError, match="one of each" if same_kind else "same Microsoft Entra"):
        build([first, second], tmp_path / "combined.zip", root)
    assert not (tmp_path / "combined.zip").exists()
