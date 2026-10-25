import importlib.util
import json
import sys
from pathlib import Path

import pytest

from claude_foundry.azure import AzureError
from claude_foundry.config import write_json

ROOT = Path(__file__).resolve().parents[1]
TENANT = "00000000-0000-0000-0000-000000000001"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000002"
USER = "00000000-0000-0000-0000-000000000003"
GROUP = "00000000-0000-0000-0000-000000000009"
DESKTOP = "00000000-0000-0000-0000-000000000010"
PORTAL_APP = "00000000-0000-0000-0000-000000000011"


@pytest.fixture
def deploy():
    spec = importlib.util.spec_from_file_location("deploy_script", ROOT / "scripts/deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "deploy.json"
    value = json.loads((ROOT / "examples/deploy.json").read_text())
    value.update(
        tenant_id=TENANT,
        subscription_id=SUBSCRIPTION,
        suffix="contoso1",
        accept_marketplace_terms=True,
    )
    path.write_text(json.dumps(value))
    return path


class FakeRunner:
    """Answers Azure CLI calls and emulates the files each documented command writes."""

    def __init__(self, org, group_exists=False, account_tenant=TENANT, consent_error=None):
        self.calls = []
        self.org = org
        self.group_exists = group_exists
        self.account_tenant = account_tenant
        self.consent_error = consent_error
        self.synced = ["m1"]

    def __call__(self, args, capture=False, allowed=(0,)):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == "az":
            if args[1:5] == ["ad", "app", "permission", "admin-consent"] and self.consent_error:
                raise self.consent_error("Command failed with exit code 1. Forbidden")
            return json.dumps(self.az(args[1:])) if capture else 0
        command = args[3:] if args[1:3] == ["-m", "claude_foundry.cli"] else args[1:]
        return self.tool(command, capture)

    def az(self, args):
        joined = " ".join(args)
        if joined.startswith("account show"):
            return {"tenantId": self.account_tenant, "id": SUBSCRIPTION}
        if joined.startswith("account list"):
            return [{"id": SUBSCRIPTION, "tenantId": TENANT}]
        if joined.startswith("ad signed-in-user show"):
            return {"id": USER, "displayName": "Owner", "userPrincipalName": "owner@example.com"}
        if joined.startswith("provider show"):
            return "Registered"
        if joined.startswith("ad group list"):
            return (
                [{"id": GROUP, "displayName": "Claude & Codex developers"}]
                if self.group_exists
                else []
            )
        if joined.startswith("ad group create"):
            return {"id": GROUP}
        if joined.startswith("ad group member check"):
            return {"value": False}
        if joined.startswith("deployment group create") and "claude-foundation" in args:
            return self.org
        return {}

    def tool(self, command, capture):
        name = Path(command[0]).name
        if name == "register_client_app.py":
            return json.dumps({"client_id": DESKTOP})
        if name == "register_portal.py":
            write_json(Path(command[command.index("--output") + 1]), {"client_id": PORTAL_APP})
        if name in {"prepare_portal.py", "build_client_bundle.py", "build_portal.py"}:
            flag = "--output"
            Path(command[command.index(flag) + 1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[command.index(flag) + 1]).write_text("{}")
        if command[:1] == ["admin"]:
            config = Path(command[2])
            action = command[3]
            if action == "init-codex":
                write_json(
                    Path(command[5]),
                    {
                        **self.org,
                        "client_type": "codex",
                        "gateway_url": "https://apim/codex/v1",
                        "models": [],
                    },
                )
            if action == "sync-models":
                # Like the real command, keep existing entries (and their qualification).
                data = json.loads(config.read_text())
                kept = {m["name"]: m for m in data.get("models", [])}
                data["models"] = [kept.get(name, {"name": name}) for name in self.synced]
                write_json(config, data)
            if action == "qualify":
                data = json.loads(config.read_text())
                data["models"] = [dict(m, qualified=True) for m in data["models"]]
                write_json(config, data)
            if action == "export-client":
                write_json(Path(command[command.index("--output") + 1]), {"tenant_id": TENANT})
        return "" if capture else 0


def organization(org):
    return {
        **org,
        "subscription_id": SUBSCRIPTION,
        "tenant_id": TENANT,
        "foundry_name": "ccf-foundry-contoso1",
        "foundry_resource_group": "rg-ccf-contoso1",
        "apim_name": "ccf-apim-contoso1",
        "apim_resource_group": "rg-ccf-contoso1",
        "models": [],
    }


def run_deployment(deploy, settings, tmp_path, runner, monkeypatch, managed=False):
    monkeypatch.setattr(deploy, "wait_for_health", lambda url: None)

    class FakeAzure:
        def __init__(self, subscription):
            pass

        def request(self, method, path):
            if not managed:
                raise AzureError(404, "missing")
            return {"properties": {"value": '<set-variable name="governance" value="x" />'}}

    job = deploy.Deployment(
        deploy.load_settings(settings),
        local=tmp_path / ".local",
        dist=tmp_path / "dist",
        runner=runner,
        azure=FakeAzure,
    )
    job.deploy(verify=False)
    return job


def commands(runner):
    return [" ".join(Path(a).name if "/" in a else a for a in call) for call in runner.calls]


def test_full_deployment_runs_documented_steps_in_order(
    deploy, settings, tmp_path, org, monkeypatch
):
    runner = FakeRunner(organization(org))
    job = run_deployment(deploy, settings, tmp_path, runner, monkeypatch)
    seen = commands(runner)
    order = [
        "az ad group create",
        "az deployment group create --name claude-foundation",
        "--apply --accept-marketplace-terms",
        "sync-models",
        "gateway",
        "observability --enable-dimensions",
        "init-codex",
        "qualify",
        "register_client_app.py",
        "ad app permission admin-consent",
        "export-client",
        "build_client_bundle.py",
        "prepare_portal.py",
        "register_portal.py",
        "az deployment group create --name admin-portal",
        "build_portal.py",
        "config-zip",
    ]
    positions = [next(i for i, c in enumerate(seen) if step in c) for step in order]
    assert positions == sorted(positions)
    assert any("ad group member add" in c and USER in c for c in seen)
    bundle = next(c for c in runner.calls if c[1].endswith("build_client_bundle.py"))
    assert bundle.count("--profile") == 2
    assert job.state["developer_group_id"] == GROUP
    assert job.state["desktop_client_id"] == DESKTOP
    assert job.state["admin_consent"] is True
    assert job.warnings == []
    assert job.state["portal_url"] == "https://ccf-admin-contoso1.azurewebsites.net"
    main = json.loads((tmp_path / ".local/parameters.json").read_text())["parameters"]
    assert main["foundryName"]["value"] == "ccf-foundry-contoso1"
    assert main["inferencePrincipals"]["value"] == [{"id": GROUP, "type": "Group"}]
    portal = json.loads((tmp_path / ".local/portal/parameters.json").read_text())["parameters"]
    assert portal["entraClientId"]["value"] == PORTAL_APP
    assert portal["desktopClientId"]["value"] == DESKTOP
    assert portal["ownerObjectId"]["value"] == USER
    org_config = json.loads((tmp_path / ".local/organization.json").read_text())
    assert org_config["model_provider_data"]["countryCode"] == "US"


def test_rerun_reuses_group_app_registration_and_qualified_models(
    deploy, settings, tmp_path, org, monkeypatch
):
    run_deployment(deploy, settings, tmp_path, FakeRunner(organization(org)), monkeypatch)
    runner = FakeRunner(organization(org), group_exists=True)
    run_deployment(deploy, settings, tmp_path, runner, monkeypatch)
    seen = commands(runner)
    assert not any("ad group create" in c for c in seen)
    assert not any("register_client_app.py" in c for c in seen)
    assert not any("admin-consent" in c for c in seen)
    assert not any(" qualify " in f" {c} " for c in seen)
    assert any("--app-id " + PORTAL_APP in c for c in seen)
    org_config = json.loads((tmp_path / ".local/organization.json").read_text())
    assert org_config["models"] == [{"name": "m1"}]


def test_rerun_qualifies_only_models_not_yet_qualified(
    deploy, settings, tmp_path, org, monkeypatch
):
    run_deployment(deploy, settings, tmp_path, FakeRunner(organization(org)), monkeypatch)
    runner = FakeRunner(organization(org), group_exists=True)
    runner.synced = ["m1", "m2"]
    run_deployment(deploy, settings, tmp_path, runner, monkeypatch)
    qualify = [c for c in runner.calls if "qualify" in c]
    assert len(qualify) == 1 and qualify[0][5].endswith("codex-qualification-retry-config.json")
    retry = json.loads((tmp_path / ".local/codex-qualification-retry-config.json").read_text())
    assert [m["name"] for m in retry["models"]] == ["m2"]
    codex = json.loads((tmp_path / ".local/codex-organization.json").read_text())
    assert [(m["name"], m.get("qualified")) for m in codex["models"]] == [
        ("m1", True),
        ("m2", True),
    ]
    gateways = [c for c in runner.calls if c[-1] == "gateway" and "codex" in c[5]]
    assert len(gateways) == 2


def test_failed_retry_preserves_a_published_default(deploy, settings, tmp_path, org, monkeypatch):
    run_deployment(deploy, settings, tmp_path, FakeRunner(organization(org)), monkeypatch)
    runner = FakeRunner(organization(org), group_exists=True)
    runner.synced = ["m1", "m2"]
    original = runner.tool

    def tool(command, capture):
        if command[:1] == ["admin"] and command[3] == "qualify":
            retry = json.loads(Path(command[2]).read_text())
            assert retry["default_model"] == "m2"
            return 1  # No new model passes; the CLI preserves the retry profile.
        result = original(command, capture)
        if command[:1] == ["admin"] and command[3] == "sync-models" and "codex" in command[2]:
            config = Path(command[2])
            data = json.loads(config.read_text())
            data["default_model"] = "m2"
            write_json(config, data)
        return result

    monkeypatch.setattr(runner, "tool", tool)
    run_deployment(deploy, settings, tmp_path, runner, monkeypatch)
    codex = json.loads((tmp_path / ".local/codex-organization.json").read_text())
    assert codex["default_model"] == "m1"
    assert [m["name"] for m in codex["models"]] == ["m1"]


@pytest.mark.parametrize("field,value", [("subscription_id", USER), ("suffix", "another")])
def test_target_change_stops_before_any_azure_command(
    deploy, settings, tmp_path, org, monkeypatch, field, value
):
    run_deployment(deploy, settings, tmp_path, FakeRunner(organization(org)), monkeypatch)
    changed = json.loads(settings.read_text())
    changed[field] = value
    settings.write_text(json.dumps(changed))
    runner = FakeRunner(organization(org))
    with pytest.raises(deploy.DeployError, match="target changed"):
        run_deployment(deploy, settings, tmp_path, runner, monkeypatch)
    assert runner.calls == []


def test_legacy_customer_profile_is_checked_before_azure(
    deploy, settings, tmp_path, org, monkeypatch
):
    write_json(tmp_path / ".local/organization.json", {**organization(org), "tenant_id": USER})
    runner = FakeRunner(organization(org))
    with pytest.raises(deploy.DeployError, match="another deployment"):
        run_deployment(deploy, settings, tmp_path, runner, monkeypatch)
    assert runner.calls == []


def test_verification_detects_published_claude_governance(
    deploy, settings, tmp_path, org, monkeypatch
):
    runner = FakeRunner(organization(org))
    job = run_deployment(deploy, settings, tmp_path, runner, monkeypatch, managed=True)
    job.verify([tmp_path / ".local/client.json"])
    check = next(c for c in runner.calls if c[3:4] == ["verify"])
    assert "--governed" in check


def test_missing_admin_consent_is_reported_without_stopping(
    deploy, settings, tmp_path, org, monkeypatch
):
    runner = FakeRunner(organization(org), consent_error=deploy.DeployError)
    job = run_deployment(deploy, settings, tmp_path, runner, monkeypatch)
    assert job.state["desktop_client_id"] == DESKTOP
    assert "admin_consent" not in job.state
    assert len(job.warnings) == 1 and f"admin-consent --id {DESKTOP}" in job.warnings[0]
    exports = [c for c in runner.calls if "export-client" in c]
    assert exports and all(DESKTOP in c for c in exports)
    assert any("config-zip" in c for c in runner.calls)


def test_portal_managed_api_is_not_overwritten(deploy, settings, tmp_path, org, monkeypatch):
    runner = FakeRunner(organization(org))
    run_deployment(deploy, settings, tmp_path, runner, monkeypatch, managed=True)
    claude_gateway = [
        c
        for c in runner.calls
        if c[3:4] == ["admin"] and c[-1] == "gateway" and "codex" not in c[5]
    ]
    assert claude_gateway == []


def test_marketplace_terms_must_be_accepted_before_models(
    deploy, settings, tmp_path, org, monkeypatch
):
    value = json.loads(settings.read_text())
    value["accept_marketplace_terms"] = False
    settings.write_text(json.dumps(value))
    runner = FakeRunner(organization(org))
    with pytest.raises(deploy.DeployError, match="Marketplace"):
        run_deployment(deploy, settings, tmp_path, runner, monkeypatch)
    assert not any("--apply" in c for c in commands(runner))


def test_wrong_tenant_requires_sign_in(deploy, settings, tmp_path, org, monkeypatch):
    runner = FakeRunner(organization(org), account_tenant="00000000-0000-0000-0000-000000000099")
    monkeypatch.setattr(
        runner,
        "az",
        lambda args: [] if args[0:2] == ["account", "list"] else {"tenantId": "x", "id": "y"},
    )
    with pytest.raises(deploy.DeployError, match="az login --tenant"):
        run_deployment(deploy, settings, tmp_path, runner, monkeypatch)


@pytest.mark.parametrize(
    "change,message",
    [
        ({"suffix": "Bad_Suffix"}, "suffix"),
        ({"marketplace": {}}, "marketplace"),
        ({"claude": {"enabled": False}}, "portal"),
        ({"apim_sku": "Developer"}, "apim_sku"),
        ({"claude": {"capacity": 0}}, "capacity"),
    ],
)
def test_settings_are_validated_before_azure(deploy, settings, change, message):
    value = json.loads(settings.read_text())
    value.update(change)
    settings.write_text(json.dumps(value))
    with pytest.raises(deploy.DeployError, match=message):
        deploy.load_settings(settings)


def test_organization_refresh_preserves_catalog_edits(deploy, settings, org):
    existing = {
        **org,
        "models": [{"name": "kept"}],
        "excluded_deployments": ["x"],
        "profile_name": "Mine",
    }
    merged = deploy.merge_organization(
        existing,
        {**org, "models": [], "profile_name": "Claude on Azure"},
        deploy.load_settings(settings),
    )
    assert merged["models"] == [{"name": "kept"}]
    assert merged["excluded_deployments"] == ["x"]
    assert merged["profile_name"] == "Mine"
    assert merged["model_provider_data"]["organizationName"] == "Contoso Ltd"


def test_script_exits_cleanly_on_errors(deploy, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(sys, "argv", ["deploy.py", "--settings", str(missing)])
    assert deploy.main() == 1
    assert "rerun the same command" in capsys.readouterr().err
