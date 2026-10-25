"""Shared-runtime orchestration used by the standard deployer and portal-only upgrades."""

import hashlib
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from claude_foundry.azure import AzureError, account_path, api_path
from claude_foundry.config import read_json, write_json
from claude_foundry.models import CS_VERSION
from claude_foundry.unified import provider_configs, reviewed_deployments


def wait_for_route(url, attempts=30, delay=10):
    """Wait for the new APIM operation's authentication guard, without inference."""
    for _ in range(attempts):
        try:
            with urlopen(url, timeout=15):
                pass
        except HTTPError as error:
            if error.code == 401:
                return
        except (URLError, TimeoutError):
            pass
        time.sleep(delay)
    raise RuntimeError(
        "The governed APIM route is not ready; reconcile its operations and policy before distributing profiles"
    )


def prepare(job):
    """Prepare only local files. Existing catalogs and customer state remain untouched."""
    primary = read_json(job.org_path if job.s["claude"]["enabled"] else job.codex_path)
    providers = {}
    for client, path in [("claude", job.org_path), ("codex", job.codex_path)]:
        if not job.s[client]["enabled"]:
            continue
        profile = read_json(path)
        route = f"{client}-governed"
        profile.update(
            api_id=route,
            api_path=route,
            gateway_url=f"https://{urlsplit(primary['gateway_url']).netloc}/{route}"
            + ("/v1" if client == "codex" else ""),
        )
        providers[client] = profile
    providers = provider_configs(primary, providers)
    digest = hashlib.sha256((job.s["subscription_id"] + job.s["suffix"]).encode()).hexdigest()[:6]
    storage_name = f"ccfbud{job.s['suffix']}{digest}"
    runtime_scope = f"/subscriptions/{job.s['subscription_id']}/resourceGroups/{job.n['portal_resource_group']}/providers/Microsoft.Web/sites/ccf-budget-{job.s['suffix']}"
    try:
        existing_runtime = job.azure(job.s["subscription_id"]).request(
            "POST", runtime_scope + "/config/appsettings/list", version="2024-04-01"
        )["properties"]
    except AzureError as error:
        if error.status != 404:
            raise
        existing_runtime = {}
    if existing_runtime and (
        existing_runtime.get("BUDGET_STORAGE_ACCOUNT") != storage_name
        or existing_runtime.get("BUDGET_ORGANIZATION_ID") != job.s["subscription_id"]
        or existing_runtime.get("BUDGET_TENANT_ID") != job.s["tenant_id"]
    ):
        raise ValueError(
            "The existing runtime belongs to a different ledger or tenant; preserve its configuration before upgrading"
        )
    job.unified_runtime_settings = existing_runtime
    policy_path = job.s["portal"].get("model_policy_file")
    prices = read_json(Path(policy_path)) if policy_path else []
    if isinstance(prices, dict):
        prices = prices["budget_prices"]
    if not isinstance(prices, list):
        raise ValueError(
            "The model policy file must contain a reviewed price list or budget_prices"
        )
    deployments = {}
    if prices:
        actual = job.azure(job.s["subscription_id"]).request(
            "GET", account_path(primary) + "/deployments", version=CS_VERSION
        )["value"]
        deployments = reviewed_deployments(providers, prices, actual)
    # Preserve an established runtime manifest if no new model policy was supplied.
    manifest = job.local / "unified/runtime-models.json"
    if not prices and manifest.exists():
        deployments = read_json(manifest)
    if not prices and existing_runtime.get("BUDGET_DEPLOYMENTS_JSON"):
        deployments = json.loads(existing_runtime["BUDGET_DEPLOYMENTS_JSON"])
    write_json(manifest, deployments)
    settings = {
        "budgetStorageAccount": storage_name,
        "budgetOrganizationId": job.s["subscription_id"],
        "providerConfigs": providers,
        "initialBudgetPrices": prices,
    }
    write_json(job.local / "unified/portal-settings.json", settings)
    if not deployments:
        job.warnings.append(
            "Governed inference is closed until a reviewed portal.model_policy_file is supplied and the owner publishes membership, prices and spending policy. See docs/unified-administration.md."
        )
    return primary, settings, deployments


def deploy_runtime(
    job, primary, settings, deployments, portal_identity, content_logs_config=None
):

    job.step("Connecting both providers to shared portal governance")
    app_path = job.local / "unified/entra.json"
    if app_path.exists():
        audience = read_json(app_path)["audience"]
    else:
        name = f"Claude & Codex Governance ({job.s['suffix']})"
        apps = job.az("ad", "app", "list", "--display-name", name)
        apps = [a for a in apps if a["displayName"] == name]
        if len(apps) > 1:
            raise ValueError(
                "Multiple governance applications match; resolve their ownership first"
            )
        app = (
            apps[0]
            if apps
            else job.az(
                "ad", "app", "create", "--display-name", name, "--sign-in-audience", "AzureADMyOrg"
            )
        )
        audience = "api://" + app["appId"]
        if audience not in app.get("identifierUris", []):
            job.az("ad", "app", "update", "--id", app["appId"], "--identifier-uris", audience)
        if not job.az("ad", "sp", "list", "--filter", f"appId eq '{app['appId']}'"):
            job.az("ad", "sp", "create", "--id", app["appId"])
        write_json(app_path, {"audience": audience})
    existing_runtime = getattr(job, "unified_runtime_settings", {})
    if existing_runtime.get("BUDGET_AUDIENCE") and existing_runtime["BUDGET_AUDIENCE"] != audience:
        raise ValueError(
            "The recorded runtime audience differs from the live service; preserve its existing Entra registration"
        )
    apim = job.azure(job.s["subscription_id"]).request("GET", api_path(primary).split("/apis/")[0])
    name = f"ccf-budget-{job.s['suffix']}"
    content_logs_config = content_logs_config or {}
    content_logs_enabled = (
        existing_runtime.get("BUDGET_CONTENT_LOGS_ENABLED", "").lower() == "true"
        or content_logs_config.get("capture_enabled") is True
    )
    params = {
        "name": name,
        "location": job.s["location"],
        "organizationId": settings["budgetOrganizationId"],
        "tenantId": job.s["tenant_id"],
        "audience": audience,
        "apimPrincipalId": apim["identity"]["principalId"],
        "portalPrincipalId": portal_identity,
        "foundryName": primary["foundry_name"],
        "foundryResourceGroup": primary["foundry_resource_group"],
        "deployments": deployments,
        "portalVnetName": job.n["portal"] + "-vnet",
        "privateStorage": True,
        "storageName": settings["budgetStorageAccount"],
        "policyCacheUrl": existing_runtime.get("BUDGET_POLICY_CACHE_URL", ""),
        "contentLogsEnabled": content_logs_enabled,
    }
    path = job.local / "unified/runtime-parameters.json"
    write_json(
        path,
        {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {k: {"value": v} for k, v in params.items()},
        },
    )
    job.az(
        "deployment",
        "group",
        "create",
        "--name",
        "unified-runtime",
        "--resource-group",
        job.n["portal_resource_group"],
        "--template-file",
        Path(__file__).resolve().parents[2] / "infra/budget.bicep",
        "--parameters",
        f"@{path}",
    )
    package = job.dist / "claude-codex-budget.zip"
    job.script("build_budget.py", "--output", package)
    job.run(
        [
            "az",
            "functionapp",
            "deployment",
            "source",
            "config-zip",
            "-g",
            job.n["portal_resource_group"],
            "-n",
            name,
            "--src",
            package,
            "--build-remote",
            "true",
            "--timeout",
            "900",
            "--only-show-errors",
            "-o",
            "none",
        ]
    )
    for client, config in settings["providerConfigs"].items():
        # The helper owns new APIs only; it cannot replace an existing baseline API.
        from claude_foundry.budget_gateway import deploy

        azure = job.azure(job.s["subscription_id"])
        deploy(
            primary,
            azure,
            f"https://{name}.azurewebsites.net",
            audience,
            "openai" if client == "codex" else "anthropic",
            config["api_id"],
            config["api_path"],
        )
        from claude_foundry.gateway import configure_observability

        configure_observability(config, azure, enable_dimensions=True)
        wait_for_route(
            config["gateway_url"].rstrip("/") + ("/models" if client == "codex" else "/v1/models")
        )
        write_json(job.local / f"unified/{client}-organization.json", config)
    job.save(governance_mode="unified", budget_storage=settings["budgetStorageAccount"])
