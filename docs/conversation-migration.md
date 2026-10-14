# Bring existing conversations to Azure

[← Mac setup](macos.md) · [Administrator setup](administrator.md) · [Security](security.md)

Claude Desktop has a native importer for Claude.ai chats/projects and eligible local Desktop sessions. Claude Foundry can enable it during onboarding. Import creates local copies; originals stay available. Continuing imported work uses the configured Azure gateway. See [Anthropic's import guide](https://claude.com/docs/third-party/claude-desktop/import) for the complete workflow and current compatibility.

## For users

Use Claude Desktop **2.26454.0 or later**, approved by your organization. The installer checks this minimum so source-organization restrictions cannot silently be ignored by an older build. Ask your platform team for an import-enabled installation bundle, then run `Install.command` and restart Desktop. The installer prints the next step when import is enabled.

For a locally managed installation, you can explicitly enable the wizard when running your bundle:

```bash
./Install.command --enable-history-import
```

If the current runtime already includes this feature, reapply your installed profile without downloading dependencies or starting a new interactive sign-in:

```bash
~/.local/share/claude-foundry/runtime/bin/claude-foundry install \
  --config ~/.config/claude-foundry/client.json --enable-history-import
```

This requires a working inference credential. If it has expired, run the documented [login command](macos.md#later-sign-in-and-verification) first. The installation takes a private settings snapshot and retains the existing restore mechanism. It does not read, upload, rewrite, or delete conversation files.

In Desktop, open **Settings → Import & export → Import…**. Choose your Claude.ai account or an export file, select eligible local sessions, review the summary, and import. Verify a familiar conversation and project. Review the trust prompt before continuing imported work.

The wizard can require a separate Claude.ai sign-in for the source; Microsoft sign-in still controls Azure inference. A Team/Enterprise workspace owner must enable member exports before its users can download their histories. Imports do not continuously synchronize. Member exports omit attachment and project-knowledge file contents; retain those separately through an approved process.

## For platform administrators

Import is opt-in. Add this property to your private organization JSON **before** running `admin export-client` and building the Mac ZIP:

```json
"conversation_import": {
  "enabled": true,
  "bannerBehavior": "detect",
  "allowedOrganizationUuids": ["00000000-0000-0000-0000-000000000003"]
}
```

Replace the placeholder with the **source Claude.ai organization UUID**, not the Microsoft Entra tenant or group ID. Restricting sources disables file/local import in Claude's wizard; omitting the list permits those paths, while an empty list permits no source organizations. Choose the policy appropriate to your migration. [Vendor policy reference](https://claude.com/docs/third-party/claude-desktop/configuration#claudeaiimport).

The field is validated and carried through CLI and portal client-profile exports and the bundle allowlist. Portal profile export uses the organization's deployed configuration; this change does not add a portal policy editor or update existing hosted configuration. Re-export profiles and redistribute bundles after changing policy.

The installer translates this object into Claude's `claudeAiImport` setting. Supported fields are `enabled`, `bannerBehavior` (`off`, `detect`, `show`), and `allowedOrganizationUuids` (at most 100 unique UUIDs). Unknown fields fail validation. An existing local source restriction is retained when the new profile omits a list. An explicitly supplied list replaces it. A custom import endpoint requires manual administration; the installer refuses to redirect it.

Automatic background import and session export are disabled when this policy is applied. Automatic import can also bring scheduled tasks, plugins, and memory into use; it is intentionally outside this history-only onboarding option. No conversation data is added to installation bundles or sent to the management portal.

An MDM import policy blocks local installation. For managed fleets, use Claude's configuration editor and your organization's MDM process to deploy the reviewed `claudeAiImport` value. The local installer cannot override it. The source-organization list requires Desktop 2.26454.0 or later; pilot with the same approved Desktop build that customers will receive.

## Verify and recover

Record the source account/workspace, Desktop version, imported counts, skipped/failed counts, and destination profile in private rollout notes. Check one chat and one local Code session where available; verify Azure routing before resuming them. Do not put conversation titles, transcripts, exports, or screenshots with customer data in Git.

`claude-foundry restore` rolls back managed settings, including this import setting when it has not been changed since installation. It does **not** undo imports or remove original/copied conversations. Use Claude's import history UI to review a batch; removing imported data is a separate user decision. Keep originals until your migration has been accepted.

Terminal Claude Code sessions stay in its existing local store because this installer does not change `CLAUDE_CONFIG_DIR`. Resume a session from its original working directory with `claude --resume`; check the selected Azure model and connection before continuing. Desktop import and terminal session resumption are separate workflows. See [Claude Code sessions](https://code.claude.com/docs/en/sessions).

## Validation

Automated coverage checks policy validation/export/packaging, preservation of source restrictions, MDM refusal, settings recovery, and unchanged conversation files. Native import is performed by Claude Desktop, not by these tests. Live workstation results are recorded separately; cloud imports require the source account's access and any required organization export policy.
