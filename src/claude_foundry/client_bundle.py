"""Build a self-contained, client-only onboarding ZIP from reviewed source files."""

import copy
import io
import json
import zipfile

from .config import validate_client

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


def build_bundle(profile, template):
    config = validate_client(copy.deepcopy(profile))
    if set(config) - CLIENT_FIELDS:
        raise ValueError("Use admin export-client first; the bundle accepts a client profile only")
    codex = config.get("client_type") == "codex"
    if codex and not all(m.get("qualified") is True for m in config["models"]):
        raise ValueError("Install requires a qualified Codex client profile")
    target = "Codex Desktop / CLI" if codex else "Claude Desktop / Claude Code"
    guide = "codex.md" if codex else "macos.md"
    mode = config.get("auth_mode", "azure-cli")
    cli_help = (
        "This profile uses Azure CLI sign-in. If Azure CLI is missing and Homebrew is installed, run "
        "`bash scripts/macos/install.sh --install-azure-cli`. If Homebrew is unavailable, ask your "
        "platform team for an MSAL profile that supports browser sign-in without Azure CLI."
        if mode == "azure-cli"
        else "This profile uses Microsoft browser sign-in (MSAL); Azure CLI is not required."
    )
    readme = f"""# Set up {target} on this Mac

This ZIP includes the installer and your organization's connection settings. No repository clone,
Azure deployment, or manual gateway configuration is needed. Install your organization's approved
{target} app first.

## Connection details

- Application to configure: {target}
- Gateway address: `{config["gateway_url"]}`
- Microsoft tenant: `{config["tenant_id"]}`
- Sign-in method: `{mode}`
- Approved model deployments: {", ".join("`" + m["name"] + "`" for m in config["models"])}

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

After setup, run `~/.local/share/claude-foundry/runtime/bin/claude-foundry verify{" --config ~/.config/claude-foundry/codex/client.json" if codex else ""}` to make
a small billable connection test. The installer prints the restore command when it finishes.
Restore previous settings with:

```bash
~/.local/share/claude-foundry/runtime/bin/claude-foundry restore{" --client codex" if codex else ""}
```

See [the application guide](docs/{guide}) for details. Keep access tokens out of chats and logs.
"""
    instructions = """# Installation assistant instructions

This folder is a complete macOS client installation bundle. Start with README.md and client.json.
When the user requests local client setup, use the bundled installer rather than inventing settings.

- Read gateway_url, tenant_id, auth_mode and models from client.json. Never infer a gateway from
  the portal URL. client_type selects the target app; an absent client_type means Claude, even
  when Codex is the assistant performing setup.
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
    return output.getvalue()
