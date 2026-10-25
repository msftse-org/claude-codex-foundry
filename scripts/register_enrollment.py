#!/usr/bin/env python3
"""Prepare dedicated single-tenant enrollment registrations; --apply writes Entra.

Never updates the portal or legacy Azure CLI registration. Keep the receipt in
this customer's ignored .local directory and reuse it after an interrupted run.
"""

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from claude_foundry.azure import az_json
from claude_foundry.local_update import _read, _replace

SCOPE = "configuration.read"
MARKER = "claude-foundry-enrollment-v1:"


def uuid(value):
    return str(UUID(value))


class Graph:
    def __init__(self, tenant, subscription):
        auth = az_json(
            "account",
            "get-access-token",
            "--subscription",
            subscription,
            "--resource",
            "https://graph.microsoft.com",
        )
        if auth["tenant"] != tenant:
            raise ValueError("Azure CLI must be signed into the requested tenant.")
        self.token = auth["accessToken"]

    def __call__(self, method, path, body=None):
        request = Request(
            "https://graph.microsoft.com/v1.0/" + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ValueError("Entra response exceeds the supported size.")
                return json.loads(raw) if raw else None
        except HTTPError as error:
            # Do not print Graph response bodies, tokens or operator identity.
            raise RuntimeError(
                f"Entra request returned HTTP {error.code}; reuse this receipt after review."
            ) from None
        except URLError:
            raise RuntimeError(
                "Entra request outcome is uncertain; reuse this receipt, do not create a new one."
            ) from None


def unique(graph, collection, expression):
    response = graph("GET", collection + "?" + urlencode({"$filter": expression}))
    if "@odata.nextLink" in response or len(response["value"]) > 1:
        raise ValueError("Registration lookup is ambiguous; stop for administrator review.")
    return response["value"][0] if response["value"] else None


def cognitive_permission(graph):
    service = unique(
        graph,
        "servicePrincipals",
        "servicePrincipalNames/any(s:s eq 'https://cognitiveservices.azure.com')",
    )
    scopes = (
        [
            s
            for s in service["oauth2PermissionScopes"]
            if s["value"] == "user_impersonation" and s["isEnabled"] is True
        ]
        if service
        else []
    )
    if len(scopes) != 1:
        raise ValueError("Could not uniquely discover delegated Cognitive Services permission.")
    return {
        "resourceAppId": uuid(service["appId"]),
        "resourceAccess": [{"id": uuid(scopes[0]["id"]), "type": "Scope"}],
    }


def manifest(receipt, role, cognitive, resource_id=None):
    value = {
        "displayName": receipt["name"] + (" Configuration API" if role == "api" else " Enrollment"),
        "signInAudience": "AzureADMyOrg",
        "tags": [MARKER + receipt["registration_id"] + ":" + role],
        "isFallbackPublicClient": False,
        "publicClient": {"redirectUris": [] if role == "api" else ["http://localhost"]},
        "web": {"redirectUris": []},
        "spa": {"redirectUris": []},
        "identifierUris": [],
        "appRoles": [],
        "requiredResourceAccess": [],
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [],
            "preAuthorizedApplications": [],
            "knownClientApplications": [],
        },
    }
    if role == "api":
        value["api"]["oauth2PermissionScopes"] = [
            {
                "id": receipt["scope_id"],
                "value": SCOPE,
                "type": "Admin",
                "isEnabled": True,
                "adminConsentDisplayName": "Read own organization client configuration",
                "adminConsentDescription": (
                    "Read the signed-in user's currently published model configuration. "
                    "Does not grant administration or inference access."
                ),
            }
        ]
    else:
        value["requiredResourceAccess"] = [
            {
                "resourceAppId": resource_id,
                "resourceAccess": [{"id": receipt["scope_id"], "type": "Scope"}],
            },
            cognitive,
        ]
    return value


def verify(app, expected, *, resource=False):
    """Refuse drift, never repair or adopt another application's manifest."""
    for key in (
        "displayName",
        "signInAudience",
        "tags",
        "isFallbackPublicClient",
        "appRoles",
    ):
        if app.get(key) != expected[key]:
            raise ValueError("Dedicated registration changed; stop for administrator review.")
    for key in ("passwordCredentials", "keyCredentials"):
        if app.get(key):
            raise ValueError("Enrollment registrations must not have credentials.")
    for platform in ("web", "spa", "publicClient"):
        actual = app.get(platform, {})
        if actual.get("redirectUris", []) != expected[platform]["redirectUris"]:
            raise ValueError("Dedicated registration redirect configuration changed.")
        if platform == "web" and any(
            actual.get("implicitGrantSettings", {}).get(flag, False)
            for flag in ("enableIdTokenIssuance", "enableAccessTokenIssuance")
        ):
            raise ValueError("Implicit token issuance must be disabled.")
    if app.get("api", {}).get("requestedAccessTokenVersion") != 2:
        raise ValueError("Dedicated registrations require v2 access tokens.")
    for key in ("preAuthorizedApplications", "knownClientApplications"):
        if app.get("api", {}).get(key, []) != []:
            raise ValueError("Unexpected registration preauthorization.")
    actual_scopes = app.get("api", {}).get("oauth2PermissionScopes", [])
    expected_scopes = expected["api"]["oauth2PermissionScopes"]
    keys = ("id", "value", "type", "isEnabled")
    if [{k: s.get(k) for k in keys} for s in actual_scopes] != [
        {k: s[k] for k in keys} for s in expected_scopes
    ]:
        raise ValueError("Dedicated registration scopes changed.")
    permissions = lambda entries: sorted(  # noqa: E731
        (e["resourceAppId"], sorted((p["id"], p["type"]) for p in e["resourceAccess"]))
        for e in entries
    )
    if permissions(app.get("requiredResourceAccess", [])) != permissions(
        expected["requiredResourceAccess"]
    ):
        raise ValueError("Dedicated registration permissions changed.")
    allowed = [[], ["api://" + app["appId"]]] if resource else [[]]
    if app.get("identifierUris", []) not in allowed:
        raise ValueError("Dedicated registration resource URI changed.")


def prepare(graph, tenant, name, output, *, apply=False, consent=False):
    if consent and not apply:
        raise ValueError("--grant-admin-consent requires --apply.")
    output = Path(output).absolute()
    raw = _read(output)
    if raw is None:
        receipt = {
            "schema_version": 1,
            "tenant_id": uuid(tenant),
            "name": name,
            "registration_id": str(uuid4()),
            "scope_id": str(uuid4()),
        }
    else:
        if raw[1] != 0o600:
            raise ValueError("Registration receipt must have mode 0600.")
        receipt = json.loads(raw[0])
        if (
            set(receipt)
            != {
                "schema_version",
                "tenant_id",
                "name",
                "registration_id",
                "scope_id",
            }
            or type(receipt["schema_version"]) is not int
            or receipt["schema_version"] != 1
            or receipt["tenant_id"] != uuid(tenant)
            or receipt["name"] != name
        ):
            raise ValueError("Receipt does not match this tenant and registration name.")
        uuid(receipt["registration_id"])
        uuid(receipt["scope_id"])
    if not name or len(name) > 100 or "'" in name or any(ord(c) < 32 for c in name):
        raise ValueError("Use a unique registration name (1–100 characters, no quotes/controls).")
    cognitive = cognitive_permission(graph)
    apps = {}
    # Resolve both names before any write. A private receipt tag identifies our
    # own partial creation even if POST succeeded but its response was lost.
    for role in ("api", "client"):
        expected = manifest(receipt, role, cognitive)
        app = unique(graph, "applications", f"displayName eq '{expected['displayName']}'")
        if app and expected["tags"] != app.get("tags"):
            raise ValueError("Application name already exists; choose a new unique --name.")
        apps[role] = app
    if apps["client"] and not apps["api"]:
        raise ValueError("API registration is missing; do not repoint an existing public client.")
    resource_id = apps["api"]["appId"] if apps["api"] else None
    for role, app in apps.items():
        if app:
            verify(app, manifest(receipt, role, cognitive, resource_id), resource=role == "api")
            if graph("GET", f"applications/{uuid(app['id'])}/federatedIdentityCredentials")[
                "value"
            ]:
                raise ValueError("Enrollment registrations must not have federated credentials.")
    if not apply:
        return {"mode": "preview", "existing": {role: bool(app) for role, app in apps.items()}}
    # Save the durable operation identity BEFORE creation. Reusing the receipt
    # is mandatory; there is no automatic retry of POST or consent.
    _replace(output, (json.dumps(receipt, indent=2).encode() + b"\n", 0o600))
    for role in ("api", "client"):
        expected = manifest(receipt, role, cognitive, resource_id)
        app = apps[role]
        if not app:
            app = graph("POST", "applications", expected)
            verify(app, expected, resource=role == "api")
            apps[role] = app
        if role == "api":
            resource_id = uuid(app["appId"])
            if not app.get("identifierUris"):
                graph(
                    "PATCH",
                    f"applications/{uuid(app['id'])}",
                    {"identifierUris": ["api://" + resource_id]},
                )
        service = unique(graph, "servicePrincipals", f"appId eq '{uuid(app['appId'])}'")
        if not service:
            graph("POST", "servicePrincipals", {"appId": app["appId"]})
        elif service.get("accountEnabled") is not True:
            raise ValueError(
                "Dedicated enterprise application is disabled; review before continuing."
            )
    if consent:
        az_json("ad", "app", "permission", "admin-consent", "--id", apps["client"]["appId"])
    return {
        "mode": "applied",
        "tenant_id": receipt["tenant_id"],
        "configuration_resource_id": resource_id,
        "public_client_id": uuid(apps["client"]["appId"]),
        "scope": "api://" + resource_id + "/" + SCOPE,
        "admin_consent_requested": consent,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument(
        "--name", required=True, help="Unique environment-specific registration prefix"
    )
    parser.add_argument("--output", type=Path, default=Path(".local/enrollment/entra-receipt.json"))
    parser.add_argument(
        "--apply", action="store_true", help="Create only these dedicated registrations"
    )
    parser.add_argument("--grant-admin-consent", action="store_true")
    args = parser.parse_args()
    result = prepare(
        Graph(uuid(args.tenant), uuid(args.subscription)),
        args.tenant,
        args.name,
        args.output,
        apply=args.apply,
        consent=args.grant_admin_consent,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
