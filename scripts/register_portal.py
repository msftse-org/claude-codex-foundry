#!/usr/bin/env python3
"""Register a single-tenant portal SPA/API with PKCE; creates no client secret."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from claude_foundry.azure import az_json
from claude_foundry.config import load_org, write_json


def register(config, name, redirects, app_id=None):
    for uri in redirects:
        parsed = urlsplit(uri)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        ):
            raise ValueError("Redirects must use HTTPS or a local development origin")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Redirects cannot contain credentials, query strings, or fragments")
    auth = az_json(
        "account",
        "get-access-token",
        "--subscription",
        config["subscription_id"],
        "--resource",
        "https://graph.microsoft.com",
    )
    if auth["tenant"] != config["tenant_id"]:
        raise ValueError("Azure CLI is signed into the wrong tenant")

    def graph(method, path, body=None):
        request = Request(
            "https://graph.microsoft.com/v1.0/" + path,
            method=method,
            headers={
                "Authorization": "Bearer " + auth["accessToken"],
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        with urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    if app_id:
        app_id = str(UUID(app_id))
        app = graph("GET", "applications?" + urlencode({"$filter": f"appId eq '{app_id}'"}))[
            "value"
        ][0]
    else:
        app = graph("POST", "applications", {"displayName": name, "signInAudience": "AzureADMyOrg"})
    scope_id = next(
        (
            s["id"]
            for s in app.get("api", {}).get("oauth2PermissionScopes", [])
            if s["value"] == "access_as_user"
        ),
        str(uuid4()),
    )
    # Use a dedicated registration. This command intentionally owns its manifest.
    graph(
        "PATCH",
        "applications/" + app["id"],
        {
            "signInAudience": "AzureADMyOrg",
            "identifierUris": ["api://" + app["appId"]],
            "spa": {"redirectUris": redirects},
            "api": {
                "requestedAccessTokenVersion": 2,
                "oauth2PermissionScopes": [
                    {
                        "id": scope_id,
                        "value": "access_as_user",
                        "type": "User",
                        "isEnabled": True,
                        "adminConsentDisplayName": "Use Claude & Codex Admin",
                        "adminConsentDescription": "Access this organization's Claude & Codex administration portal as the signed-in user. The portal enforces each member's assigned role.",
                        "userConsentDisplayName": "Use Claude & Codex Admin",
                        "userConsentDescription": "Use your organization's Claude & Codex administration portal with your assigned permissions.",
                    }
                ],
            },
            "requiredResourceAccess": [
                {
                    "resourceAppId": app["appId"],
                    "resourceAccess": [{"id": scope_id, "type": "Scope"}],
                }
            ],
        },
    )
    existing = graph(
        "GET", "servicePrincipals?" + urlencode({"$filter": f"appId eq '{app['appId']}'"})
    )["value"]
    if not existing:
        graph("POST", "servicePrincipals", {"appId": app["appId"]})
    return {
        "client_id": app["appId"],
        "object_id": app["id"],
        "tenant_id": config["tenant_id"],
        "scope_id": scope_id,
        "redirect_uris": redirects,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--name", default="Claude & Codex Admin Preview")
    parser.add_argument("--redirect-uri", action="append", required=True)
    parser.add_argument(
        "--app-id", help="Update this dedicated application; omit only on first creation"
    )
    parser.add_argument("--output", type=Path, default=Path(".local/portal/entra.json"))
    args = parser.parse_args()
    result = register(load_org(args.organization), args.name, args.redirect_uri, args.app_id)
    write_json(args.output, result)
    print(f"Entra application configured. Client ID: {result['client_id']}. Saved {args.output}.")


if __name__ == "__main__":
    main()
