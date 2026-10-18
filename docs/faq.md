# Frequently asked questions

[← README](../README.md)

## Is this a replacement for Claude Desktop or Claude Code?

No. It configures the installed Claude applications to call organization-owned Anthropic model deployments in Azure Foundry through standard APIM. It does not ship or update Claude itself.

## Do developers need Azure CLI or an Azure subscription?

They do not need subscription administration. An MSAL client profile lets them use browser sign-in without Azure CLI. An Azure CLI profile reuses the official CLI. There is no supported authentication-only Azure CLI distribution; MSAL is the lighter installation path here.

The baseline gateway requires the developer or a group to have Foundry inference RBAC. Portal governance uses gateway membership and APIM's managed identity instead. The portal SPA app registration and the desktop public-client registration are separate.

## Will existing conversations move to the Azure profile?

Yes, through Claude Desktop's native import wizard. Claude & Codex on Azure supports enabling it during onboarding with `--enable-history-import` or an organization import policy. It copies eligible history into the Azure-connected profile and preserves originals. Source permissions and file-content limitations apply; see [conversation migration](conversation-migration.md). Restoring installation settings does not undo an import.

## Can Codex Desktop and CLI use the same Azure foundation?

Yes, on the `feat/codex-azure` feature branch. A separate **Codex via Azure Foundry** API exposes stateless OpenAI Responses with Entra authentication. The Mac installer preserves existing Codex settings and sessions. The baseline portal governance remains Claude-only; the unreleased budget hierarchy adds shared Claude/Codex permissions and monetary counters on dedicated routes. See [Codex setup](codex.md), [budget architecture](budget-architecture.md) and the [model/client validation record](codex-validation.md).

Existing OpenAI-backed Codex conversations can contain encrypted state Azure cannot decrypt. The included read-only history exporter supports a reviewed visible-text handoff into a new Azure conversation. It preserves the original but does not transfer attachments, tool payloads or hidden reasoning. Ordinary ChatGPT cloud history is outside this integration.

## Does this need the new Azure AI Gateway product?

The repository uses standard Azure API Management. The new foundation supports Basic v2 or Standard v2; Anthropic token metrics require a v2 tier. The API's display name is **Claude via Azure Foundry**.

## Are all Anthropic models available?

Discovery checks the regional catalog, subscription quota, retirement information, and existing deployments. Purchasability and inference availability still depend on the subscription and deployment type. Deployments must pass live verification before being published to users. The nine models shown in the gallery are the preview's configured catalog, not a promise of nine models in every customer subscription.

## Is the portal production-ready for 2,000 developers?

It is a working preview. Offline import and generated-policy-size checks cover 2,000 members, but representative concurrency, customer identity policies, recovery, and operating costs still need validation. Start with an isolated pilot and follow the [validation record](../portal/VALIDATION.md).

## Does a dollar budget stop spending?

Legacy planning amounts do not stop requests. The unreleased [budget gateway](budget-enforcement.md) adds atomic organization/group/user reservations across Claude and Codex. It applies only after that service and its dedicated routes are deployed and a reviewed policy is published. All limits apply to owners too. A zero spending allowance blocks new requests; an unset personal allowance inherits the group limit. Existing endpoints retain their previous behavior. Configure Azure billing alerts separately for infrastructure and other charges.

## What is the budget service, and who checks each request?

It is custom Python code from this repository, running in a separate Azure Functions Flex Consumption app in your subscription. APIM validates the developer's Entra token. The budget Function validates APIM's managed identity, checks published user/group/model permissions, reserves allowance in Azure Table Storage, and only then calls Foundry using its own managed identity. It settles from final provider usage. The [complete Azure flow and diagram](budget-architecture.md) explain every service, identity and failure boundary.

## Where are budgets and usage saved?

Private Blob Storage holds portal drafts and audit history in `portal-state/organization.json`, plus immutable published snapshots in `budget-policies`. Azure Table Storage's `BudgetLedger` holds the active policy reference, monthly organization/group/user counters and request reservations/settlements. Claude and Codex share those counters. Optional prompt/response captures are stored separately in a dedicated Log Analytics workspace. See the [data storage map](budget-architecture.md#where-budgets-usage-and-conversations-are-stored).

## Can concurrent requests spend the same remaining allowance?

The budget service commits all three counters and each reservation in one conditional Azure Table transaction. Competing updates cannot both commit against the same stale balance. A failed reservation prevents model dispatch. This mechanism still requires load testing for the customer's intended traffic; it does not establish a 2,000-concurrent-user capacity guarantee.

## Why can a request be refused when some money remains?

The remaining allowance must cover a conservative maximum request reservation, including other running requests. The reservation uses verified model input capacity and the enforced output limit. Complete usage releases the unused amount; missing usage or an uncertain outcome keeps it held. See the [reservation example](budget-architecture.md#6-provider-usage-settles-the-reservation) and [recovery rules](budget-enforcement.md#failure-and-recovery).

## Why does estimated cost differ from Azure billing?

The portal multiplies reported input/output tokens by the currently configured planning rates. Cache categories, some prompt-length tiers, discounts, tax, and infrastructure costs are not reconciled. Historical estimates change when rates change. Azure Cost Management is authoritative for billed spend; see the [pricing reference](../portal/PRICING.md).

## Can administrators read developers' prompts or chats?

Yes, when an organization explicitly enables [prompt and response logging](content-logs.md). Portal owners and administrators can inspect newly captured Claude/Codex gateway messages after recording an access reason. Billing, auditor and developer roles cannot view them. Capture is off by default, with seven-day configured retention. Earlier uncaptured chats cannot be recovered, and full local conversation history is not imported.

The budget hierarchy preview additionally scopes group administrators to their own team's request-time-attributed logs. Moving a user into another team does not expose their previous team's history. See [the budget identity and storage flow](budget-architecture.md).

## Does portal membership synchronize with Entra or send invitations?

No. Administrators register existing Entra object IDs or import a validated CSV. Members have one primary portal group plus optional personal model grants. The portal does not create directory accounts, send invitations, implement SCIM, or enroll devices in MDM.

## What happens if the portal is offline?

Legacy governance continues with its last published APIM policy. Budget routes continue with the published Blob/Table policy while the budget Function and storage remain available. The portal is needed for administration, not every model request. If the budget service or ledger is unavailable, new budgeted inference fails rather than bypassing enforcement. See the [failure table](budget-architecture.md#what-happens-when-something-fails).

## Can developers bypass these budgets?

They can if they retain another authorized inference path, such as direct Foundry RBAC, a baseline gateway route or a model key. Customer rollout must remove these alternatives and distribute the governed client profile. Azure administrators who can change runtime code, settings, RBAC or ledger data remain inside the trust boundary. These limits cover qualified model inference, not the entire Azure bill.

## Where do I get help?

Ask your organization about access, installation bundles, billing, and tenant policy. Use [troubleshooting](troubleshooting.md) for common errors and sanitized GitHub issues for project defects. Follow [SECURITY.md](../SECURITY.md) for security reporting. No commercial support SLA is included.
