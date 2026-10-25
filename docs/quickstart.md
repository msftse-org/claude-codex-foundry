# Quick start: deploy and onboard with one command

[← README](../README.md) · [Detailed administrator guide](administrator.md) · [Mac installation](macos.md)

This path deploys a complete, standard installation from one settings file, then grants developers access by email. It runs the same documented commands as the [administrator guide](administrator.md) in order and prints each one. Use the detailed guides when you need to change a single step.

## What you need

| Who | Permission |
| --- | --- |
| Azure administrator | Owner, or Contributor plus Role Based Access Control Administrator, on the subscription. The subscription must be allowed to purchase the selected Anthropic Marketplace offers. |
| Entra administrator (can be the same person) | Create app registrations and grant admin consent (for example Application Administrator), and create a security group, or provide an existing group's object ID. |

Install Git, [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) with Bicep, [uv](https://docs.astral.sh/uv/), and Node.js 24 with npm (portal build). Before you start, check the subscription's Azure Policy assignments for deny rules on locations, tags, public storage access or subnet network security groups (the portal network does not create NSGs).

## 1. Describe the deployment

```bash
git clone https://github.com/msftse-org/claude-codex-foundry.git
cd claude-codex-foundry
uv sync --frozen --extra auth --extra portal
mkdir -p .local && cp examples/deploy.json .local/deploy.json
az login --tenant YOUR_TENANT_ID
```

Edit `.local/deploy.json`:

| Field | Meaning |
| --- | --- |
| `tenant_id`, `subscription_id` | Target tenant and subscription. The script selects this subscription in Azure CLI. |
| `suffix` | 3–12 lowercase letters or digits. Names become `rg-ccf-SUFFIX`, `ccf-foundry-SUFFIX`, `ccf-apim-SUFFIX`, `rg-ccf-SUFFIX-admin` and `ccf-admin-SUFFIX`; Foundry, APIM and portal names must be globally unique. |
| `location` | Region for Foundry, monitoring and the portal. Optional `apim_location` defaults to it. |
| `publisher_name`, `publisher_email` | API Management publisher contact. |
| `accept_marketplace_terms` | Set to `true` after reviewing the paid Anthropic Marketplace terms. Claude models are not deployed until then. |
| `marketplace` | Legal organization name, two-letter country code and industry for Anthropic attestation. |
| `developer_group_id` | Existing Entra security group for developers. Leave empty to create or reuse **Claude & Codex developers**. |
| `claude`, `codex` | `enabled`, optional `models` list (empty deploys every eligible model with quota), `capacity` per deployment, and `sku`. |
| `portal.enabled` | Deploys the administration portal on the same Claude route. Governance is not published automatically. |

Optional fields: `apim_sku` (`BasicV2` or `StandardV2`), `auth_mode` (`msal`, the default, needs no Azure CLI on developer Macs; `azure-cli` needs no app registration or consent), `allowed_object_ids`, `profile_name`, `grant_admin_consent`, `add_deployer_to_developers` and `names` overrides. Keep this file and everything the script writes in `.local/`, outside Git.

## 2. Deploy

```bash
uv run python scripts/deploy.py --settings .local/deploy.json
```

The script creates billable Azure resources and makes small billable verification requests. API Management provisioning can take 30 minutes or more. It:

1. Selects the subscription and registers the required resource providers.
2. Creates or reuses the developer group and adds you to it so you can verify access.
3. Deploys Foundry, API Management, Application Insights and Log Analytics, granting the group inference access.
4. Deploys Claude models, publishes **Claude via Azure Foundry** and connects observability.
5. Creates **Codex via Azure Foundry**, deploys and qualifies OpenAI models, and publishes only those that pass.
6. Registers the developer sign-in application (public client, no secret), requests tenant-wide admin consent and exports client profiles. If you cannot grant consent, the script finishes and prints the command for an Entra administrator; developers can sign in once consent is granted.
7. Builds one installer ZIP for Claude and Codex in `dist/`.
8. Deploys the administration portal and its application package, then waits for it to report healthy.
9. Verifies the gateways with your Azure CLI sign-in, detecting published Claude governance on reruns. Use `--skip-verify` to skip these final checks; Codex qualification still makes billable requests.

If a step fails, fix the reported problem and run the same command again. Existing resources, deployments, the group and the app registrations are reused; IDs and the deployment target are kept in `.local/deploy-state.json`. Use a separate checkout for each customer: changing the tenant, subscription or resource names while retaining this state is rejected before Azure changes. To change models later, edit `.local/deploy.json`, rerun the command and redistribute the new ZIP.

## 3. Onboard developers

Grant access by work email, user principal name or object ID, or from a CSV whose first column lists people:

```bash
uv run python scripts/onboard_users.py alice@contoso.com bob@contoso.com
uv run python scripts/onboard_users.py --csv developers.csv
```

Access reaches Foundry within a few minutes. Send developers the installer ZIP from `dist/` through your normal software channel. Each developer:

1. Installs your approved Claude Desktop, Claude Code and/or Codex.
2. Extracts the ZIP, opens `Install.command` and signs in with Microsoft once.
3. Reopens the apps and selects an organization model.

Claude is configured on every Mac. Codex is configured when it is installed; developers who install Codex later open `Install.command` again. To remove baseline group access, run `onboard_users.py --remove` with the same people. On portal-governed routes, also suspend the member and publish; removing Entra group membership alone does not revoke the portal's published access. See the [Mac guide](macos.md) for verification and restore.

Onboarding checks the signed-in tenant against deployment state before resolving users or changing membership. For an explicit group or older state, supply `--tenant YOUR_TENANT_ID` and, when needed, `--group YOUR_GROUP_OBJECT_ID`. Users must already exist in that tenant; this script does not create directory accounts or invite guests.

The script never publishes portal governance. Until you do, access is controlled by the developer group. To manage people in the portal instead, sign in as the owner, import `.local/portal-members.csv` (written by `onboard_users.py`) under **Members**, review and publish, then run `claude-foundry verify --governed`. Register everyone who should keep access first: publication denies anyone who is not an active member.

## Remove the deployment

Delete `rg-ccf-SUFFIX-admin`, then `rg-ccf-SUFFIX`. Deleted Foundry accounts and API Management services are retained in a soft-deleted state; purge them to release quota and names. Remove the two app registrations and, if the script created it, the developer group. Restore developer Macs first with `claude-foundry restore`.
