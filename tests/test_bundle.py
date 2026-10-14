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
