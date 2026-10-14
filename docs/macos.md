# Mac installation

Obtain the installation bundle from your organization and unzip it. The bundle must include `client.json`. Use your organization's approved distribution of Claude Desktop and, if needed, the Claude Code CLI. The installer configures those apps; it does not replace or update them.

## Install and sign in

Double-click `Install.command`, or run this from the extracted folder:

```bash
./scripts/macos/install.sh
```

The command opens Terminal and then Microsoft sign-in in your browser when interaction is needed. Complete your organization's MFA. It downloads a pinned uv bootstrap if needed, creates an isolated Python runtime under `~/.local/share/claude-foundry/runtime`, installs pinned authentication dependencies, and configures Claude after credentials work. It needs no Python installation, shell startup changes or administrator password.

If the profile uses Azure CLI and the CLI is missing, use:

```bash
./scripts/macos/install.sh --install-azure-cli
```

This installs the official [Azure CLI Homebrew cask](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-macos) without adding extensions. Homebrew must already be installed using your organization's approved method. Azure CLI does not offer an officially supported authentication-only distribution. An **MSAL profile** avoids installing Azure CLI entirely; request one from your administrator if that is preferred.

For an existing checkout, supply the profile explicitly:

```bash
./scripts/macos/install.sh --config /path/to/client.json
```

Use `--no-login` only if the selected authentication method already has a working session. Credential validation still runs before changing settings. The installer stops when it detects managed inference settings that local settings cannot override; your MDM administrator must deploy that configuration.

Fully quit and reopen Claude Desktop. Its third-party profile enables Chat and Code and lists the organization's published models. The CLI uses the same gateway and credential helper. Existing consumer Claude conversations remain in their previous profile; they do not automatically move into the third-party profile.

## Later sign-in and verification

```bash
~/.local/share/claude-foundry/runtime/bin/claude-foundry login
~/.local/share/claude-foundry/runtime/bin/claude-foundry verify
```

Login opens the browser for MSAL, or runs `az login` for Azure CLI mode. For environments that permit device-code sign-in, `login --device-code` is also available. Background credential refresh never opens a browser. Verification makes small billable inference requests.

The helper writes only a token to standard output. Do not run the `token` command in a recorded terminal or paste its output into support tickets.

## Settings and recovery

Managed files are:

- `~/.claude/settings.json` for Claude Code.
- `~/Library/Application Support/Claude-3p/configLibrary/` and `claude_desktop_config.json` for Desktop.
- `~/.config/claude-foundry/` for the organization profile, helper, installation manifest and private settings snapshots.

The installer preserves unrelated settings. Existing credentials found in settings may be retained in private recovery snapshots, so treat the backup directory as sensitive. Do not distribute it.

To restore settings:

```bash
~/.local/share/claude-foundry/runtime/bin/claude-foundry restore
```

Restore reverts managed values that have not subsequently been changed. It preserves later user changes, unrelated preferences and conversations. Restart Desktop afterward. Backups and sign-in caches remain available; this is a settings rollback, not a credential wipe. An administrator can remove the isolated runtime after rollback if it is no longer needed.

To update the installation, run the new organization's bundle. The helper uses the durable runtime path, so the downloaded bundle can be removed after successful installation.
