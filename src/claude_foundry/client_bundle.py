"""Build a self-contained, client-only onboarding ZIP from reviewed source files."""

import copy
import io
import json
import zipfile

from .config import validate_client
from .configuration_client import EnrollmentDescriptor

CLIENT_FIELDS = {
    "schema_version",
    "profile_name",
    "tenant_id",
    "gateway_url",
    "token_resource",
    "auth_mode",
    "client_id",
    "models",
    "conversation_import",
    "client_type",
    "default_model",
}


def build_template(root):
    """Package only public installer files; this template contains no profile."""
    files = [
        root / path
        for path in (
            "pyproject.toml",
            "requirements-auth.lock",
            "Install.command",
            "scripts/macos/install.sh",
            "docs/macos.md",
            "docs/conversation-migration.md",
            "docs/codex.md",
            "docs/codex-validation.md",
            "docs/client-configuration-refresh.md",
        )
    ]
    files += sorted((root / "src/claude_foundry").glob("*.py"))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            if path.is_symlink():
                raise ValueError(f"Symlinks are not accepted: {path}")
            bundle.write(path, "claude-foundry/" + str(path.relative_to(root)))
    return output.getvalue()


def build_enrollment_bundle(descriptor, template, *, clients):
    """Initial, trusted bootstrap only; published grants are fetched after sign-in."""
    parsed = EnrollmentDescriptor.parse(descriptor)
    if (
        not isinstance(clients, list)
        or not clients
        or any(not isinstance(name, str) for name in clients)
        or len(set(clients)) != len(clients)
        or set(clients) - {route.client for route in parsed.routes}
    ):
        raise ValueError("Choose clients included in the enrollment descriptor")
    target = (
        "Claude and Codex Desktop / CLI"
        if len(clients) == 2
        else "Codex Desktop / CLI"
        if clients == ["codex"]
        else "Claude Desktop / Claude Code"
    )
    gateways = "\n".join(
        f"- {route.client.title()} gateway: `{route.gateway_url}`"
        for route in parsed.routes
        if route.client in clients
    )
    readme = f"""# Enroll {target}

This experimental Mac setup connects to your organization once, then refreshes your published
model access without another JSON download. It contains no model grants, user identity or tokens.
Download this installer only from your trusted organization portal.

## Before installing

Review these trusted connection details with your platform team:

- Configuration service: `{parsed.configuration_url}`
- Microsoft tenant: `{parsed.tenant_id}`
- Application: {target}
{gateways}

`enrollment.json` pins the organization, tenant, configuration resource and gateway routes.
`installation.json` selects the applications only. Neither file grants access. The signed-in
user's current published policy determines the available models and server-side budget.

## Install

1. Install your organization's approved {target}. Azure CLI is not required.
2. Save active work, extract the ZIP, open the `claude-foundry` folder, then run
   `bash Install.command` (or double-click it).
3. Review the connection details and complete Microsoft work-account sign-in and MFA.
4. Follow the installer's result; reopen the app only after `SETUP COMPLETE`.
5. To receive future published model changes, explicitly enable background refresh:
   `~/.local/share/claude-foundry/runtime/bin/claude-foundry enable-refresh`.

Background refresh runs about every 15 minutes while you are logged into this Mac. Reopen an
already-running client to reload its model picker. Refresh cannot grant access or bypass the
gateway's current policy; failed/offline refresh keeps the last good local configuration.

Existing JSON installations require an explicit migration:
`bash Install.command --migrate-existing`. Unrelated settings and conversations are preserved.
Unexpected routing changes or managed preferences stop setup for review.

## Status and recovery

Use `~/.local/share/claude-foundry/runtime/bin/claude-foundry configuration-status` for status,
`refresh` for a noninteractive update, or `refresh --login` when explicit sign-in is needed.
Use `disable-refresh` to stop the background job. Do not use the legacy `restore` command on an
enrolled installation; see [the pilot guide](docs/client-configuration-refresh.md) for limitations.

MSAL stores credentials in macOS Keychain, with no plaintext fallback. A locked or unavailable
key store stops sign-in/refresh. Background refresh never opens a login window. Conditional
Access may occasionally require foreground sign-in again, and enrollment may need separate
configuration and inference consent prompts. This installer does not change Azure resources.
Never paste tokens into a chat or send them to your administrator.
"""
    instructions = """# Installation assistant instructions

Read README.md, enrollment.json and installation.json first. Use bash Install.command from this
folder for the user's requested local setup. Do not invent a gateway, change the enrollment
descriptor, or substitute a legacy client profile. Encrypted credential storage is required.

Let the user complete explicit Microsoft sign-in. Never print, request or copy access tokens.
Existing JSON migration and enabling background refresh each require the user's explicit choice.
Do not work around managed settings or routing-conflict errors. Report a setup failure honestly;
only report success after SETUP COMPLETE. Preserve unrelated settings and conversations.
"""
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(template)) as source,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle,
    ):
        for entry in source.infolist():
            if entry.filename in {
                "claude-foundry/client.json",
                "claude-foundry/codex-client.json",
                "claude-foundry/enrollment.json",
                "claude-foundry/installation.json",
                "claude-foundry/README.md",
                "claude-foundry/AGENTS.md",
            }:
                raise ValueError("Enrollment requires an unconfigured installer template")
            bundle.writestr(entry, source.read(entry))
        bundle.writestr("claude-foundry/README.md", readme)
        bundle.writestr("claude-foundry/AGENTS.md", instructions)
        bundle.writestr("claude-foundry/enrollment.json", json.dumps(descriptor, indent=2) + "\n")
        bundle.writestr(
            "claude-foundry/installation.json",
            json.dumps({"schema_version": 1, "clients": sorted(clients)}, indent=2) + "\n",
        )
    return output.getvalue()


def build_bundle(profiles, template, *, governed=False, budgeted=False):
    if isinstance(profiles, dict):
        profiles = [profiles]
    configs = [validate_client(copy.deepcopy(profile)) for profile in profiles]
    if any(set(config) - CLIENT_FIELDS for config in configs):
        raise ValueError("Use admin export-client first; the bundle accepts a client profile only")
    kinds = [config.get("client_type", "claude") for config in configs]
    if not configs or len(configs) > 2 or len(set(kinds)) != len(kinds):
        raise ValueError("Supply one Claude profile, one Codex profile, or one of each")
    if len({config["tenant_id"] for config in configs}) != 1:
        raise ValueError("Combined profiles must use the same Microsoft Entra tenant")
    if any(
        config.get("client_type") == "codex"
        and not all(model.get("qualified") is True for model in config["models"])
        for config in configs
    ):
        raise ValueError("Install requires a qualified Codex client profile")
    by_kind = dict(zip(kinds, configs, strict=True))
    config = by_kind.get("claude") or by_kind["codex"]
    combined = len(configs) == 2
    codex = config.get("client_type") == "codex"
    target = (
        "Claude Desktop / Claude Code and Codex Desktop / CLI"
        if combined
        else "Codex Desktop / CLI"
        if codex
        else "Claude Desktop / Claude Code"
    )
    heading = "Claude & Codex on Azure" if combined else f"Set up {target} on this Mac"
    guide = "codex.md" if codex else "macos.md"
    mode = config.get("auth_mode", "azure-cli")
    cli_help = (
        "This profile uses Azure CLI sign-in. If Azure CLI is missing and Homebrew is installed, run "
        "`bash scripts/macos/install.sh --install-azure-cli`. If Homebrew is unavailable, ask your "
        "platform team for an MSAL profile that supports browser sign-in without Azure CLI."
        if mode == "azure-cli"
        else "This profile uses Microsoft browser sign-in (MSAL); Azure CLI is not required."
    )
    extra_connection = (
        f"\n- Codex gateway address: `{by_kind['codex']['gateway_url']}`\n"
        "\nThis combined bundle also includes `codex-client.json`. The installer configures Codex\n"
        "when it is installed; run `Install.command` again if you install Codex later. Both profiles\n"
        "use the same Microsoft tenant; each distinct sign-in configuration is authenticated once.\n"
        if combined
        else ""
    )
    verification = "\n".join(
        "~/.local/share/claude-foundry/runtime/bin/claude-foundry verify --config "
        f"~/.config/claude-foundry/{'codex/' if kind == 'codex' else ''}client.json"
        + (
            " --budgeted"
            if budgeted and kind == "claude"
            else " --governed"
            if governed and kind == "claude"
            else ""
        )
        for kind in by_kind
    )
    restore = "\n".join(
        "~/.local/share/claude-foundry/runtime/bin/claude-foundry restore"
        + (" --client codex" if kind == "codex" else "")
        for kind in by_kind
    )
    readme = f"""# {heading}

This ZIP includes the installer and your organization's connection settings. No repository clone,
Azure deployment, or manual gateway configuration is needed. Install your organization's approved
{target} app first.

## Connection details

- Application to configure: {target}
- Gateway address: `{config["gateway_url"]}`
- Microsoft tenant: `{config["tenant_id"]}`
- Sign-in method: `{mode}`
- Approved model deployments: {", ".join("`" + m["name"] + "`" for m in config["models"])}
{extra_connection}

`client.json` beside this file is the source of truth. The installer reads its gateway address,
tenant, and model settings automatically. Each user signs in with their own Microsoft work account.

## Install

1. Extract the ZIP and open the `claude-foundry` folder.
2. Save active work in {target}, then double-click `Install.command`.
   From a terminal or coding assistant in this folder, run `bash Install.command`.
3. Complete Microsoft sign-in and your organization's MFA in the browser.
4. Fully quit and reopen the app, select an approved model, and send a short test message.

The installer supports macOS and downloads its isolated runtime and pinned dependencies.
{cli_help}

## Ask Codex to set this up

Open this extracted folder in Codex and paste:

> Set up {target} locally using this installation bundle. Read README.md, AGENTS.md,
> and client.json, then run bash Install.command from this folder. Use the gateway_url
> from client.json exactly. Help me complete Microsoft sign-in, preserve existing settings
> and conversations, and explain how to reopen the app and check the connection.

The application to configure is determined by `client_type` in `client.json` (omitted means Claude).
Using Codex to run this setup does not change which application the profile configures.

## Verify or recover

After setup, these commands make small billable connection tests:

```bash
{verification}
```

The installer prints the restore commands when it finishes. Restore previous settings with:

```bash
{restore}
```

See [the application guide](docs/{guide}) for details. Keep access tokens out of chats and logs.
"""
    instructions = """# Installation assistant instructions

This folder is a complete macOS client installation bundle. Start with README.md and client.json.
When the user requests local client setup, use the bundled installer rather than inventing settings.

- Read gateway_url, tenant_id, auth_mode and models from client.json. Never infer a gateway from
  the portal URL. client_type selects the target app; an absent client_type means Claude, even
  when Codex is the assistant performing setup. If codex-client.json is also present, read its
  separate gateway and model settings too; Install.command handles both profiles.
- Confirm macOS and that the target app is installed. From this folder run bash Install.command.
- For an Azure CLI profile, check for az first. If absent and Homebrew is available, use
  bash scripts/macos/install.sh --install-azure-cli. If neither is available, explain the missing
  prerequisite or request an MSAL profile from the platform team. Do not silently change auth_mode.
- Let the user complete interactive Microsoft sign-in. Never print, request or copy access tokens.
- The installer preserves existing settings and provides recovery. Keep conversations intact.
  Respect managed settings conflicts reported by the installer.
- After success, report the configured target app and gateway, ask the user to save active work
  and fully reopen the app, and explain how to send a short test message. Report failures clearly.
"""
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(template)) as source,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle,
    ):
        for entry in source.infolist():
            bundle.writestr(entry, source.read(entry))
        bundle.writestr("claude-foundry/README.md", readme)
        bundle.writestr("claude-foundry/AGENTS.md", instructions)
        bundle.writestr("claude-foundry/client.json", json.dumps(config, indent=2) + "\n")
        if combined:
            bundle.writestr(
                "claude-foundry/codex-client.json", json.dumps(by_kind["codex"], indent=2) + "\n"
            )
    return output.getvalue()
