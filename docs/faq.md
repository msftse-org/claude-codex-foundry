# Frequently asked questions

[← README](../README.md)

## Is this a replacement for Claude Desktop or Claude Code?

No. It configures the installed Claude applications to call organization-owned Anthropic model deployments in Azure Foundry through standard APIM. It does not ship or update Claude itself.

## Do developers need Azure CLI or an Azure subscription?

They do not need subscription administration. An MSAL client profile lets them use browser sign-in without Azure CLI. An Azure CLI profile reuses the official CLI. There is no supported authentication-only Azure CLI distribution; MSAL is the lighter installation path here.

The baseline gateway requires the developer or a group to have Foundry inference RBAC. Portal governance uses gateway membership and APIM's managed identity instead. The portal SPA app registration and the desktop public-client registration are separate.

## Will existing conversations move to the Azure profile?

Yes, through Claude Desktop's native import wizard. Claude Foundry supports enabling it during onboarding with `--enable-history-import` or an organization import policy. It copies eligible history into the Azure-connected profile and preserves originals. Source permissions and file-content limitations apply; see [conversation migration](conversation-migration.md). Restoring installation settings does not undo an import.

## Does this need the new Azure AI Gateway product?

The repository uses standard Azure API Management. The new foundation supports Basic v2 or Standard v2; Anthropic token metrics require a v2 tier. The API's display name is **Claude via Azure Foundry**.

## Are all Anthropic models available?

Discovery checks the regional catalog, subscription quota, retirement information, and existing deployments. Purchasability and inference availability still depend on the subscription and deployment type. Deployments must pass live verification before being published to users. The nine models shown in the gallery are the preview's configured catalog, not a promise of nine models in every customer subscription.

## Is the portal production-ready for 2,000 developers?

It is a working preview. Offline import and generated-policy-size checks cover 2,000 members, but representative concurrency, customer identity policies, recovery, and operating costs still need validation. Start with an isolated pilot and follow the [validation record](../portal/VALIDATION.md).

## Does a dollar budget stop spending?

No. Dollar budgets are planning thresholds. Published token quotas and rate limits can restrict gateway traffic, but streaming estimates and concurrency can overshoot and gateway regions do not share an atomic global counter. Configure Azure billing alerts separately.

## Why does estimated cost differ from Azure billing?

The portal multiplies reported input/output tokens by the currently configured planning rates. Cache categories, some prompt-length tiers, discounts, tax, and infrastructure costs are not reconciled. Historical estimates change when rates change. Azure Cost Management is authoritative for billed spend; see the [pricing reference](../portal/PRICING.md).

## Can administrators read developers' prompts or chats?

The portal does not offer conversation viewing or retention controls. New gateway diagnostics are configured for operational metadata with zero body bytes; existing APIM diagnostics are preserved and must be reviewed separately. Provider handling and local Claude storage follow their own configuration and terms.

## Does portal membership synchronize with Entra or send invitations?

No. Administrators register existing Entra object IDs or import a validated CSV. Members have one primary portal group plus optional personal model grants. The portal does not create directory accounts, send invitations, implement SCIM, or enroll devices in MDM.

## What happens if the portal is offline?

The last published APIM policy remains in the inference path. The portal is needed to inspect and publish new administrative changes, not to serve every model request.

## Where do I get help?

Ask your organization about access, installation bundles, billing, and tenant policy. Use [troubleshooting](troubleshooting.md) for common errors and sanitized GitHub issues for project defects. Follow [SECURITY.md](../SECURITY.md) for security reporting. No commercial support SLA is included.
