# Deploy a fresh customer environment with an LLM

[← README](../README.md) · [One-command quick start](quickstart.md) · [Administrator reference](administrator.md)

Give this guide and repository access to the customer's coding assistant. The assistant can prepare the configuration, deploy infrastructure, resolve existing directory users, build the installer and verify the result. A customer administrator supplies the target environment and authorizes the Azure changes and paid services. Interactive Microsoft sign-in and any required administrator consent still happen in the customer's tenant.

This runbook targets a fresh Azure public-cloud installation with macOS clients. For an existing standard installation, use the [portal-only upgrade](unified-administration.md#upgrade-an-existing-standard-installation). For other existing infrastructure, use the [administrator guide](administrator.md); do not point the fresh-deployment script at shared resources without reviewing ownership and its templates.

## Prompt to give the customer's assistant

Copy this prompt into an assistant with a terminal and access to this repository. Supply the customer inputs in the next section through an approved private channel.

```text
Deploy Claude & Codex on Azure for this customer using this repository's main branch.
Read AGENTS.md and docs/llm-onboarding.md, then inspect the current deployment scripts,
examples/deploy.json and the linked guides before running commands.

Use a separate checkout for this customer. Collect the missing required inputs in one
request; never infer a tenant, subscription, legal attestation, model selection or user
list from another environment. Prepare .local/deploy.json, a deployment plan and the
initial user CSV. Explain the target resources, model/SKU/geography choices, access
mode, charges and outstanding customer decisions before applying cloud changes.
Use authorization already provided; ask only for missing decisions or authorization.

Once the plan is authorized, run the repository's deployment and onboarding scripts,
verify the actual result, and resolve failures within that scope. Use existing state
to resume the same deployment. Keep credentials, identity data and raw reports out of
Git and chat. Never change tenant policies or weaken networking to bypass a failure.

Treat Entra group membership, portal membership and policy publication as distinct
steps. Use unified portal administration by default for both enabled providers. Follow
docs/unified-administration.md: review model prices/capacities, verify and pin actual
versions, configure portal.model_policy_file, then obtain owner publication. Complete
the budget acceptance checks before claiming access and spending controls are active.
Use portal.mode=baseline only if the customer explicitly chooses the legacy setup.

Finish with the source commit, deployed resources and URLs, actual available models,
verification evidence, import counts and unresolved identities, installer location,
remaining consent/publication/pilot work, and operating/recovery instructions.
Mark the installation incomplete if a required acceptance check has not passed.
```

## Collect the customer inputs

| Input | What the assistant needs |
| --- | --- |
| Target | Tenant ID, subscription ID, customer-specific checkout directory, and a unique suffix of 3–12 lowercase letters/digits starting with a letter. |
| Geography and policy | Approved Foundry/portal region, APIM region if different, permitted model deployment SKUs, and applicable Azure Policy/network/tag requirements. Defaults in the example are examples, not a residency decision. |
| Azure authority | An operator with Owner, or Contributor plus Role Based Access Control Administrator, on the target subscription. Check the customer's actual role-assignment scope. |
| Entra authority | Ability to read users, create or use a security group and change its membership, create app registrations and obtain required admin consent. Azure subscription roles do not grant these directory permissions. |
| Contact and terms | APIM publisher name/email. For Claude: legal organization name, two-letter country code, industry and explicit authorization to accept the paid Anthropic Marketplace terms. |
| Clients and models | Claude, Codex or both; an approved model list and capacity for each, plus SKU if required. Empty model lists attempt every eligible model with quota. Capacity is not a spending limit. |
| Access mode | Unified Claude/Codex administration by default. Identify the owner, administrators, approved spend policy and reviewed model prices/capacities. Explicit legacy mode is optional. |
| Sign-in | `azure-cli` is the default and avoids a separate app registration or consent; select `msal` explicitly for developer Macs without Azure CLI, with app registration and any consent required by tenant policy. |
| Developer group | Existing group object ID, or authorization to create a group. Use a customer-specific `developer_group_name` if several installations share a tenant; the default display name can reuse a group. |
| Initial people | Approved work emails, UPNs or tenant-local user object IDs. Include guest users only after they exist in the target tenant. Keep the CSV under `.local/`. |
| Operating owner | Person/team receiving the installer, monitoring and billing alerts, rollout decisions, and private configuration backups. |

The deployer becomes the initial portal owner and joins the developer group by default. If a different owner is required, plan the ownership change with the customer and verify it before handover. A supplied group must permit membership changes; dynamic or externally managed groups need their normal identity-management workflow.

Choose the enforcement path before distributing profiles:

| Path | Result and required follow-up |
| --- | --- |
| Unified (default) | Both enabled providers share portal membership, model grants and the ledger. Runtime/storage are automatic; reviewed model bounds, owner publication and acceptance checks remain required. |
| Baseline (explicit) | Set `portal.mode=baseline` for developer-group inference RBAC and the legacy Claude-only portal. No shared USD enforcement. |
| Legacy Claude portal governance | The same Claude route gains published membership, model and token policies. Import all intended callers and review/publish separately. Codex remains on its separate baseline route. |
| Enforced USD budgets / delegated group administrators | The default deployer includes the runtime. Follow [unified administration](unified-administration.md) for reviewed prices/capacities and policy publication. Complete the [release acceptance checks](budget-enforcement.md#release-acceptance) and bypass review. This remains an evaluation feature. |

Dollar planning fields alone do not enforce spending. Shared enforcement requires runtime verification and a valid owner-published policy. Content capture is also a separate, opt-in [deployment](content-logs.md); leave it off unless requested.

## Prepare and validate locally

Run commands from the repository root. Use a fresh customer-specific directory; do not copy `.local/` or `dist/` from a prior customer.

```bash
git clone --branch main https://github.com/msftse-org/claude-codex-foundry.git CUSTOMER_CHECKOUT
cd CUSTOMER_CHECKOUT
git rev-parse HEAD
uv sync --frozen --extra auth --extra portal
mkdir -p .local
cp examples/deploy.json .local/deploy.json
uv run --frozen python scripts/deploy.py --help
uv run --frozen python scripts/onboard_users.py --help
```

Install Git, Azure CLI with Bicep, uv/Python 3.11+, and Node.js 24/npm before deployment. The portal builder runs `npm ci` and a production build. Check these locally with `git --version`, `az version`, `az bicep version`, `uv --version`, `node --version` and `npm --version`.

Edit `.local/deploy.json` using the customer inputs. Replace every example tenant/subscription/contact/attestation value. Use explicit model lists for a scoped pilot. The unified portal supports Codex-only installations; use `portal.enabled=false` only when intentionally omitting administration. Set `accept_marketplace_terms=true` only after the customer has authorized the relevant terms. The [quick start](quickstart.md#1-describe-the-deployment) describes the supported settings and naming overrides.

Validate settings without calling Azure or printing their contents:

```bash
uv run --frozen python -c 'from pathlib import Path; from scripts.deploy import load_settings; load_settings(Path(".local/deploy.json")); print("Settings valid")'
```

This is structural validation. It does not check quota, Marketplace eligibility, Azure Policy, consent, regional availability or name availability. Read the deployed templates and review Azure Policy requirements before applying. Use the [step-by-step foundation what-if](administrator.md#1-sign-in-and-choose-infrastructure) if the customer requires an ARM change preview; `deploy.py` has no dry-run flag.

Prepare `.local/developers.csv` with one identity per row in the first column:

```csv
email
alice@contoso.com
bob@contoso.com
```

This input adds existing directory users to the developer group. In unified mode, shared-route inference also requires portal membership and publication. It does not create accounts, invite guests, assign licenses, import conversation histories or synchronize an identity provider. Unknown or ambiguous identities need correction by the customer.

## Deploy the authorized plan

```bash
az login --tenant YOUR_TENANT_ID
az account set --subscription YOUR_SUBSCRIPTION_ID
az account show --query '{tenantId:tenantId,subscriptionId:id}' -o json
uv run --frozen python scripts/deploy.py --settings .local/deploy.json
```

Compare the selected IDs to the settings before proceeding. Sign in as the intended owner/deployer; the script expects an interactive user and reads `az ad signed-in-user show`. A service-principal-only CI deployment is not implemented by this wrapper.

The script registers resource providers, prepares group/RBAC, deploys Foundry/APIM/monitoring, deploys models and API routes, qualifies Codex models, prepares sign-in, deploys the unified portal/runtime, exports governed profiles and packages the installer by default. APIM can take 30 minutes or longer. It prints the commands it runs. Model provisioning/qualification may partially succeed; inspect the reports and actual published catalog against the approved plan.

In explicit baseline mode, `--skip-verify` skips the final gateway checks. Unified mode names owner publication and the acceptance checks as pending instead of probing inference before a policy exists. Codex qualification still sends billable model requests; the command is not an offline or free deployment mode. If MSAL admin consent cannot be granted, the script can finish with an action-needed message. Record and complete that consent step before declaring developer sign-in ready. If `grant_admin_consent=false` was chosen, verify consent separately.

On a failure, preserve `.local/deploy-state.json` and the organization files, fix the specific issue, and rerun the same command for the same target. Target changes are rejected before Azure changes. Do not delete state or change the suffix to hide a provisioning error: that can orphan billable resources. Escalate quota, policy, purchase and consent restrictions to their customer owners.

## Import developers and configure governance

After deployment succeeds, confirm Azure CLI is still signed into the customer tenant, then run:

```bash
uv run --frozen python scripts/onboard_users.py --csv .local/developers.csv
```

The script checks the tenant recorded in deployment state, resolves each identity, adds missing group memberships, prints results, and writes `.local/portal-members.csv` with `object_id,name,email`. Existing members are not added again. A nonzero exit can mean some people were added while others could not be resolved; reconcile every input and rerun after correcting failures. Group access may take time to propagate. Each run replaces the generated portal CSV with that run's resolved users, so retain separately named batch files if importing in batches (`--portal-csv .local/portal-batch-01.csv`).

For **baseline access**, the group grant is sufficient after propagation. Installing the portal does not turn on governance, and the script does not import portal members automatically.

For **unified administration**, complete [model verification and shared publication](unified-administration.md) for both providers. For **legacy Claude portal governance**, use the authenticated owner/admin portal session and [member-import workflow](portal-guide.md#members-and-bulk-onboarding):

1. Create the intended portal groups and review their allowed/default models and quotas. Portal groups do not synchronize with Entra groups.
2. Import the generated CSV under **Members → Import CSV**, selecting the appropriate default portal group. Split the file by team if needed. The portal accepts up to 2,500 new developers per file. Exclude the existing owner and other already registered members; duplicate object IDs are rejected rather than updated.
3. Validate and save the import. The import creates developers; it cannot grant administrative roles. Assign any approved roles separately and confirm the owner retains access.
4. Review the complete publication impact, including every intended caller, and publish the approved policy. Unregistered/inactive callers lose access when governance takes effect.
5. Export the pilot user's profile from the portal so it matches that user's model grants, and run the governed verification below. Redistribute profiles if their catalog changed.

If the assistant has no authenticated portal control, hand these concrete steps and the private CSV to the customer owner and mark publication pending. Do not replace Microsoft sign-in with local-owner mode or edit private Blob state to bypass the portal API.

For **enforced budgets**, complete the [unified acceptance procedure](unified-administration.md#acceptance-and-handover); the runtime is already deployed by default. The [low-level runtime guide](../budget/README.md) remains available for manual installations. Publish the approved hierarchy and reviewed enforcement prices, export profiles for the budget routes, remove alternate direct/baseline inference paths as documented, and run its acceptance checks. Keep that work distinct from the standard portal publication above.

## Verify and hand over

For the default unified setup, follow [shared acceptance](unified-administration.md#acceptance-and-handover). Use `claude-foundry verify --config .local/client.json --budgeted` for Claude and `claude-foundry verify --config .local/codex-client.json` for Codex after owner publication. The legacy governed-verifier examples below apply only to `portal.mode=baseline`.

Check the command's final status, model/qualification reports and any consent warnings. Gateway verification uses the deployer's Azure CLI identity; it does not prove a developer's MSAL sign-in, Conditional Access or desktop installation works.

For a baseline deployment, the explicit checks are:

```bash
# Run only for enabled clients. These make small billable requests.
uv run --frozen claude-foundry login --config .local/client.json
uv run --frozen claude-foundry verify --config .local/client.json --all-models
uv run --frozen claude-foundry login --config .local/codex-client.json
uv run --frozen claude-foundry verify --config .local/codex-client.json
```

For published Claude governance, use the current pilot-user profile and add `--governed`:

```bash
uv run --frozen claude-foundry verify --config .local/pilot-client.json --all-models --governed
```

The governed verifier is safe for the shared route. Disruptive suspension/quota tests belong on an [explicitly isolated preview API](../portal/README.md#validation-and-release-checks). For a budget deployment, use its own verification and live acceptance procedure.

Confirm the portal health endpoint returns 200 and anonymous `/api/state` returns 401, then test real owner sign-in. Verify an imported developer can make a request and that telemetry reaches the intended Application Insights resource. Test the ZIP on a pilot Mac using the customer's approved Claude/Codex versions. The combined installer configures Codex when it is installed; a Codex-only bundle configures Codex. Fully reopen the applications and verify the expected organization models. Record unfinished checks explicitly.

Give the customer owner this private handover record:

| Item | Evidence to retain |
| --- | --- |
| Source and resources | Repository commit, deployment date, tenant/subscription, regions/SKUs, resource groups, resource IDs, app IDs and developer group ID. |
| Access and endpoints | Claude/Codex gateway URLs, portal URL, owner, access mode and published policy status; identify any retained baseline/bypass access. |
| Models and tests | Actual available models/defaults, qualification exclusions, inference/stream/tool checks, portal sign-in and pilot Mac results. Distinguish prior project evidence from this customer's checks. |
| Import outcome | Requested/resolved/added/already-member/failed counts, private CSV locations, remaining identities, and portal import/publication status if applicable. |
| Installer | `dist/claude-codex-SUFFIX-macos.zip` and the approved internal distribution channel. Inspect it before distribution; send only the installer bundle, not deployment state or raw reports. |
| Operations | Private backup location for `.local/`, monitoring/billing-alert ownership, consent or quota actions, rollout limits and [operations/recovery instructions](operations.md). |

Do not report success solely because the Function health check or ARM deployment succeeded. Required model, consent, import, publication, budget and pilot checks must have evidence or remain named open actions.

## Offboarding and recovery

For baseline access, remove people from the developer group:

```bash
uv run --frozen python scripts/onboard_users.py --remove alice@contoso.com
```

For portal-governed access, also suspend the member and publish the policy, then verify denial of a new request. Foundry is called by a managed identity on those routes, so group removal alone is insufficient. Budget routes require their budget-policy publication. Review any other direct role assignments; in-flight requests may complete.

Client recovery uses `claude-foundry restore --client claude` or `--client codex` from the installed runtime as described in the [Mac guide](macos.md) and [Codex guide](codex.md). Preserve deployment state for resuming or recovery. Follow the [publication and Blob recovery procedure](operations.md#portal-publication-recovery) for portal incidents; do not restore old budget ledger balances over a live ledger.

Resource deletion, soft-delete purging and removal of Entra applications/groups require a separate retirement decision and inventory of dedicated versus shared resources. See the [quick-start removal notes](quickstart.md#remove-the-deployment). Stopping or deleting the portal alone does not stop APIM charges.
