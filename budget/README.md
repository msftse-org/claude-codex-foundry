# Budget gateway evaluation

This optional preview implements the [approved spending hierarchy](../docs/budget-enforcement.md) and is included in `main`. It uses standard APIM, a separate streaming Functions Flex Consumption app, Azure Table transactions and immutable policy snapshots. The portal and baseline gateways remain separate. Customer release checks still apply.

Before deploying, read the [Azure architecture and full request-flow diagram](../docs/budget-architecture.md): which service checks identity and allowance, where data is stored, how publication works, and what happens when a dependency fails.

The runtime and budget-connected portal include a bounded in-memory cache for immutable, hash-verified policy snapshots. An [optional APIM shared cache](#optional-shared-apim-policy-cache) reuses those snapshots across runtime workers. [The cache design](../docs/budget-architecture.md#how-apim-caching-and-azure-table-work-together) keeps the active policy and all monetary checks in Azure Table; APIM never caches budget approvals. Deploy both application packages for a cache-code update; existing policy, spend, reservations and drafts persist. Semantic prompt caching is not enabled.

## Optional shared APIM policy cache

Use APIM's built-in cache on a supported tier, such as Basic v2; Consumption requires a configured external cache. No Redis resource is created by this repository. The default cache backend is `internal`. `external` requires an existing APIM external-cache configuration; `prefer-external` uses that configuration when present and otherwise the built-in cache. Configure external Redis TLS, credentials, network isolation and retention through your organization's standard infrastructure process.

1. Deploy the portal package with `policyCacheAudience` equal to the runtime's dedicated budget audience and `policyCacheApimPrincipalId` equal to the APIM identity. These set `BUDGET_POLICY_CACHE_AUDIENCE` and `BUDGET_POLICY_CACHE_APIM_PRINCIPAL_ID`. The origin is otherwise disabled. It accepts only that APIM application identity, never portal user tokens.
2. Install the machine-only operation into the already-owned Claude budget API, allowing the runtime identity's **object ID** as its reader:

```bash
uv run python scripts/deploy_policy_cache.py --config .local/organization.json \
  --api-id claude-budget-preview \
  --origin-url https://BUDGET_PORTAL_NAME.azurewebsites.net \
  --audience api://BUDGET_APPLICATION_ID \
  --reader-object-id BUDGET_RUNTIME_IDENTITY_OBJECT_ID \
  --organization-id LEDGER_ORGANIZATION_UUID --caching-type internal
```

3. Set runtime template parameter `policyCacheUrl` (app setting `BUDGET_POLICY_CACHE_URL`) to `https://YOUR_APIM.azure-api.net/claude-budget-preview/v1/policy-snapshots`, then deploy/restart the runtime. It authenticates to APIM with its managed identity and the existing budget audience. The portal origin uses its existing private Blob access; APIM needs no Blob role or private-storage network access.
4. Verify an initial shared miss, a subsequent shared hit, unchanged authoritative balances and budget refusals. The owner/admin-only `/claude-budget-preview/v1/policy-cache/status` endpoint reports current-worker counters, not organization-wide totals. Its `shared_probe` bypasses local memory, verifies the shared document and reports `hit` or `miss` independently of worker recycling. `failed` requires investigation even if direct Blob fallback keeps inference working. A separate cold-worker test verifies the normal L1-miss path.

To inspect the policy in Azure: open **API Management → APIs → Claude via Azure Foundry — budget preview → Design → Policy snapshot cache (runtime identities only)**, then open the operation's policy code. The operation ID is `policy-snapshot`, with URL `/v1/policy-snapshots/{digest}`. It contains `cache-lookup-value` and `cache-store-value` with the selected `caching-type`; model inference operations remain uncached. The Codex budget runtime uses the same shared snapshot endpoint.

The shared operation rejects user tokens before cache lookup. The origin is separate from the inference app, avoiding a cache-miss callback into a worker waiting on itself. A shared-cache failure falls back to the runtime's exact-version private Blob read. Clearing `BUDGET_POLICY_CACHE_URL` disables L2 without changing published policy or ledger state; existing snapshots expire from APIM normally. Do not purge or rewrite counters to test cache behavior.

Do not distribute its client profile before the release acceptance tests pass. The template is an evaluation deployment, not an automatic customer migration.

## Prerequisites

Deploy the existing portal/network and Foundry/APIM foundation first. An administrator needs permissions to create a single-tenant Entra application/service principal, deploy resources, and assign storage/inference roles. Record the APIM system-assigned identity object ID, portal identity object ID, Foundry account, tenant and subscription.

Use a dedicated application audience for the budget service. No client secret is needed:

```bash
az ad app create --display-name YOUR_BUDGET_API --sign-in-audience AzureADMyOrg
az ad app update --id BUDGET_APPLICATION_ID --identifier-uris api://BUDGET_APPLICATION_ID
az ad sp create --id BUDGET_APPLICATION_ID
```

Use `api://BUDGET_APPLICATION_ID` as `audience` in a private parameter file. Do not grant user access to this backend application; the service verifies the APIM identity explicitly.

## Infrastructure and code

Prepare `.local/budget.parameters.json` using the parameters in [infra/budget.bicep](../infra/budget.bicep). `organizationId` is a stable UUID for this ledger, shared by both providers. `deployments` maps deployment names to `provider`, `version`, `max_input_tokens`, and `max_output_tokens`; these must match the reviewed price/capacity policy. Set `supports_reasoning: false` for a qualified non-reasoning OpenAI deployment so the gateway removes Codex’s unsupported reasoning field. Pin model deployments against automatic upgrades.

Codex requests containing a `compaction_trigger` input item need at least 20,000 output tokens. When the client omits the output limit, the gateway uses at least that amount and reserves its full cost before dispatch. Explicit smaller limits or models with a verified output maximum below 20,000 are rejected before reservation. Ordinary requests retain their reviewed default output limit.

Deployment throughput is separate from the per-request context/output limits above. New model deployment plans start conservatively at 10 capacity units. To review unused quota and maximize **existing metered** deployments, run:

```sh
uv run python scripts/scale_model_capacity.py --config .local/organization.json
# Apply after reviewing the plan:
uv run python scripts/scale_model_capacity.py --config .local/organization.json --apply
```

The scaler preserves model versions and upgrade policies, honors SKU increments and maximums, and uses only unallocated quota. Other resources' allocations remain in place. Shared pools are allocated once in deployment-name order; review the plan when multiple deployments share a pool. Re-run the plan after Azure reports all updates succeeded; quota changes can take up to 15 minutes to propagate. A skipped item needs manual review, and a failed PATCH must be reconciled before retrying.

The default uses the existing portal VNet and private Blob/Table endpoints. Deploy into the portal resource group and provide `portalVnetName`. Identity-protected public storage is an explicit `privateStorage=false` option only where organizational policy permits it. The template disables shared keys and public Blob access in both modes. Private endpoints have a small ongoing cost; Functions has no configured always-ready instances.

```bash
az deployment group what-if -g PORTAL_RESOURCE_GROUP \
  --template-file infra/budget.bicep --parameters @.local/budget.parameters.json
az deployment group create -g PORTAL_RESOURCE_GROUP --name budget-preview \
  --template-file infra/budget.bicep --parameters @.local/budget.parameters.json
uv sync --frozen --extra portal
uv run --extra portal python scripts/build_budget.py
az functionapp deployment source config-zip -g PORTAL_RESOURCE_GROUP \
  -n BUDGET_FUNCTION_NAME --src dist/claude-codex-budget-preview.zip --build-remote true
uv run python scripts/deploy_budget_gateway.py --config .local/organization.json \
  --budget-url https://BUDGET_FUNCTION_NAME.azurewebsites.net \
  --audience api://BUDGET_APPLICATION_ID
```

The new API paths default to `/claude-budget-preview` and `/codex-budget-preview`. The deployment helper refuses to overwrite the known baseline APIs or an API with another ownership marker. Only after the authenticated API policy is installed does it disable subscription-key requirements. Failed creation remains closed.

Private networking must exist when the Flex app initializes. If subscription policy disables a public storage account during initial provisioning, redeploying a private endpoint alone may leave a failed deployment environment. Validate DNS, role assignment and VNet integration first; do not open storage access or enable shared keys to make deployment succeed.

## Configure and publish policy

For a separate hosted evaluation portal, use [infra/budget-portal.bicep](../infra/budget-portal.bicep) in the budget resource group. It reuses the budget account's private Blob/Table endpoints and the existing Functions subnet, creates a separate deployment container and portal-state container, and runs on a 512 MB Flex Consumption plan with no always-ready instances. It does not modify the existing portal or baseline gateway.

Prepare a private parameter file with the template's organization configuration, stable ledger organization ID, owner identity, Entra SPA client ID, approved redirect URI, gateway URL, model metadata and monitoring resources. Use a dedicated Entra registration for customer environments. An evaluation may reuse an existing portal registration only after adding the new SPA redirect while preserving its existing redirects and scope. `additionalModels` uses the portal catalog fields `name`, `labelOverride` and `anthropicFamilyTier` (use `openai` for OpenAI entries).

```bash
az deployment group what-if -g PORTAL_RESOURCE_GROUP \
  --template-file infra/budget-portal.bicep --parameters @.local/budget-portal.parameters.json
az deployment group create -g PORTAL_RESOURCE_GROUP --name budget-admin-preview \
  --template-file infra/budget-portal.bicep --parameters @.local/budget-portal.parameters.json
uv run --extra portal python scripts/build_portal.py
az functionapp deployment source config-zip -g PORTAL_RESOURCE_GROUP \
  -n BUDGET_PORTAL_NAME --src dist/claude-foundry-admin-preview.zip --build-remote true
```

The new portal identity receives Blob Data Owner and Table Data Contributor on budget storage, Monitoring Reader/Metrics Publisher on the specified Application Insights resource, and optional Log Analytics Reader on the dedicated content workspace. It receives no APIM policy-write or Foundry inference role. Azure data operators remain inside the trust boundary.

On first authenticated access to an empty portal store, the portal imports identities, groups, ceilings and reviewed rates from the existing ledger's immutable active policy. The configured owner must already be an active owner in that policy. This does not activate policy, reset spend, release reservations or clear a paused ledger. Redeployments preserve existing drafts and audit history. An unavailable ledger or invalid policy prevents initialization; it is not replaced with permissive defaults. If the ledger has never been initialized, the owner starts with an unpublished draft and must configure and publish finite limits.

Deploy the portal from the same branch. Set these app settings through ARM or Azure CLI using a private JSON file (do not put values or credentials into Git):

| Setting | Purpose |
| --- | --- |
| `BUDGET_STORAGE_ACCOUNT` | Ledger and policy storage account from the template output |
| `BUDGET_ORGANIZATION_ID` | Same stable UUID used by the runtime |
| `BUDGET_CLAUDE_GATEWAY_URL` | Validated APIM URL ending in `/claude-budget-preview`; governed profile exports require this |
| `PORTAL_BUDGET_MODELS_JSON` | Additional deployed model metadata for the portal, such as OpenAI models |

The owner uses **Budgets & limits** to set the organization limit and verified prices. Group drawers expose owner ceilings, delegated models and operating limits; member drawers expose personal limits and the group-administrator role. Publish the budget policy separately from the legacy APIM token policy. Legacy gateway publication is disabled once this portal is connected to the budget ledger. A group administrator publishes only their team changes against the already active owner-controlled policy.

For private-storage bootstrap before the portal is connected, the runtime supports a server-owned `BUDGET_BOOTSTRAP_JSON` setting containing a reviewed portal-state document. The first authenticated APIM call validates it, writes its immutable snapshot and initializes the empty ledger atomically. It cannot replace any existing active policy, reset balances or clear a paused incident. Remove the setting after initialization. Prefer portal publication for normal administration.

`GET /v1/budget` on either budget API returns the caller's ledger balance and held request IDs. Owners/global administrators also receive their organization/group balances. This endpoint is authenticated like inference. Do not expose storage directly to developer machines.

## Client compatibility and verification

Claude uses `/v1/messages`, `/v1/messages/count_tokens` and `/v1/models`. Codex uses `/v1/responses` and `/v1/models`; its provider base URL normally ends in `/codex-budget-preview/v1`. Use the existing installers/configuration commands with a reviewed profile that names the new endpoint. The budget gateway currently rejects background jobs, premium tiers, server tools and unsupported protocol fields. It cannot promise compatibility with every future native-client feature.

Verify a synthetic JSON request and streaming request for each provider, then confirm the ledger settled. Lower a test user's remaining allowance below the maximum request reservation and confirm rejection without a Foundry dependency. Use separate test identities/groups and tiny test budgets; do not exhaust customer allowances as a test.

Use the existing opt-in content-log deployment for the new API IDs only when approved. Group-admin content requires the `budget-governance` group attribution trace and the updated portal query implementation. Existing logs without it remain unavailable to group admins.

The durable ledger, scoped portal controls and streaming proxy have automated tests. See [release acceptance](../docs/budget-enforcement.md#release-acceptance) before making a customer rollout decision.
