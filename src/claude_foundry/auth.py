"""User sign-in. Helpers never prompt or print anything except a token."""

import os
import subprocess
import sys
from pathlib import Path

from .azure import az_json, find_az
from .config import TOKEN_RESOURCE, load_client, read_json, write_json


def state_dir():
    return Path(os.environ.get("CLAUDE_FOUNDRY_HOME", str(Path.home() / ".config/claude-foundry")))


def msal_app(c):
    try:
        import msal
        from msal_extensions import PersistedTokenCache, build_encrypted_persistence
    except ImportError:
        raise RuntimeError(
            "Install the lightweight authentication dependencies: pip install 'claude-foundry[auth]' from this repository"
        ) from None
    directory = state_dir() / "auth" / c["tenant_id"] / c["client_id"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    persistence = build_encrypted_persistence(str(directory / "token-cache"))
    if not persistence.is_encrypted:
        raise RuntimeError(
            "Encrypted credential storage is required; no plaintext fallback is allowed"
        )
    app = msal.PublicClientApplication(
        c["client_id"],
        authority=f"https://login.microsoftonline.com/{c['tenant_id']}",
        token_cache=PersistedTokenCache(persistence),
    )
    return app, directory


def credential(c, interactive=False, device_code=False):
    if c.get("auth_mode", "azure-cli") == "azure-cli":
        if interactive:
            env = dict(os.environ, AZURE_CORE_LOGIN_EXPERIENCE_V2="off")
            command = [
                find_az(),
                "login",
                "--tenant",
                c["tenant_id"],
                "--allow-no-subscriptions",
                "--output",
                "none",
            ]
            if device_code:
                command.append("--use-device-code")
            subprocess.run(command, env=env, check=True)
        # Tenant targeting allows a user with only resource/group RBAC to sign in;
        # no default subscription is changed by the helper.
        result = az_json(
            "account", "get-access-token", "--tenant", c["tenant_id"], "--resource", TOKEN_RESOURCE
        )
        return result["accessToken"]
    app, directory = msal_app(c)
    selected = read_json(directory / "account.json", default={}).get("home_account_id")
    accounts = app.get_accounts()
    account = next((a for a in accounts if a["home_account_id"] == selected), None)
    scopes = [TOKEN_RESOURCE + "/.default"]
    result = app.acquire_token_silent_with_error(scopes, account=account) if account else None
    if interactive and (not result or "access_token" not in result):
        if device_code:
            flow = app.initiate_device_flow(scopes=scopes)
            if "user_code" not in flow:
                raise RuntimeError(
                    flow.get("error_description", "Unable to initiate device sign-in")
                )
            print(flow["message"], file=sys.stderr)
            result = app.acquire_token_by_device_flow(flow)
        else:
            result = app.acquire_token_interactive(scopes=scopes, timeout=300)
        if result and "access_token" in result:
            # Match the signed-in account using immutable token claims, not list ordering.
            claims = result.get("id_token_claims", {})
            matches = [
                a
                for a in app.get_accounts()
                if a.get("local_account_id") == claims.get("oid")
                and a.get("realm") == c["tenant_id"]
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "Could not identify the signed-in account uniquely; retry sign-in"
                )
            write_json(
                directory / "account.json", {"home_account_id": matches[0]["home_account_id"]}
            )
    if not result or "access_token" not in result:
        code = result.get("error", "interaction_required") if result else "interaction_required"
        raise RuntimeError(
            f"Sign-in required ({code}). Run claude-foundry login; background helpers do not open a browser."
        )
    return result["access_token"]


def installed_config():
    return load_client(state_dir() / "client.json")


def helper(config_path=None):
    print(credential(load_client(config_path) if config_path else installed_config()))


def login(config_path=None, device_code=False):
    c = load_client(config_path) if config_path else installed_config()
    credential(c, interactive=True, device_code=device_code)
    print("Signed in. Claude Desktop and Code can now refresh credentials silently.")
