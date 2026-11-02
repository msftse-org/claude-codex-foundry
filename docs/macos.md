# Mac installation

Use Claude Desktop Chat, Desktop Code, and the Code CLI with the models approved by your organization. Your platform team deploys Azure and supplies the installation bundle; you do not need to clone this repository or administer Azure.

## Before you begin

You need a Mac, your work Microsoft account, your organization’s installation ZIP, and permission to use its models. Install the approved Claude apps first. Keep any current Claude work safe and quit the app before changing its connection settings. Internet access is needed for runtime/dependency downloads and Microsoft sign-in.

Obtain the installation bundle from your organization and unzip it. The bundle must include `client.json`. Use your organization's approved distribution of Claude Desktop and, if needed, the Claude Code CLI. The installer configures those apps; it does not replace or update them.

A combined bundle also contains `codex-client.json`. One run of `Install.command` and one Microsoft sign-in then configure Claude and, when Codex Desktop or the Codex CLI is installed, Codex as well. If you install Codex later, open `Install.command` again.

In the administration portal, open **Developer onboarding** and choose **Download Mac setup ZIP**. Your gateway address is shown on that page and in the ZIP's `README.md`; the installer reads it automatically from `client.json`. To have Codex perform setup, use **Copy setup prompt for Codex** and paste it into Codex on your Mac after downloading. The ZIP includes `AGENTS.md` with setup instructions, so the assistant does not need this repository. A Claude profile configures Claude even when Codex runs the installer.

## Install and sign in

### Apple security approval

The current ZIP contains source/scripts, not an Apple-signed, notarized installer.
If Apple cannot verify `Install.command`, first confirm the ZIP came from your trusted
organization portal and that IT permits installing it. Click **Done**, then use the
portal's **Open macOS Privacy & Security** button. Your browser may ask permission to
open System Settings. If nothing happens, open Apple menu → System Settings →
Privacy & Security manually. Find `Install.command`, choose **Open Anyway**, and confirm.
This creates an exception for that installer; it does not install it or grant model access.

Do not remove quarantine, disable Gatekeeper or bypass managed-device policy. If
**Open Anyway** is unavailable, ask IT for an approved distribution. See
[Apple's guidance](https://support.apple.com/102445).
Only the user can approve an exception: a blocked launcher cannot open settings for
itself, and a website cannot approve it. The portal shortcut is a best-effort OS handoff,
not a notarization substitute. A warning-free publisher-verified download requires
an approved Apple Developer ID signing/notarization release process or managed rollout.

### Run setup

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

Fully quit and reopen Claude Desktop. Its third-party profile enables Chat and Code and lists the organization's published models. The CLI uses the same gateway and credential helper.

## Bring your existing conversations

An import-enabled bundle exposes Claude's native **Settings → Import & export → Import…** workflow. For a locally managed installation, pass `--enable-history-import` to `Install.command` to enable it explicitly. The importer preserves originals; installing alone does not copy history. Follow the [migration guide](conversation-migration.md) for source permissions, local Code sessions, attachment limits, and verification.

## Confirm it works

After restarting Desktop, select the organization profile, choose one of its published models, and send a short message. If you use the Code CLI, open a new terminal and check it uses the same organization connection. The installer ends with an “Installed” message and the restore command. If installation fails, retain the error message and contact your platform team; do not paste access tokens.

The installer configures your local apps. It does not enroll your device in MDM, create a Microsoft account, or grant model permissions.

## Later sign-in and verification

```bash
~/.local/share/claude-foundry/runtime/bin/claude-foundry login
~/.local/share/claude-foundry/runtime/bin/claude-foundry verify
```

Login opens the browser for MSAL, or runs `az login` for Azure CLI mode. For environments that permit device-code sign-in, `login --device-code` is also available. Background credential refresh never opens a browser. Verification makes small billable inference requests.

If your organization manages access through the administration portal, run `verify --governed` instead. A governed gateway rejects an unlisted model with 403 rather than the baseline 400. Your platform team can tell you which mode applies.

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

## Get help

Ask your platform team for a new bundle when the organization changes its model catalog. For expired sign-in, run `login` above; for an incorrect endpoint or MDM conflict, use the [online troubleshooting guide](https://github.com/msftse-org/claude-codex-foundry/blob/main/docs/troubleshooting.md). Share the error code, time, app version, and whether the CLI or Desktop is affected. Remove tokens, tenant identifiers, prompts, and customer content from screenshots and logs.
