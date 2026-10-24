#!/usr/bin/env python3
"""Deploy Claude & Codex on Azure from one settings file.

Runs the documented administrator steps in order and prints every command. It is safe
to rerun after fixing a reported problem: existing resources, deployments and app
registrations are reused. Customer files are written to .local/ and dist/.
"""

import argparse
import copy
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID

from claude_foundry.azure import Azure, AzureError, api_path
from claude_foundry.config import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = (
    "Microsoft.ApiManagement",
    "Microsoft.CognitiveServices",
    "Microsoft.Insights",
    "Microsoft.OperationalInsights",
    "Microsoft.Web",
    "Microsoft.App",
    "Microsoft.Storage",
    "Microsoft.Network",
    "Microsoft.ManagedIdentity",
)
INDUSTRIES = {
    "technology",
    "finance",
    "healthcare",
    "education",
    "retail",
    "manufacturing",
    "government",
    "media",
    "other",
}


class DeployError(RuntimeError):
    pass


def run(args, capture=False, allowed=(0,)):
    args = [str(a) for a in args]
    print("$ " + " ".join(shlex.quote(a) for a in args), flush=True)
    result = subprocess.run(args, text=True, capture_output=capture)
    if result.returncode not in allowed:
        detail = (result.stderr or "").strip() if capture else ""
        raise DeployError(f"Command failed with exit code {result.returncode}. {detail}".strip())
    return result.stdout if capture else result.returncode


def _uuid(value, field):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise DeployError(f"{field} must be a UUID") from None


def load_settings(path):
    s = json.loads(Path(path).read_text())
    s["tenant_id"] = _uuid(s.get("tenant_id"), "tenant_id")
    s["subscription_id"] = _uuid(s.get("subscription_id"), "subscription_id")
    if not re.fullmatch(r"[a-z][a-z0-9]{2,11}", s.get("suffix", "")):
        raise DeployError("suffix must be 3-12 lowercase letters/digits, starting with a letter")
    if not s.get("publisher_name") or "@" not in s.get("publisher_email", ""):
        raise DeployError("publisher_name and publisher_email are required")
    s.setdefault("location", "eastus2")
    s.setdefault("apim_location", s["location"])
    s.setdefault("apim_sku", "BasicV2")
    if s["apim_sku"] not in {"BasicV2", "StandardV2"}:
        raise DeployError("apim_sku must be BasicV2 or StandardV2")
    s.setdefault("auth_mode", "msal")
    if s["auth_mode"] not in {"msal", "azure-cli"}:
        raise DeployError("auth_mode must be msal or azure-cli")
    s.setdefault("grant_admin_consent", True)
    s.setdefault("add_deployer_to_developers", True)
    s.setdefault("allowed_object_ids", [])
    for oid in s["allowed_object_ids"]:
        _uuid(oid, "allowed_object_ids entry")
    if s.get("developer_group_id"):
        s["developer_group_id"] = _uuid(s["developer_group_id"], "developer_group_id")
    s.setdefault("developer_group_name", "Claude & Codex developers")
    claude = {"enabled": True, "models": [], "capacity": 10, "sku": "GlobalStandard"}
    codex = {"enabled": True, "models": [], "capacity": 10, "sku": None}
    s["claude"] = {**claude, **s.get("claude", {})}
    s["codex"] = {**codex, **s.get("codex", {})}
    s["portal"] = {"enabled": True, **s.get("portal", {})}
    if not s["claude"]["enabled"] and not s["codex"]["enabled"]:
        raise DeployError("Enable Claude, Codex or both")
    if s["portal"]["enabled"] and not s["claude"]["enabled"]:
        raise DeployError("The administration portal manages the Claude route; enable Claude")
    if s["claude"]["sku"] not in {"GlobalStandard", "DataZoneStandard"}:
        raise DeployError("claude.sku must be GlobalStandard or DataZoneStandard")
    for section in ("claude", "codex"):
        if type(s[section]["capacity"]) is not int or s[section]["capacity"] < 1:
            raise DeployError(f"{section}.capacity must be a positive integer")
    if s["claude"]["enabled"]:
        market = s.get("marketplace") or {}
        if (
            not market.get("organizationName")
            or not re.fullmatch(r"[A-Z]{2}", market.get("countryCode", ""))
            or market.get("industry") not in INDUSTRIES
        ):
            raise DeployError(
                "marketplace needs organizationName, a two-letter countryCode and an industry "
                "(" + ", ".join(sorted(INDUSTRIES)) + ") for Anthropic Marketplace attestation"
            )
    suffix = s["suffix"]
    names = {
        "resource_group": f"rg-ccf-{suffix}",
        "portal_resource_group": f"rg-ccf-{suffix}-admin",
        "foundry": f"ccf-foundry-{suffix}",
        "apim": f"ccf-apim-{suffix}",
        "insights": "ccf-insights",
        "logs": "ccf-logs",
        "portal": f"ccf-admin-{suffix}",
        "desktop_app": f"Claude & Codex Sign-in ({suffix})",
        "portal_app": f"Claude & Codex Admin ({suffix})",
    }
    s["names"] = {**names, **s.get("names", {})}
    return s


class Deployment:
    def __init__(self, settings, local=Path(".local"), dist=Path("dist"), runner=run, azure=Azure):
        self.s = settings
        self.n = settings["names"]
        self.local = Path(local).resolve()
        self.dist = Path(dist).resolve()
        self.run = runner
        self.azure = azure
        self.state_path = self.local / "deploy-state.json"
        self.state = read_json(self.state_path, default={"schema_version": 1})
        self.warnings = []
        self.org_path = self.local / "organization.json"
        self.codex_path = self.local / "codex-organization.json"

    def save(self, **values):
        self.state.update(values)
        write_json(self.state_path, self.state)

    def az(self, *args):
        out = self.run(["az", *args, "-o", "json", "--only-show-errors"], capture=True)
        return json.loads(out) if out and out.strip() else None

    def cli(self, *args, allowed=(0,)):
        return self.run([sys.executable, "-m", "claude_foundry.cli", *args], allowed=allowed)

    def script(self, name, *args, capture=False):
        return self.run([sys.executable, ROOT / "scripts" / name, *args], capture=capture)

    def step(self, title):
        print(f"\n== {title}", flush=True)

    def sign_in(self):
        self.step("Checking Azure CLI sign-in")
        account = self.az("account", "show")
        if account["tenantId"] != self.s["tenant_id"] or account["id"] != self.s["subscription_id"]:
            subscriptions = self.az("account", "list", "--query", "[].{id:id, tenantId:tenantId}")
            if not any(
                a["id"] == self.s["subscription_id"] and a["tenantId"] == self.s["tenant_id"]
                for a in subscriptions
            ):
                raise DeployError(
                    f"Sign in first: az login --tenant {self.s['tenant_id']}, then rerun this command"
                )
            self.run(["az", "account", "set", "--subscription", self.s["subscription_id"]])
        user = self.az("ad", "signed-in-user", "show")
        self.user = {
            "id": user["id"],
            "name": user.get("displayName") or user["userPrincipalName"],
            "email": user.get("mail") or user["userPrincipalName"],
        }

    def providers(self):
        self.step("Registering Azure resource providers")
        for namespace in PROVIDERS:
            state = self.az(
                "provider", "show", "--namespace", namespace, "--query", "registrationState"
            )
            if state != "Registered":
                self.run(["az", "provider", "register", "--namespace", namespace, "--wait"])

    def developer_group(self):
        self.step("Preparing the developer access group")
        group = self.s.get("developer_group_id") or self.state.get("developer_group_id")
        if group:
            self.az("ad", "group", "show", "--group", group)
        else:
            name = self.s["developer_group_name"]
            found = [
                g
                for g in self.az("ad", "group", "list", "--display-name", name)
                if g["displayName"] == name
            ]
            if len(found) > 1:
                raise DeployError(f"Several groups are named {name!r}; set developer_group_id")
            if found:
                group = found[0]["id"]
            else:
                nickname = re.sub(r"[^A-Za-z0-9]", "", name)[:64] or "claudecodexdevelopers"
                group = self.az(
                    "ad",
                    "group",
                    "create",
                    "--display-name",
                    name,
                    "--mail-nickname",
                    nickname,
                    "--description",
                    "Developers allowed to use Claude & Codex on Azure",
                )["id"]
        self.save(developer_group_id=group)
        if self.s["add_deployer_to_developers"]:
            member = self.az(
                "ad", "group", "member", "check", "--group", group, "--member-id", self.user["id"]
            )
            if not member["value"]:
                self.run(
                    [
                        "az",
                        "ad",
                        "group",
                        "member",
                        "add",
                        "--group",
                        group,
                        "--member-id",
                        self.user["id"],
                    ]
                )
        return group

    def foundation(self, group):
        self.step("Deploying Foundry, API Management and monitoring (APIM can take 30+ minutes)")
        parameters = {
            "location": self.s["location"],
            "apimLocation": self.s["apim_location"],
            "foundryName": self.n["foundry"],
            "apimName": self.n["apim"],
            "apimSku": self.s["apim_sku"],
            "publisherName": self.s["publisher_name"],
            "publisherEmail": self.s["publisher_email"],
            "applicationInsightsName": self.n["insights"],
            "logAnalyticsName": self.n["logs"],
            "inferencePrincipals": [{"id": group, "type": "Group"}],
            "allowedObjectIds": self.s["allowed_object_ids"],
        }
        path = self.local / "parameters.json"
        write_json(path, deployment_parameters(parameters))
        self.az(
            "group", "create", "--name", self.n["resource_group"], "--location", self.s["location"]
        )
        output = self.az(
            "deployment",
            "group",
            "create",
            "--name",
            "claude-foundation",
            "--resource-group",
            self.n["resource_group"],
            "--template-file",
            ROOT / "infra/main.bicep",
            "--parameters",
            f"@{path}",
            "--query",
            "properties.outputs.organizationConfig.value",
        )
        write_json(
            self.org_path, merge_organization(read_json(self.org_path, default={}), output, self.s)
        )

    def claude(self):
        self.step("Deploying Claude models and the Claude gateway")
        if not self.s.get("accept_marketplace_terms"):
            raise DeployError(
                "Claude models are paid Azure Marketplace offers. Review docs/administrator.md, "
                "then set accept_marketplace_terms to true and rerun"
            )
        config = self.org_path
        args = ["admin", "--config", config, "models", "--capacity", self.s["claude"]["capacity"]]
        args += ["--sku", self.s["claude"]["sku"], "--apply", "--accept-marketplace-terms"]
        args += ["--wait-seconds", 900, "--report", self.local / "models-report.json"]
        for model in self.s["claude"]["models"]:
            args += ["--model", model]
        if self.cli(*args, allowed=(0, 2)) == 2:
            print("Some Claude deployments failed or are pending; see .local/models-report.json")
        self.cli("admin", "--config", config, "sync-models")
        if self.portal_managed(read_json(config)):
            print("Skipping the baseline gateway: this API is published by the portal.")
        else:
            self.cli("admin", "--config", config, "gateway")
        self.cli("admin", "--config", config, "observability", "--enable-dimensions")

    def portal_managed(self, org):
        try:
            policy = self.azure(org["subscription_id"]).request(
                "GET", api_path(org) + "/policies/policy"
            )
        except AzureError as error:
            if error.status == 404:
                return False
            raise
        return 'name="governance"' in policy["properties"]["value"]

    def codex(self):
        self.step("Deploying Codex models and the Codex gateway")
        config = self.codex_path
        if not config.exists():
            self.cli("admin", "--config", self.org_path, "init-codex", "--output", config)
        args = ["admin", "--config", config, "models", "--capacity", self.s["codex"]["capacity"]]
        args += ["--apply", "--report", self.local / "codex-deployment-report.json"]
        if self.s["codex"]["sku"]:
            args += ["--sku", self.s["codex"]["sku"]]
        for model in self.s["codex"]["models"]:
            args += ["--model", model]
        if self.cli(*args, allowed=(0, 2)) == 2:
            print(
                "Some Codex deployments failed or are pending; see .local/codex-deployment-report.json"
            )
        self.cli("admin", "--config", config, "sync-models")
        self.cli("admin", "--config", config, "gateway")
        self.cli("admin", "--config", config, "observability")
        org = read_json(config)
        pending = [m for m in org["models"] if m.get("qualified") is not True]
        if pending:
            # Three small billable requests per model; failing models are left unpublished.
            if len(pending) == len(org["models"]):
                code = self.cli(
                    "admin",
                    "--config",
                    config,
                    "qualify",
                    "--report",
                    self.local / "codex-qualification.json",
                    "--publish-passed",
                    allowed=(0, 2),
                )
            else:
                # Retry only models not yet qualified, e.g. after a transient network error.
                retry = self.local / "codex-qualification-retry-config.json"
                write_json(retry, {**org, "models": pending})
                code = self.cli(
                    "admin",
                    "--config",
                    retry,
                    "qualify",
                    "--report",
                    self.local / "codex-qualification-retry.json",
                    "--publish-passed",
                    allowed=(0, 1, 2),
                )
                passed = set() if code == 1 else {m["name"] for m in read_json(retry)["models"]}
                org["models"] = [
                    dict(m, qualified=True)
                    for m in org["models"]
                    if m.get("qualified") is True or m["name"] in passed
                ]
                write_json(config, org)
            if code != 0:
                print("Some Codex models failed qualification and were not published.")
            self.cli("admin", "--config", config, "gateway")

    def desktop_app(self):
        if self.s["auth_mode"] != "msal":
            return None
        client_id = self.state.get("desktop_client_id")
        if not client_id:
            self.step("Registering the developer sign-in application (no secrets)")
            output = self.script(
                "register_client_app.py",
                "--tenant",
                self.s["tenant_id"],
                "--name",
                self.n["desktop_app"],
                capture=True,
            )
            client_id = json.loads(output)["client_id"]
            self.save(desktop_client_id=client_id)
        if self.s["grant_admin_consent"] and not self.state.get("admin_consent"):
            try:
                self.run(
                    ["az", "ad", "app", "permission", "admin-consent", "--id", client_id],
                    capture=True,
                )
                self.save(admin_consent=True)
            except DeployError:
                # Deployment can finish; developers sign in once an administrator consents.
                self.warnings.append(
                    "Developer sign-in needs tenant-wide admin consent. Ask an Entra administrator "
                    f"to run: az ad app permission admin-consent --id {client_id}"
                )
        return client_id

    def profiles(self, client_id):
        self.step("Exporting developer profiles and the installer")
        auth = ["--auth-mode", self.s["auth_mode"]] + (
            ["--client-id", client_id] if client_id else []
        )
        profiles = []
        for enabled, config, output in [
            (self.s["claude"]["enabled"], self.org_path, self.local / "client.json"),
            (self.s["codex"]["enabled"], self.codex_path, self.local / "codex-client.json"),
        ]:
            if enabled:
                self.cli("admin", "--config", config, "export-client", "--output", output, *auth)
                profiles.append(output)
        bundle = self.dist / f"claude-codex-{self.s['suffix']}-macos.zip"
        args = []
        for profile in profiles:
            args += ["--profile", profile]
        self.script("build_client_bundle.py", *args, "--output", bundle)
        self.save(bundle=str(bundle))
        return profiles

    def portal(self, client_id):
        self.step("Deploying the administration portal on the Claude route")
        portal_dir = self.local / "portal"
        config = portal_dir / "organization.json"
        self.script("prepare_portal.py", "--organization", self.org_path, "--output", config)
        entra = portal_dir / "entra.json"
        url = f"https://{self.n['portal']}.azurewebsites.net"
        args = ["--organization", config, "--name", self.n["portal_app"]]
        args += ["--redirect-uri", url + "/", "--output", entra]
        if entra.exists():
            args += ["--app-id", read_json(entra)["client_id"]]
        self.script("register_portal.py", *args)
        parameters = {
            "portalName": self.n["portal"],
            "location": self.s["location"],
            "organization": read_json(config),
            "ownerObjectId": self.user["id"],
            "ownerName": self.user["name"],
            "ownerEmail": self.user["email"],
            "entraClientId": read_json(entra)["client_id"],
            "desktopClientId": client_id or "",
            "insightsName": self.n["insights"],
            "insightsResourceGroup": self.n["resource_group"],
            "privateStorage": True,
        }
        path = portal_dir / "parameters.json"
        write_json(path, deployment_parameters(parameters))
        group = self.n["portal_resource_group"]
        self.az("group", "create", "--name", group, "--location", self.s["location"])
        self.az(
            "deployment",
            "group",
            "create",
            "--name",
            "admin-portal",
            "--resource-group",
            group,
            "--template-file",
            ROOT / "infra/portal.bicep",
            "--parameters",
            f"@{path}",
            "--query",
            "properties.provisioningState",
        )
        package = self.dist / "claude-foundry-admin-preview.zip"
        self.script("build_portal.py", "--output", package)
        self.run(
            [
                "az",
                "functionapp",
                "deployment",
                "source",
                "config-zip",
                "--resource-group",
                group,
                "--name",
                self.n["portal"],
                "--src",
                package,
                "--build-remote",
                "true",
                "--timeout",
                900,
                "--only-show-errors",
                "--output",
                "none",
            ]
        )
        wait_for_health(url + "/api/health")
        self.save(portal_url=url)

    def verify(self, profiles):
        self.step("Verifying the gateways with your Azure CLI sign-in (small billable requests)")
        for profile in profiles:
            config = read_json(profile)
            config["auth_mode"] = "azure-cli"
            config.pop("client_id", None)
            path = self.local / "verify" / profile.name
            write_json(path, config)
            args = ["verify", "--config", path]
            if config.get("client_type") != "codex":
                args.append("--all-models")
            # New group membership can take a few minutes to reach Foundry authorization.
            for attempt in range(4):
                if self.cli(*args, allowed=(0, 1, 2)) == 0:
                    break
                if attempt == 3:
                    raise DeployError(
                        f"Verification failed for {profile.name}; see the output above"
                    )
                print("Waiting 90 seconds for access to propagate, then retrying.", flush=True)
                time.sleep(90)

    def deploy(self, verify=True):
        self.sign_in()
        self.providers()
        group = self.developer_group()
        self.foundation(group)
        if self.s["claude"]["enabled"]:
            self.claude()
        if self.s["codex"]["enabled"]:
            self.codex()
        client_id = self.desktop_app()
        profiles = self.profiles(client_id)
        if self.s["portal"]["enabled"]:
            self.portal(client_id)
        if verify:
            self.verify(profiles)
        self.summary(group)

    def summary(self, group):
        org = read_json(self.org_path)
        print("\nDeployment complete.")
        if self.s["claude"]["enabled"]:
            print(f"  Claude gateway:  {org['gateway_url']}")
        if self.s["codex"]["enabled"]:
            print(f"  Codex gateway:   {read_json(self.codex_path)['gateway_url']}")
        if self.state.get("portal_url"):
            print(f"  Portal:          {self.state['portal_url']} (owner: {self.user['email']})")
        print(f"  Developer group: {self.s['developer_group_name']} ({group})")
        print(f"  Installer ZIP:   {self.state['bundle']}")
        for warning in self.warnings:
            print(f"\nAction needed: {warning}")
        print("\nOnboard developers:")
        print("  uv run python scripts/onboard_users.py alice@example.com bob@example.com")
        print("  Send them the installer ZIP. They open Install.command and sign in once.")


def deployment_parameters(values):
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in values.items()},
    }


def merge_organization(existing, deployed, settings):
    """Refresh infrastructure fields while preserving the administrator's catalog edits."""
    org = {**copy.deepcopy(existing), **copy.deepcopy(deployed)}
    for key in ("models", "excluded_deployments", "conversation_import", "profile_name"):
        if key in existing:
            org[key] = copy.deepcopy(existing[key])
    if settings.get("profile_name") and "profile_name" not in existing:
        org["profile_name"] = settings["profile_name"]
    if settings["claude"]["enabled"]:
        org["model_provider_data"] = copy.deepcopy(settings["marketplace"])
    return org


def wait_for_health(url, attempts=40, delay=15):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                if response.status == 200:
                    print(f"Healthy: {url}")
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(delay)
    raise DeployError(f"{url} did not become healthy; check the Function app deployment logs")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--settings", type=Path, default=Path(".local/deploy.json"))
    parser.add_argument("--skip-verify", action="store_true", help="Skip billable gateway checks")
    args = parser.parse_args()
    try:
        Deployment(load_settings(args.settings)).deploy(verify=not args.skip_verify)
    except (DeployError, AzureError, OSError, ValueError, KeyError) as error:
        print(f"\nStopped: {error}", file=sys.stderr)
        print(
            "Fix the problem and rerun the same command; completed steps are reused.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
