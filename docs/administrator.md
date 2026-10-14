# Organization deployment

[← README](../README.md) · [Mac guide](macos.md) · [Operations](operations.md)

This guide takes an Azure administrator from an empty resource group (or existing resources) to a verified gateway and a Mac installation ZIP. Developers receive the ZIP; they do not perform these cloud steps. The commands in this guide configure the **baseline user-token gateway**. Add the [isolated portal preview](../portal/README.md#isolated-azure-preview) afterward to evaluate per-member governance. Do not run the baseline `gateway` command against a portal-governed API: it replaces that API’s policy.

## Before you begin

Install Git, [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli), and [uv](https://docs.astral.sh/uv/). Clone the repository and run every command below from its root:

```bash
git clone https://github.com/msftse-org/claude-foundry.git
cd claude-foundry
uv sync --frozen --extra auth
```

Have the subscription/tenant IDs, approved regions, publisher contact, legal Marketplace details, and an Entra inference group ready. Parameter files and generated profiles belong in ignored `.local/`, not public Git. Review expected APIM, model, and monitoring charges with your subscription owner.

Use a dedicated resource group for a new installation. The administrator needs Azure deployment permissions, permission to assign inference RBAC, and subscription eligibility for the selected Anthropic Marketplace offers. Azure CLI and uv are administrator prerequisites. Confirm tenant, subscription, region and Marketplace billing details before applying deployments.

## 1. Sign in and choose infrastructure

```bash
az login --tenant YOUR_TENANT_ID
az account set --subscription YOUR_SUBSCRIPTION_ID
mkdir -p .local
```

**Existing infrastructure:** copy `examples/organization.json` to `.local/organization.json`, replace the placeholders, and continue at step 2. The gateway command owns the specified APIM API and its three operations. Use a new, dedicated API ID/path; it does not replace other APIs or service-level policies/diagnostics. Ensure users or an Entra group have **Cognitive Services User** at the Foundry account scope. Azure Contributor alone does not grant inference permission.

**New infrastructure:** copy `infra/parameters.example.json` to `.local/parameters.json`. Supply globally unique resource names, your publisher contact, supported regions, and the object ID of an Entra security group in `inferencePrincipals`. Use `type: Group` for groups and `type: User` for individual users. The foundation disables Foundry API keys, grants inference RBAC, and creates APIM plus Application Insights and Log Analytics. APIM has an ongoing service charge; inference and logging are metered.

```bash
az group create --name YOUR_RESOURCE_GROUP --location eastus2 --subscription YOUR_SUBSCRIPTION_ID
az deployment group what-if --resource-group YOUR_RESOURCE_GROUP --subscription YOUR_SUBSCRIPTION_ID \
  --template-file infra/main.bicep --parameters @.local/parameters.json
az deployment group create --name claude-foundation --resource-group YOUR_RESOURCE_GROUP \
  --subscription YOUR_SUBSCRIPTION_ID --template-file infra/main.bicep \
  --parameters @.local/parameters.json \
  --query properties.outputs.organizationConfig.value -o json > .local/organization.json
```

Review the generated configuration. Add `model_provider_data` with the organization's legal name, two-letter country code and industry when the selected models require Marketplace attestation. Use the shape in `examples/organization.json`.

## 2. Discover and deploy models

```bash
uv sync --frozen --extra auth
uv run claude-foundry admin --config .local/organization.json models
```

Review `.local/models-report.json`. Discovery examines the account's regional catalog, subscription quota, retirement dates and existing deployments. It prefers Azure-hosted/default variants, preserves existing deployments and capacities, and requests at most 10 capacity units for each missing model by default. `--capacity N` changes that request. Capacity units and billing vary by model/SKU; they are not a spending cap.

Deploying can accept paid Marketplace terms for the organization. Review [Microsoft's deployment and Marketplace attestation guidance](https://learn.microsoft.com/en-us/azure/developer/ai/how-to/deploy-claude-foundry) and the relevant Anthropic offer terms before using the explicit acceptance flag.

```bash
uv run claude-foundry admin --config .local/organization.json models \
  --apply --accept-marketplace-terms --wait-seconds 900
uv run claude-foundry admin --config .local/organization.json sync-models
```

The report distinguishes retired models, missing quota, purchase restrictions and provisioning failures. The command exits with code 2 if a deployment failed or remains pending; it retains the report and successful deployments. Models listed in the catalog are not necessarily purchasable by the subscription. Organization purchase restrictions must be resolved by the subscription/billing administrator.

Deployment defaults to `GlobalStandard`. Where the regional catalog and subscription support it, explicitly select `--sku DataZoneStandard` to use that deployment type and its separate quota. Use `--model MODEL_NAME` (repeatable) to target particular models. For example, after reviewing the plan:

```bash
uv run claude-foundry admin --config .local/organization.json models \
  --model claude-opus-5-5 --sku DataZoneStandard --report .local/datazone-models.json
uv run claude-foundry admin --config .local/organization.json models \
  --model claude-opus-5-5 --sku DataZoneStandard --report .local/datazone-models.json \
  --apply --accept-marketplace-terms
```

The command never changes an existing deployment's SKU or capacity, and it does not silently fall back between deployment types. Review the selected type's regional availability, data-processing geography and pricing for your organization.

### Size quota for Desktop traffic

The default capacity of 10 is a starting allocation for validation, not an organization-wide sizing recommendation. Desktop can issue several token-counting requests together. Check each deployment's actual `properties.rateLimits`, monitor 429 responses by operation, and allocate sufficient request and token capacity before rollout. [Claude quota is shared across resources and regions for Global Standard, and within a data zone for Data Zone Standard](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-quotas-limits).

For a new model, use `models --model MODEL_NAME --capacity N` (and `--sku` when applicable). For an existing deployment, change its capacity in Foundry or use an explicit ARM patch after reviewing available quota. This keeps the model and deployment type unchanged:

```bash
az rest --method patch --subscription YOUR_SUBSCRIPTION_ID \
  --url 'https://management.azure.com/subscriptions/YOUR_SUBSCRIPTION_ID/resourceGroups/YOUR_RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/YOUR_FOUNDRY_ACCOUNT/deployments/YOUR_DEPLOYMENT_NAME?api-version=2025-10-01-preview' \
  --body '{"sku":{"name":"DataZoneStandard","capacity":40}}'
```

Replace the example SKU with the deployment's current SKU and the capacity with the reviewed allocation. Wait for `provisioningState: Succeeded`, then repeat the affected live check and inspect the response's `x-ratelimit-*` headers. A successful management update does not prove that the inference service already enforces the new limits. More users or large uncached contexts may require a subscription quota increase; adding a resource or changing region does not create new Global Standard quota.

`sync-models` includes successful Anthropic deployments and sets the newest model in each family as the default. Review the list before publishing it. Provisioning success must still be checked with live inference. To intentionally limit the published catalog, remove models from the local configuration after synchronization; the gateway enforces that list for all its users.

## 3. Publish the dedicated API

```bash
uv run claude-foundry admin --config .local/organization.json gateway
uv run claude-foundry admin --config .local/organization.json observability --enable-dimensions
```

In APIM's API list, the API appears as **Claude via Azure Foundry**. The organization setting `api_display_name` controls this name independently of the Desktop `profile_name`.

The API exposes `/v1/messages`, `/v1/messages/count_tokens` and an authenticated `/v1/models`. It validates the Entra issuer, tenant, Cognitive Services audience, token signature and expiry. Optional `allowed_object_ids` restricts APIM to listed **user or service-principal object IDs**; these are not group IDs. An empty list permits authenticated tenant callers at APIM while Foundry RBAC still decides inference access.

The observability command reuses the connected Application Insights logger and enables information traces/custom metrics at this API's scope. It preserves existing diagnostic settings, including payload logging choices. Follow the [observability guide](observability.md) to review those choices and enable metric dimensions. For classic APIM tiers, set `telemetry.token_metrics` to `false`.

## 4. Choose user sign-in

### Azure CLI

No new app registration is needed:

```bash
uv run claude-foundry admin --config .local/organization.json export-client \
  --auth-mode azure-cli --output .local/client.json
```

### MSAL without Azure CLI

A tenant app administrator registers a single-tenant public desktop client with a `http://localhost` redirect URI and the delegated Cognitive Services `user_impersonation` permission. It has no client secret. The supplied script discovers the resource app and scope instead of hardcoding their IDs:

```bash
uv run python scripts/register_client_app.py --tenant YOUR_TENANT_ID \
  --name 'Your Organization Claude Sign-in' --grant-admin-consent
uv run claude-foundry admin --config .local/organization.json export-client \
  --auth-mode msal --client-id RETURNED_CLIENT_ID --output .local/client.json
```

The registration script requires directory read/app registration permissions; consent requires an appropriate Entra admin role. Omit `--grant-admin-consent` if a separate administrator will grant consent. The app name must be unique. The client follows the tenant's MFA and Conditional Access policies. This browser-based helper does not implement device broker authentication; policies requiring a broker or device-bound tokens may require a different approved login flow.

## 5. Verify and distribute

To offer existing-history migration, add a reviewed `conversation_import` policy to the organization config before exporting the client profile. The [migration guide](conversation-migration.md#for-platform-administrators) covers source-organization restrictions and managed fleets. Import remains opt-in and uses Claude Desktop's native wizard.

```bash
uv run --extra auth claude-foundry login --config .local/client.json
uv run --extra auth claude-foundry verify --config .local/client.json --all-models
uv run python scripts/build_client_bundle.py --profile .local/client.json \
  --output dist/claude-foundry-macos.zip
```

Distribute the bundle through an approved internal channel. Its file allowlist excludes `.local`, deployment reports, cloud administrator configuration, Git metadata and caches. `client.json` contains tenant ID, gateway URL, public client ID when applicable, and the model list; it contains no secret. Users follow [the Mac guide](macos.md).

To update models, repeat discovery/deployment, synchronize, redeploy the gateway, export and verify a fresh profile, then redistribute the bundle. Existing client catalogs do not refresh automatically.

## Completion checklist

You should now have a dedicated **Claude via Azure Foundry** API, successfully verified model calls, telemetry in the intended Insights resource, and a client ZIP ready for internal distribution. Test that ZIP on a pilot Mac before wider rollout. For portal evaluation, continue with the [Azure preview guide](../portal/README.md#isolated-azure-preview); for ongoing ownership, upgrades, and offboarding, use [operations](operations.md).

## Recovery

Before an organization change, retain the local config and export the current API policy/operations through Azure's normal backup process. Reapply the last reviewed configuration to revert a gateway change. Model deletion and foundation teardown are deliberately separate from installation. Do not delete a shared resource group to remove this solution. User settings have a separate restore command described in the Mac guide.

To retain a deployment for diagnosis while excluding it from future synchronization, add its exact deployment name to an `excluded_deployments` array in the organization configuration. This list is applied by `sync-models` and is not exported to users. Redeploy the gateway and export a new client profile after changing it.
