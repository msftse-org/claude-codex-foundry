import importlib.util
import io
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
        portal={"enabled": True, "mode": "baseline"},
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
    monkeypatch.setattr(deploy, "verify_portal_setup", lambda url, clients: None)

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
    value = json.loads(settings.read_text())
    value["auth_mode"] = "msal"
    settings.write_text(json.dumps(value))
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
        "scale_model_capacity.py --config",
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
    assert sum("scale_model_capacity.py" in command for command in seen) == 1
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


def test_capacity_maximization_can_be_disabled(
    deploy, settings, tmp_path, org, monkeypatch
):
    value = json.loads(settings.read_text())
    value["maximize_model_capacity"] = False
    settings.write_text(json.dumps(value))
    runner = FakeRunner(organization(org))

    run_deployment(deploy, settings, tmp_path, runner, monkeypatch)

    assert not any("scale_model_capacity.py" in command for command in commands(runner))


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
    value = json.loads(settings.read_text())
    value["auth_mode"] = "msal"
    settings.write_text(json.dumps(value))
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


def test_unified_is_default_and_codex_only_portal_is_supported(deploy, settings):
    value = json.loads(settings.read_text())
    value.pop("portal")
    value["claude"]["enabled"] = False
    settings.write_text(json.dumps(value))
    result = deploy.load_settings(settings)
    assert result["portal"] == {"enabled": True, "mode": "unified"}
    assert result["auth_mode"] == "azure-cli"


class UnifiedRunner(FakeRunner):
    def az(self, args):
        joined = " ".join(args)
        if joined.startswith("deployment group create") and "admin-portal" in args:
            return {"identityObjectId": {"value": PORTAL_APP}}
        if joined.startswith("ad app list"):
            return []
        if joined.startswith("ad app create"):
            return {"appId": PORTAL_APP}
        if joined.startswith("ad sp list"):
            return []
        return super().az(args)

    def tool(self, command, capture):
        result = super().tool(command, capture)
        if command[:1] == ["admin"]:
            path = Path(command[2])
            if command[3] == "init-codex":
                from claude_foundry.codex_models import initialize

                write_json(Path(command[5]), initialize(self.org))
            if command[3] == "sync-models":
                data = json.loads(path.read_text())
                if data.get("client_type") == "codex":
                    data["models"] = [{"name": "gpt-test", "model": "gpt-test", "qualified": True}]
                    data["default_model"] = "gpt-test"
                else:
                    data["models"] = [
                        {
                            "name": "claude-test",
                            "anthropicFamilyTier": "sonnet",
                            "labelOverride": "Claude test",
                            "isFamilyDefault": True,
                        }
                    ]
                write_json(path, data)
        return result


@pytest.fixture
def unified_job(deploy, settings, tmp_path, org, monkeypatch):
    value = json.loads(settings.read_text())
    value["portal"] = {"enabled": True}
    settings.write_text(json.dumps(value))
    runner = UnifiedRunner(organization(org))
    cloud = []

    class FakeAzure:
        def __init__(self, subscription):
            pass

        def request(self, method, path, body=None, **kwargs):
            cloud.append((method, path, body))
            if path.endswith("/config/appsettings/list") or path.endswith("/policies/policy"):
                raise AzureError(404, "missing")
            return {"identity": {"principalId": GROUP}}

    monkeypatch.setattr(deploy, "wait_for_health", lambda url: None)
    monkeypatch.setattr(
        deploy, "verify_portal_setup", lambda url, clients: cloud.append(("setup", url, clients))
    )
    monkeypatch.setattr(
        "claude_foundry.budget_gateway.deploy",
        lambda *args: cloud.append(("route", args[-2], args[-1])),
    )
    monkeypatch.setattr("claude_foundry.gateway.configure_observability", lambda *args, **kw: None)
    monkeypatch.setattr("claude_foundry.unified_deployment.wait_for_route", lambda url: None)
    job = deploy.Deployment(
        deploy.load_settings(settings), tmp_path / ".local", tmp_path / "dist", runner, FakeAzure
    )
    return job, runner, cloud


def test_default_deployment_connects_both_catalogs_and_installer(unified_job):
    job, runner, cloud = unified_job
    job.deploy(verify=True)
    params = json.loads((job.local / "portal/parameters.json").read_text())["parameters"]
    assert set(params["providerConfigs"]["value"]) == {"claude", "codex"}
    assert ("setup", "https://ccf-admin-contoso1.azurewebsites.net", ["claude", "codex"]) in cloud
    assert params["organization"]["value"]["gateway_url"].endswith("/claude-governed")
    assert params["budgetStorageAccount"]["value"] == job.state["budget_storage"]
    assert params["providerConfigs"]["value"]["codex"]["gateway_url"].endswith("/codex-governed/v1")
    assert [(c[1], c[2]) for c in cloud if c[0] == "route"] == [
        ("claude-governed", "claude-governed"),
        ("codex-governed", "codex-governed"),
    ]
    assert len([c for c in runner.calls if "unified-runtime" in c]) == 1
    main = json.loads((job.local / "parameters.json").read_text())["parameters"]
    assert main["inferencePrincipals"]["value"] == [{"id": USER, "type": "User"}]
    assert all(
        "unified/" in c[c.index("--config") + 1] for c in runner.calls if "export-client" in c
    )
    assert not any(c[3:4] == ["verify"] for c in runner.calls)
    assert any("closed until" in w for w in job.warnings)
    assert job.state["governance_mode"] == "unified"


def test_deployment_enables_stream_capture_with_content_logs(unified_job):
    job, _, _ = unified_job
    portal = job.local / "portal"
    portal.mkdir(parents=True)
    (portal / "parameters.json").write_text(
        json.dumps(
            {
                "parameters": {
                    "contentLogsConfig": {
                        "value": {
                            "capture_enabled": True,
                            "workspace_id": "00000000-0000-0000-0000-000000000001",
                        }
                    }
                }
            }
        )
    )
    job.deploy(verify=False)
    params = json.loads(
        (job.local / "unified/runtime-parameters.json").read_text()
    )["parameters"]
    assert params["contentLogsEnabled"]["value"] is True


def test_upgrade_does_not_redeploy_foundation_models_or_replace_owner(unified_job):
    job, runner, _ = unified_job
    job.deploy(verify=False)
    path = job.local / "portal/parameters.json"
    params = json.loads(path.read_text())
    params["parameters"]["ownerObjectId"]["value"] = GROUP
    params["parameters"]["storageAccountName"] = {"value": "preservedportalstore"}
    path.write_text(json.dumps(params))
    runner.calls.clear()
    job.upgrade_portal()
    seen = commands(runner)
    assert not any("claude-foundation" in c or "sync-models" in c or "qualify" in c for c in seen)
    assert not any("ad group member" in c or "register_client_app" in c for c in seen)
    params = json.loads(path.read_text())["parameters"]
    assert params["ownerObjectId"]["value"] == GROUP
    assert params["storageAccountName"]["value"] == "preservedportalstore"


def test_azure_cli_upgrade_does_not_export_a_retained_msal_client(unified_job):
    job, runner, _ = unified_job
    job.deploy(verify=False)
    job.save(desktop_client_id=DESKTOP)
    job.upgrade_portal()
    params = json.loads((job.local / "portal/parameters.json").read_text())["parameters"]
    assert params["desktopClientId"]["value"] == ""
    exports = [call for call in runner.calls if "export-client" in call]
    assert exports
    assert all(call[call.index("--auth-mode") + 1] == "azure-cli" for call in exports)
    assert all("--client-id" not in call for call in exports)


def test_upgrade_refuses_to_replace_a_live_ledger(unified_job):
    job, runner, _ = unified_job
    job.deploy(verify=False)
    original = job.azure

    class OtherLedger(original):
        def request(self, method, path, body=None, **kwargs):
            if path.endswith("/config/appsettings/list"):
                return {
                    "properties": {
                        "BUDGET_STORAGE_ACCOUNT": "otherledger",
                        "BUDGET_ORGANIZATION_ID": SUBSCRIPTION,
                    }
                }
            return super().request(method, path, body, **kwargs)

    job.azure = OtherLedger
    runner.calls.clear()
    with pytest.raises(Exception, match="different.*ledger"):
        job.upgrade_portal()
    assert not any("deployment group create" in c for c in commands(runner))


@pytest.mark.parametrize(
    "status",
    [
        {"status": "ok"},
        {"status": "ok", "setup_ready": False, "client_types": ["claude", "codex"]},
        {"status": "ok", "setup_ready": True, "client_types": ["claude"]},
        {
            "status": "ok",
            "setup_ready": True,
            "client_types": ["claude", "codex"],
            "bundle_ready": False,
        },
    ],
)
def test_healthy_code_without_gateway_configuration_is_not_a_complete_deployment(
    deploy, monkeypatch, status
):
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps(status).encode()),
    )
    with pytest.raises(deploy.DeployError, match="client setup is not connected"):
        deploy.verify_portal_setup("https://portal.example.com", ["claude", "codex"])


def test_deployment_verifies_the_enabled_client_downloads(deploy, monkeypatch):
    status = {
        "status": "ok",
        "setup_ready": True,
        "client_types": ["codex", "claude"],
        "bundle_ready": True,
    }
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps(status).encode()),
    )
    deploy.verify_portal_setup("https://portal.example.com", ["claude", "codex"])


def test_unified_mode_cannot_be_downgraded(unified_job):
    job, runner, _ = unified_job
    job.deploy(verify=False)
    job.s["portal"]["mode"] = "baseline"
    runner.calls.clear()
    with pytest.raises(Exception, match="downgraded"):
        job.deploy()
    assert runner.calls == []


def test_codex_only_default_deployment_keeps_portal(unified_job):
    job, runner, cloud = unified_job
    job.s["claude"]["enabled"] = False
    job.deploy(verify=False)
    params = json.loads((job.local / "portal/parameters.json").read_text())["parameters"]
    assert set(params["providerConfigs"]["value"]) == {"codex"}
    assert [(c[1], c[2]) for c in cloud if c[0] == "route"] == [
        ("codex-governed", "codex-governed")
    ]


def test_missing_local_manifest_recovers_existing_runtime_bounds(unified_job):
    job, runner, cloud = unified_job
    job.deploy(verify=False)
    original = job.azure
    from claude_foundry.unified_deployment import prepare

    settings = json.loads((job.local / "unified/portal-settings.json").read_text())
    manifest = {
        "kept": {
            "version": "1",
            "provider": "openai",
            "max_input_tokens": 1000,
            "max_output_tokens": 1000,
        }
    }

    class ExistingRuntime(original):
        def request(self, method, path, body=None, **kwargs):
            if "/sites/ccf-budget-" in path and path.endswith("/config/appsettings/list"):
                return {
                    "properties": {
                        "BUDGET_STORAGE_ACCOUNT": settings["budgetStorageAccount"],
                        "BUDGET_ORGANIZATION_ID": SUBSCRIPTION,
                        "BUDGET_TENANT_ID": TENANT,
                        "BUDGET_DEPLOYMENTS_JSON": json.dumps(manifest),
                    }
                }
            return super().request(method, path, body, **kwargs)

    job.azure = ExistingRuntime
    (job.local / "unified/runtime-models.json").unlink()
    assert prepare(job)[2] == manifest
