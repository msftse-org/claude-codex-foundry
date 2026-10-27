# Management portal guide

> **Budget hierarchy branch:** the additional `group_admin` role, owner-controlled USD ceilings, shared ledger and request-time team content scope are documented in [Enforced USD limits](budget-enforcement.md). These require the dedicated budget runtime and remain subject to its release checks. The legacy token/planning controls below still describe the existing gateway.

[← README](../README.md) · [Deploy the portal](../portal/README.md#azure-deployment) · [Operations](operations.md)

The portal gives your organization one place to manage developer access through its Claude gateway and understand usage. It runs in your Azure subscription and uses your Microsoft Entra tenant. It is currently a **preview**; review the [validation record](../portal/VALIDATION.md) before a production rollout.

This walkthrough uses screenshots captured from the real Azure-hosted application in Edge on **October 14, 2026**. They show the isolated preview's actual state and small verification workload, including deliberate denial tests. Captures omit operator identities and infrastructure identifiers. No fictional customers or usage have been added. Your models, roles, traffic, and prices will differ.

## Start with your role

Open the portal URL supplied by your platform team in your normal work browser. Choose **Continue with Microsoft** and use the account authorized by your organization. Existing browser sign-in can be reused; tenant MFA and Conditional Access still apply.

![Portal sign-in with Microsoft](images/portal-sign-in.jpg)

An **owner or administrator** manages people, groups, and gateway policy. **Billing** maintains organization planning budgets and model rates. **Auditors** inspect administrative data. **Developers** download their setup profile and request model access; they do not receive the full administrative view shown below.

## Overview

Start here to see gateway activity, active developers, failed requests, policy status, and items requiring review. Open analytics for request details or review a draft before publishing it. The recent-changes panel links to audit events.

![Overview metrics, developer activity, policy review reminders, and organization health](images/portal-overview.jpg)

*The screenshot shows a real test day. Its error count includes intentional authentication and authorization tests; it is not an availability benchmark.*

## Members and bulk onboarding

Open **Members → Add member**. Enter the person's Entra object ID from your tenant, name, email, role, and primary group. For a guest, use the guest object's ID in your tenant. Choose a monthly personal token limit or leave `0` for no personal limit. Group and organization controls still apply.

Personal model grants add exceptions to the primary group's models. Save the member draft, then review and publish it to change gateway access. Registration does not create an Entra account or send an invitation.

![Member editor with labeled fields, role and group selectors, limits, and personal grants](images/portal-member-drawer.jpg)

The members workspace also provides search/filtering, export, editing, and suspension. Suspending a member blocks portal access from current state; gateway access changes after publication. It does not erase local Claude conversations.

<details>
<summary>Import a team from CSV</summary>

Choose **Import CSV**, select the default portal group, and use the supplied template. Required columns are `object_id,name,email`; `group_id` is optional. Validate and review before saving. A file can contain up to 2,500 new developers. Existing members are not overwritten, duplicate IDs are rejected, and imports cannot assign elevated roles.

![CSV import drawer with default group, file picker, template download, and validation action](images/portal-member-import.jpg)

</details>

## Groups

Use groups to give a team a common model selection, default model, monthly token quota, and planning budget. Each developer has one primary portal group; these groups do not synchronize with Entra groups. A group's default model must be one of its allowed models.

![Groups workspace showing member count, default model, allowed models, and token allowance](images/portal-groups.jpg)

Open a group to edit its grants in a drawer. Save, then publish to apply gateway controls. A dollar planning budget remains an estimate threshold rather than a billing cap.

## Roles and permissions

Portal roles govern administrative actions; model grants govern inference access. Assign the smallest role the person needs. Only an owner can change ownership. The application prevents self-suspension/self-role changes and removal of the last active owner.

![API-enforced matrix for owner, administrator, billing, auditor, and developer permissions](images/portal-roles.jpg)

These roles do not grant Azure subscription administration or Entra directory privileges.

## Model access and publication

**Model access** lists the configured deployments and which groups can use them. Deployment and quota management still happen through the administrator CLI/Azure. The portal publishes access to the configured catalog; it does not purchase or deploy new models.

<details>
<summary>View the deployed model catalog</summary>

![Nine configured Claude models with family, group access, and deployment status](images/portal-model-access.jpg)

</details>

The everyday workflow is **edit → save draft → review → publish → verify**:

1. Save member, group, and token-limit changes.
2. Choose **Review & publish** and inspect affected membership and the target gateway.
3. Confirm the organization name and publish the reviewed revision.
4. Verify a new allowed request on the configured route, for example with `claude-foundry verify --governed`, and confirm its telemetry. Use an explicitly isolated API for disruptive suspension/quota tests. Azure policy propagation may take time.

![Publication impact review showing active and suspended membership and enforcement limitations](images/portal-publication-review.jpg)

*The capture shows the impact section. The gateway address and confirmation controls below it are omitted to keep deployment identifiers private. No policy was published while taking these screenshots.*

Published policy uses the gateway's managed identity to call Foundry. Review developers' direct Foundry RBAC, which can bypass gateway governance. In-flight requests may finish. If publication fails, the draft is retained; investigate the actual policy and visible error before retrying.

## Model access requests

A developer can choose **Request model access**, select a deployed model, and explain the need. Owners/administrators review and approve or decline the request. Approval creates a personal model grant; it takes effect at the gateway after publication.

<details>
<summary>See the request workspace and form</summary>

![Request workspace with no pending decisions in this preview](images/portal-requests.jpg)

![Model access request drawer with model selector and reason field](images/portal-request-drawer.jpg)

</details>

## Usage and analytics

Choose **Usage & analytics** for request counts, distinct callers, errors, duration, model summaries, and recent gateway requests. Select 7, 30, or 90 days, refresh, or export the view. The daily chart also has a table view.

![Request analytics with period selector, summary metrics, and daily activity](images/portal-usage.jpg)

This page shows operational metadata, not prompts or responses. Application Insights ingestion is delayed; recent request details are limited to 250 entries. Counts honor telemetry sampling weights. [Observability](observability.md) explains the underlying logs and token metrics.

## Cost analysis

Use **Cost analysis** to answer questions such as “Which model is driving token growth?” or “How has this team's estimated spend changed from the previous period?”

![Cost analysis with UTC date controls, estimated model spend, input/output usage, and rate coverage](images/portal-cost-analysis.jpg)

1. Choose **This month**, **7 days**, **30 days**, or **90 days**; open the date control or **Filters** for a custom UTC range, up to 93 days.
2. Combine model, group, and developer filters. Selections within one dimension are alternatives; separate dimensions narrow one another. Use the chips to remove individual filters.
3. Switch the trend between tokens and estimated USD. Enable **Compare** for the immediately preceding period of equal length, or open the daily table for exact values.
4. Group the breakdown by **Models**, **Groups**, or **Developers**. Search, sort, page through results, or select a row to drill down. Filters are reflected in the URL and browser history.
5. Export the selected analysis as CSV. Review attribution quality and any truncation notice alongside totals.

![Daily estimated cost compared with the immediately preceding period](images/portal-cost-trend.jpg)

<details>
<summary>Explore the filter drawer</summary>

![Cost filter drawer with UTC dates and searchable multi-select model choices](images/portal-cost-filters.jpg)

The drawer also contains group and developer selectors below the visible model list. Individual developer identities are excluded from this capture.

</details>

**Interpret the figures carefully.** Totals are aggregated in Application Insights; detailed breakdowns are capped at 2,500 dimensions across both periods and explicitly marked when truncated. Queries are cached for 60 seconds. Missing historical group/developer dimensions appear as unattributed; they are not assigned retroactively. Today's period can be incomplete.

Costs use the current input/output planning rates. Cache charges, some prompt-length tiers, discounts, tax, and infrastructure costs are not reconciled. Changing rates recalculates historical estimates. Rate coverage indicates that reported tokens have configured rates, not that all Azure charges are included.

## Budgets, token limits, and rates

### Enforced dollar budgets

When connected to the dedicated budget gateway, **Enforced spending limits** shows the published organization, group and personal limits alongside spent, reserved and available allowance. In this mode, **zero blocks new requests**; a blank personal limit inherits the group's allowance. The [budget architecture](budget-architecture.md) explains enforcement and the [validation record](budget-validation.md) lists outstanding release checks.

Choose **Refresh balance** beside the section heading to fetch current values. The button spins and temporarily disables while the existing table stays visible. The timestamp records the last successful read. If refresh fails, the table retains its previous values with an explicit warning; retry before relying on those figures. Policy publication is disabled while balances are refreshing or the last refresh failed. This display behavior does not cache budget approvals: the gateway still checks the live ledger for every admission.

Policy actions sit in a separate toolbar above the balance table. **Organization limit** and **Add verified model rate** edit drafts; **Publish budget policy** activates the reviewed changes. Group administrators see only their permitted team-publication action.

### Baseline planning and token limits

For the baseline gateway, distinguish the dollar planning threshold from gateway-enforced token/rate controls. A zero **baseline token limit** means no limit at that scope. Organization and group controls can still restrict a developer whose personal token limit is zero.

<details>
<summary>Edit organization budgets and limits</summary>

![Budget drawer separating dollar planning budget, monthly organization tokens, and per-developer token/request rates](images/portal-budget-drawer.jpg)

</details>

Publish baseline token/rate changes before expecting gateway enforcement. Streaming token estimates and concurrency can overshoot; separate gateway regions do not share an atomic global counter. Planning dollar thresholds do not stop inference or Azure billing.

Use **Set rates** or **Manage rates** to maintain input/output prices in USD per million tokens. The cost-analysis rate editor also records source, verification date, and notes. Choose prices appropriate to the actual deployment type, geography, and contract; the [dated pricing reference](../portal/PRICING.md) is a starting point, not automatic synchronization with Azure.

<details>
<summary>See the planning-rate editor</summary>

![Planning rates for the preview's Opus 5.5 US Data Zone deployment](images/portal-pricing.jpg)

This capture shows the budget workspace's rate editor. The example price applies to the preview's deployed SKU and must not be copied to a different deployment without review.

</details>

## Audit log

Use **Audit log** to search administrative changes, inspect event details, or export the current view. Events record the actor, timestamp, action, target, and before/after values, including drafts and publication activity.

![Audit action and target columns showing recorded model-price updates](images/portal-audit.jpg)

*Actor and timestamp columns are outside this crop. Events are real saved price changes.*

Displayed times use the browser's timezone; JSON details retain UTC. State and audit events share a versioned Azure Blob. This is a change history with recovery support, not an immutable compliance archive.

## Developer onboarding

Unified installations offer separate **Claude** and **Codex** applications, using published grants and the correct provider endpoint. The Codex setup contains only qualified OpenAI models. Owners can also download the model-policy draft for the deployment administrator to verify pinned runtime bounds before publication. See [unified setup and upgrades](unified-administration.md).

After granting and publishing access, open **Developer onboarding**, choose the group when offered, and select **Download Mac setup ZIP**. Developers download their own scoped setup. The page shows the target application, gateway address, and sign-in method from that profile.

![Complete setup ZIP, gateway address and copyable Codex setup prompt](images/portal-onboarding-setup.png)

Local development verification with synthetic organization data, October 25, 2026. Microsoft sign-in and live inference were not exercised in this local check.

Extract the ZIP and open `Install.command`, or use **Copy setup prompt for Codex** on the Mac being configured. The ZIP contains the scoped `client.json`, installer, gateway details in `README.md`, and `AGENTS.md` instructions for a coding assistant. Each person signs in with their own Microsoft work account. No repository checkout or administrator packaging step is needed.

The page's **Install on your Mac** instructions cover Apple's verification warning.
For this unsigned source/script distribution, **Open macOS Privacy & Security** is an
explicit, fixed OS-settings shortcut, not automatic approval. The user must approve
only their trusted `Install.command` using **Open Anyway**. Browsers may block the
shortcut; the manual settings path remains visible. Managed devices may prohibit
exceptions. Never disable Gatekeeper or strip quarantine; contact IT instead.
Downloading or approving the ZIP does not mean setup completed. Wait for **SETUP COMPLETE**.
An Apple-signed, notarized distribution requires an approved Apple Developer team and
is not available in this pilot. See [Mac security and installation](macos.md#apple-security-approval).

The **Advanced: connection profile only** section retains the JSON export for an existing installer or managed rollout. That file alone does not contain the installer or setup instructions.

Users follow the [Mac installation guide](macos.md). For MSAL without Azure CLI, the portal deployment must be configured with the separate desktop public-client ID. The portal does not push device policies or track installation completion; use your MDM platform for managed rollout.

### Experimental organization-link onboarding

An operator can opt into the separate [configuration-refresh pilot](client-configuration-refresh.md).
It replaces this page's group/profile export flow with one stable organization
link, the user's own Entra sign-in, and a choice of eligible applications.
The setup ZIP pins connection details, not model grants; future model selections
come from the active published policy. The screenshot above shows the existing
JSON flow, not the pilot.

The pilot requires explicit enrollment, which automatically starts background
refresh. Existing installations require explicit migration, and open apps may need
reopening to show new picker entries. The server-side pilot rollout remains
disabled by default and is not release-qualified:
encrypted credential storage, native-client behavior and recovery remain gates
before rollout. Gateway access and spending policy continue to be enforced
server-side even when local files are stale.

## Organization settings and navigation

**Organization** shows the portal display name, connected sign-in provider, gateway, and captured-data scope. Owners/administrators can change the display name in a drawer. Infrastructure endpoints and tenant connections are deployment configuration, not editable here.

<details>
<summary>See the organization editor</summary>

![Organization name editor](images/portal-organization.jpg)

</details>

Use **Search or jump to…** (`⌘K` on Mac) to find pages and people. The theme control supports system, light, and dark appearance. Forms use drawers with fixed actions; unsaved edits require a discard decision when closing. Conversation retention, device compliance, and invoices remain in their respective systems.

## When something goes wrong

For an expired session, sign in through your normal work browser. For a sign-in error, retain the correlation ID and timestamp for your identity administrator; repeatedly approving MFA is not a fix for a blocked authentication method. For missing usage, check the selected period, filters, ingestion delay, and monitoring permissions. For conflicting edits, refresh and review the latest revision before retrying.

See [troubleshooting](troubleshooting.md), [operations](operations.md), and the [portal engineering guide](../portal/README.md) for diagnostics, recovery, and deployment details.

## Prompt and response logs

Owners and administrators can use **Prompt & response logs** when the platform team has enabled capture. Filter by date/time, API, developer object ID, model, or request ID, then inspect a record in a drawer. An access reason is audited before its captured prompt and response are displayed. Billing, auditors, and developers cannot open this workspace. Capture is off by default and cannot recover earlier chats. See [setup, retention and permission boundaries](content-logs.md).

An owner can choose **No organization spending limit** in the organization spending drawer and **No group spending ceiling** in a group editor. Leave the group operating limit and personal limit blank to inherit an unlimited parent. Finite group and personal limits still apply under an unlimited organization. Spent and reserved amounts remain recorded, and returning to a finite cap below their sum is rejected at publication.
