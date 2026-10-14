#!/usr/bin/env python3
"""Run once by a tenant app administrator for the optional CLI-free sign-in path."""

import argparse
import json
import subprocess


def az(*args):
    p = subprocess.run(
        ["az", *args, "-o", "json", "--only-show-errors"], text=True, capture_output=True
    )
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    return json.loads(p.stdout) if p.stdout.strip() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Claude Foundry Desktop Sign-in")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--grant-admin-consent", action="store_true")
    args = parser.parse_args()
    if az("account", "show")["tenantId"] != args.tenant:
        raise RuntimeError(
            "Azure CLI must be signed into the requested tenant. Run az login --tenant TENANT first."
        )
    services = az(
        "ad",
        "sp",
        "list",
        "--filter",
        "servicePrincipalNames/any(s:s eq 'https://cognitiveservices.azure.com')",
    )
    if len(services) != 1:
        raise RuntimeError("Could not uniquely discover the Cognitive Services service principal")
    service = services[0]
    scope = next(s for s in service["oauth2PermissionScopes"] if s["value"] == "user_impersonation")
    apps = [
        app
        for app in az("ad", "app", "list", "--display-name", args.name)
        if app.get("displayName") == args.name
    ]
    if len(apps) > 1:
        raise RuntimeError("More than one app matches; use a unique --name")
    if apps:
        app = apps[0]
        redirects = app.get("publicClient", {}).get("redirectUris", [])
        if "http://localhost" not in redirects or app.get("signInAudience") != "AzureADMyOrg":
            raise RuntimeError(
                "Existing app is not the expected single-tenant public client; use a different --name"
            )
    else:
        app = az(
            "ad",
            "app",
            "create",
            "--display-name",
            args.name,
            "--sign-in-audience",
            "AzureADMyOrg",
            "--is-fallback-public-client",
            "true",
            "--public-client-redirect-uris",
            "http://localhost",
        )
    az(
        "ad",
        "app",
        "permission",
        "add",
        "--id",
        app["appId"],
        "--api",
        service["appId"],
        "--api-permissions",
        scope["id"] + "=Scope",
    )
    if args.grant_admin_consent:
        az("ad", "app", "permission", "admin-consent", "--id", app["appId"])
    print(
        json.dumps(
            {
                "client_id": app["appId"],
                "tenant_id": args.tenant,
                "admin_consent_requested": args.grant_admin_consent,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
