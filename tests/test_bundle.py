import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest
from test_configuration_client import descriptor_value

from claude_foundry.client_bundle import build_bundle, build_enrollment_bundle, build_template
from claude_foundry.config import export_client, write_json
from claude_foundry.configuration_client import AZURE_CLI_CLIENT_ID


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
        readme = bundle.read("claude-foundry/README.md").decode()
        assert "Claude & Codex on Azure" in readme
        assert packaged["gateway_url"] in readme
        assert org["gateway_url"] in readme
        assert "codex-client.json" in readme
        assert "AGENTS.md" in readme


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


def test_shared_runtime_bundle_has_matching_verification_instructions(org):
    profiles = [export_client(org), codex_profile(org)]
    template = build_template(Path(__file__).resolve().parents[1])
    with zipfile.ZipFile(io.BytesIO(build_bundle(profiles, template, budgeted=True))) as archive:
        readme = archive.read("claude-foundry/README.md").decode()
        assert "--budgeted" in readme and "--governed" not in readme
        assert "codex/client.json" in readme


def test_portal_template_is_readable_by_a_separate_runtime_user(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "portal_builder", root / "scripts/build_portal.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *args, **kwargs: b"locked requirements"
    )
    output = module.build(tmp_path / "portal.zip", root)
    with zipfile.ZipFile(output) as package:
        template = package.getinfo("client-bundle.zip")
        assert template.external_attr >> 16 & 0o444 == 0o444
        with zipfile.ZipFile(io.BytesIO(package.read(template))) as archive:
            assert "claude-foundry/Install.command" in archive.namelist()


@pytest.mark.parametrize("clients", [["claude"], ["codex"], ["codex", "claude"]])
def test_enrollment_bundle_contains_only_trust_anchors_and_app_selection(clients):
    descriptor = descriptor_value()
    template = build_template(Path(__file__).resolve().parents[1])
    with zipfile.ZipFile(
        io.BytesIO(build_enrollment_bundle(descriptor, template, clients=clients))
    ) as archive:
        assert json.loads(archive.read("claude-foundry/enrollment.json")) == descriptor
        assert json.loads(archive.read("claude-foundry/installation.json")) == {
            "schema_version": 1,
            "clients": sorted(clients),
        }
        assert "claude-foundry/client.json" not in archive.namelist()
        assert "claude-foundry/codex-client.json" not in archive.namelist()
        assert "claude-foundry/docs/client-configuration-refresh.md" in archive.namelist()
        assert archive.getinfo("claude-foundry/Install.command").external_attr >> 16 & 0o111
        readme = archive.read("claude-foundry/README.md").decode()
        assert descriptor["configuration_url"] in readme
        assert "--migrate-existing" in readme and "enable-refresh" in readme
        assert "SETUP COMPLETE" in readme and "last good local configuration" in readme
        assert "no plaintext fallback" in readme and "Azure CLI is not required" in readme
        assert not any("/.local/" in name or "/.git/" in name for name in archive.namelist())


@pytest.mark.parametrize("clients", [[], ["claude", "claude"], ["unknown"], [None], "claude"])
def test_enrollment_bundle_rejects_invalid_selection(clients):
    with pytest.raises(ValueError, match="Choose clients"):
        build_enrollment_bundle(descriptor_value(), b"", clients=clients)


def test_cli_enrollment_bundle_explains_cli_owned_storage_without_promising_keychain():
    descriptor = descriptor_value()
    descriptor.update(schema_version=3, public_client_id=AZURE_CLI_CLIENT_ID)
    template = build_template(Path(__file__).resolve().parents[1])
    with zipfile.ZipFile(
        io.BytesIO(build_enrollment_bundle(descriptor, template, clients=["claude"]))
    ) as archive:
        readme = archive.read("claude-foundry/README.md").decode()
        assert "Azure CLI" in readme and "--install-azure-cli" in readme
        assert "not necessarily encrypted" in readme
        assert "Keychain" not in readme and "Azure CLI is not required" not in readme
        assert "writes no tokens" in readme
        assert json.loads(archive.read("claude-foundry/enrollment.json")) == descriptor


@pytest.mark.parametrize(
    "name", ["client.json", "enrollment.json", "installation.json", "README.md"]
)
def test_enrollment_bundle_refuses_a_preconfigured_template(name):
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("claude-foundry/" + name, "{}")
    with pytest.raises(ValueError, match="unconfigured"):
        build_enrollment_bundle(descriptor_value(), source.getvalue(), clients=["claude"])
