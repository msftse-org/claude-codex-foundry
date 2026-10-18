# Budget gateway evaluation

This unreleased add-on implements the [approved spending hierarchy](../docs/budget-enforcement.md). It uses standard APIM, a separate streaming Functions Flex Consumption app, Azure Table transactions and immutable policy snapshots. The portal and baseline gateways remain separate.

Before deploying, read the [Azure architecture and full request-flow diagram](../docs/budget-architecture.md): which service checks identity and allowance, where data is stored, how publication works, and what happens when a dependency fails.

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
