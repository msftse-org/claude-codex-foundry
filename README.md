# Claude & Codex on Azure

Run Claude Desktop, Claude Code and Codex Desktop/CLI through **Azure Foundry and standard Azure API Management**, with Microsoft Entra sign-in. Deploy in the customer's subscription, give developers a Mac installer, and manage Claude and Codex access and usage in one customer-owned portal.

## Start here

| I want to… | Open this |
| --- | --- |
| **Deploy with a customer's LLM** | **[LLM deployment guide →](docs/llm-onboarding.md)** — copyable prompt, customer inputs, deployment, user import and verification |
| Deploy it myself | [Quick start →](docs/quickstart.md) — one settings file and one deployment command |
| Install on a developer's Mac | [Mac guide →](docs/macos.md) — installer, sign-in, verification and restore |

## Deploy a fresh environment

**Have ready:** repository access, tenant/subscription IDs, approved region and models, publisher contact, and Claude Marketplace details. You need Azure deployment/RBAC permissions and Entra group/app-registration permissions; consent may require a separate administrator. [Full input checklist](docs/llm-onboarding.md#collect-the-customer-inputs).

**Install:** Git, Azure CLI with Bicep, uv/Python 3.11+, and Node.js 24/npm. Use a separate checkout for each customer.

### 1. Prepare the settings

```bash
git clone --branch main https://github.com/msftse-org/claude-codex-foundry.git
cd claude-codex-foundry
uv sync --frozen --extra auth --extra portal
mkdir -p .local
cp examples/deploy.json .local/deploy.json
```

**Edit `.local/deploy.json` before continuing:** replace the tenant/subscription IDs, unique resource suffix, region, publisher and Marketplace details. Select the approved models; an empty list attempts every eligible model with quota. Set `accept_marketplace_terms` to `true` only after the customer authorizes the paid terms. [Settings reference](docs/quickstart.md#1-describe-the-deployment).

### 2. Deploy

```bash
az login --tenant YOUR_TENANT_ID
az account set --subscription YOUR_SUBSCRIPTION_ID
uv run --frozen python scripts/deploy.py --settings .local/deploy.json
```

This creates Foundry, APIM, monitoring, the enabled Claude/Codex model routes, sign-in applications, an installer ZIP and unified administration with the shared runtime and private ledger storage. **Azure charges apply, including verification requests. APIM can take 30+ minutes.** Review the final verification results and any consent actions. If a step fails, fix the cause and rerun with the same settings and state.

### 3. Add developers and distribute the installer

```bash
uv run --frozen python scripts/onboard_users.py alice@contoso.com bob@contoso.com
# Or import a CSV with email, UPN or object ID in its first column:
uv run --frozen python scripts/onboard_users.py --csv .local/developers.csv
```

These commands grant group access to **existing users in the customer's tenant** and generate `.local/portal-members.csv`. Resolve any reported failures before rollout.

Send `dist/claude-codex-SUFFIX-macos.zip` through the customer's software channel. Developers install the approved apps, extract the ZIP, open `Install.command`, sign in with Microsoft and reopen their apps. Test on a pilot Mac before wider distribution. [Installation and recovery](docs/macos.md).

**To activate shared governance:** sign in as the owner, review both catalogs, import intended members, verify model prices and pinned deployment bounds, then publish access and spending policy. Governed inference stays closed until these checks pass. [Unified setup and existing-customer upgrade](docs/unified-administration.md). Portal groups and Entra groups are separate.

## Find the right guide

| Topic | Guide |
| --- | --- |
| Deployment settings, prerequisites and reruns | [Quick start](docs/quickstart.md) |
| Existing infrastructure or individual deployment steps | [Administrator guide](docs/administrator.md) |
| Customer verification and handover | [LLM runbook](docs/llm-onboarding.md#verify-and-hand-over) |
| Errors and sign-in problems | [Troubleshooting](docs/troubleshooting.md) · [FAQ](docs/faq.md) |
| Claude/Codex installation and existing conversations | [Mac setup](docs/macos.md) · [Codex setup](docs/codex.md) · [Conversation migration](docs/conversation-migration.md) |
| Members, groups, model access and cost reporting | [Portal walkthrough and screenshots](docs/portal-guide.md) |
| Portal deployment and development | [Portal engineering guide](portal/README.md) |
| Spending limits and delegated administration | [Budget architecture](docs/budget-architecture.md) · [Budget setup](budget/README.md) · [Enforcement rules](docs/budget-enforcement.md) |
| Monitoring, upgrades, offboarding and recovery | [Operations](docs/operations.md) · [Observability](docs/observability.md) |
| Identity, permissions and captured content | [Security](docs/security.md) · [Permissions](docs/identity-and-permissions.md) · [Opt-in content logs](docs/content-logs.md) |
| Source changes and security reports | [Contributing](CONTRIBUTING.md) · [Security reporting](SECURITY.md) |

## Before a customer rollout

- **Preview features:** Codex and the portal have documented acceptance gaps. Review [Codex validation](docs/codex-validation.md) and [portal validation](portal/VALIDATION.md) for the intended rollout.
- **Spending controls:** the default unified installation includes the shared runtime for Claude/Codex organization/group/user limits, activated by reviewed owner publication. Its [release checks](docs/budget-enforcement.md#release-acceptance) remain required. Explicit `portal.mode=baseline` and portal-free deployments do not enforce shared USD limits. Portal cost estimates are not Azure invoices.
- **Supported setup:** onboarding targets macOS and Azure public cloud. Baseline APIM/Foundry endpoints use public HTTPS. Keep customer configuration and identity lists in ignored `.local/`; distribute only the installer ZIP.

Independently maintained; no Microsoft or Anthropic support or certification. No repository license has been selected; confirm redistribution terms before sharing source with customers. Azure and model-provider terms apply separately.
