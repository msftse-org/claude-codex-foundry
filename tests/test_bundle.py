import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

from claude_foundry.client_bundle import build_bundle, build_template
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
        readme = bundle.read("claude-foundry/README.md").decode()
        assert org["gateway_url"] in readme
        assert "bash Install.command" in readme
        assert "Claude Desktop / Claude Code" in readme
        assert "Azure CLI sign-in" in readme
        assert "claude-foundry/AGENTS.md" in names
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


def test_msal_bundle_explains_browser_sign_in_and_has_no_admin_state(org):
    template = build_template(Path(__file__).resolve().parents[1])
    with zipfile.ZipFile(io.BytesIO(template)) as archive:
        assert "claude-foundry/client.json" not in archive.namelist()
    profile = export_client(org, "msal", "00000000-0000-0000-0000-000000000004")
    with zipfile.ZipFile(io.BytesIO(build_bundle(profile, template))) as archive:
        readme = archive.read("claude-foundry/README.md").decode()
        assert "Azure CLI is not required" in readme
        assert "--install-azure-cli" not in readme
        packaged = json.loads(archive.read("claude-foundry/client.json"))
        assert packaged == profile
        assert "subscription_id" not in packaged
        assert "apim_name" not in packaged


def test_codex_bundle_uses_codex_setup_and_restore(org):
    profile = export_client(org)
    profile.update(
        client_type="codex",
        gateway_url="https://test-apim.azure-api.net/codex/v1",
        default_model="gpt-test",
        models=[{"name": "gpt-test", "model": "gpt-test", "qualified": True}],
    )
    template = build_template(Path(__file__).resolve().parents[1])
    with zipfile.ZipFile(io.BytesIO(build_bundle(profile, template))) as archive:
        readme = archive.read("claude-foundry/README.md").decode()
        assert "Codex Desktop / CLI" in readme
        assert profile["gateway_url"] in readme
        assert "restore --client codex" in readme
        assert "verify --config ~/.config/claude-foundry/codex/client.json" in readme
        assert "docs/codex.md" in readme
    profile["models"][0]["qualified"] = False
    with pytest.raises(ValueError, match="qualified"):
        build_bundle(profile, template)
