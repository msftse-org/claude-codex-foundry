# Enforced USD limits and delegated administration

**Evaluation feature, included in `main`. Customer release requires the live acceptance checks below.** The existing demo and baseline gateway routes do not gain spending enforcement by updating source code. Legacy `budget_usd` and `monthly_budget_usd` values remain planning amounts.

Start with [How a budgeted request travels through Azure](budget-architecture.md) for the service diagram, identity checks, storage map and request-by-request walkthrough. This page specifies the hierarchy, monetary semantics and release requirements.

The agreed hierarchy is **organization → group → developer**, in **USD per UTC calendar month**. Every model request must fit all configured finite limits, including requests made by an organization owner. Only the owner can explicitly remove the organization ceiling or a group ceiling. Blank values without that explicit choice remain unconfigured and block publication. Accounting and access checks continue for unlimited scopes. Child limits are ceilings, not prepaid allocations: their sum can exceed the parent, but aggregate admitted usage cannot.

| Role | Authority |
| --- | --- |
| Organization owner | Set the organization limit, each group's maximum and delegated model allowance; appoint administrators; review enforcement prices; publish organization policy; view captured content with an audited reason. No inference exemption. |
| Group administrator | Manage developers in their assigned group, allowed models within the owner's allowance, the group operating limit, and personal limits. Publish only those team edits. See their group's costs, audit events and attributed prompt/response logs. Cannot raise the group's maximum, promote administrators, move existing members between teams, publish owner-controlled changes, or access another team. |
| Developer | Use assigned models, see personal allowance and held reservations, download setup and request access. No access to other users' data. |
| Existing global administrator | Retains organization-wide administration and content access. Can draft operating/user limits, but cannot change owner-controlled ceilings, enforcement prices or organization enforcement mode. Organization budget activation requires the owner. |
| Billing / auditor | Retain their existing planning/read permissions. Neither can view prompt/response content or change enforced dollar limits. |

Existing global administrators are not silently converted to group administrators. A group is a portal assignment, not an automatically synchronized Entra group.

## How a request is admitted

```mermaid
sequenceDiagram
  participant Client as Claude / Codex
  participant APIM as Standard APIM
  participant Budget as Streaming budget service
  participant Ledger as Private Azure Table
  participant Foundry as Azure Foundry
  Client->>APIM: Entra user access token
  APIM->>APIM: Validate tenant and audience; overwrite identity headers
  APIM->>Budget: APIM managed identity token + caller object ID
  Budget->>Budget: Verify APIM identity, membership, model, price and capacity
  Budget->>Ledger: Atomic organization + group + user reservation
  alt Insufficient allowance or unavailable ledger
    Ledger-->>Budget: Reject
    Budget-->>Client: Budget refusal; no inference dispatch
  else All three limits pass
    Ledger-->>Budget: Committed reservation
    Budget->>Foundry: One bounded model request using managed identity
    Foundry-->>Client: Response streamed through budget service and APIM
    Budget->>Ledger: Settle once from authoritative terminal usage
  end
```

The budget service is a separate Python Functions Flex Consumption app using native HTTP streaming. It is an availability dependency for governed inference. The admin portal remains separate. [Azure's HTTP-streaming requirements](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-http-webhook-trigger) require `PYTHON_ENABLE_INIT_INDEXING=1`; do not replace the streaming host with `AsgiFunctionApp`.

The ledger uses integer micro-USD (one millionth of a dollar). Organization, group, user and reservation rows share one organization partition and commit in one conditional Azure Table transaction. Admissions also update the active-policy ETag, so a concurrent policy change cannot authorize against a stale snapshot. This relies on [Azure Table atomic transactions](https://learn.microsoft.com/en-us/python/api/azure-data-tables/azure.data.tables.tableclient#azure-data-tables-tableclient-submit-transaction), not delayed monitoring data or APIM's estimated token counters.

Both Anthropic Messages and OpenAI Responses debit the same counters. A group move does not reset the user's monthly spend. Settlement stays in the month/group recorded at admission, even when a stream crosses midnight or membership changes afterward. A limit reduction below spent plus reserved is rejected.

## Monetary semantics and conservative reservations

- `settings.spend_limit_usd`: finite organization ceiling when enforcement is enabled, or `null` with owner-controlled `settings.spend_limit_unlimited: true`.
- `group.spend_ceiling_usd`: owner-controlled group maximum, or `null` with owner-controlled `group.spend_ceiling_unlimited: true`. An explicit choice is required before publication.
- `group.spend_limit_usd`: operating allowance; blank inherits the ceiling, otherwise it cannot exceed the ceiling.
- `member.spend_limit_usd`: personal allowance; blank inherits the group allowance.
- **Zero means blocked**, unlike the legacy token fields where zero means no extra token quota. Suspending a member also blocks new requests after publication.

Each request reserves **the verified model input capacity plus its enforced output maximum**, priced at reviewed upper-bound token rates. Token-count estimates are deliberately not used to authorize a smaller reservation. As a result, a request may be refused while some money remains: the remaining allowance must cover the maximum request liability, including other active requests.

After complete provider usage arrives, the ledger charges at the same reviewed conservative rates and releases the difference. Anthropic cache reads/writes are included; OpenAI cached input and reasoning are already included in its input/output totals and are not double counted. Cached input may therefore consume more budget allowance than the eventual discounted invoice. The UI separates this ledger from planning estimates.

The owner must verify model version, input/output capacity, deployment SKU/geography and rates, including cache-write and long-context premiums. Verification expires after at most 31 days; missing or expired verification blocks new inference. Deployment upgrades require renewed verification and runtime configuration. Pin deployments and do not enable automatic upgrades for governed models.

Only synchronous, replayed conversation input and client-executed tools are supported in this preview. Explicit Codex `compaction_trigger` requests use at least 20,000 output tokens within reviewed model bounds; automatic server-managed context management remains outside this supported subset. Unknown request fields, premium tiers, server tools, background jobs, opaque conversation references and server-managed compaction are rejected. Client function/custom tools may be grouped in an OpenAI namespace; nested server tools are still rejected. Claude Code’s exact thinking-preservation setting (`clear_thinking_20251015`, `keep: all`) is supported with the documented [context-management header](https://platform.claude.com/docs/en/build-with-claude/context-editing); it does not enable compaction. The gateway forces standard service tiers. Anthropic's [free token-counting endpoint](https://platform.claude.com/docs/en/build-with-claude/token-counting) requires active model access but does not debit an inference reservation. Its count is not a spending guarantee.

Limits cover supported model inference through the budget routes at the configured ceilings. They do **not** cap APIM, Functions, storage, Private Link, logging, taxes, exchange-rate changes, provisioned capacity or the entire Azure bill. Incorrect owner-entered rates/capacities invalidate the monetary bound; this is why review and pinned deployments are release prerequisites.

## Failure and recovery

No ledger, no valid price, no active membership, or no remaining allowance means no model dispatch. There is no fallback to a baseline API. Each gateway request ID is admitted once; uncertain dispatches are not automatically retried by the service.

A hold is the request's **worst-case cap, not a charge**. Normal settlement replaces it with the recorded usage charge and releases the unused allowance. A disconnect, uncertain dispatch, missing terminal usage, unreadable usage, possibly billable rejection or settlement outage can leave the cap held. **There is no timer-based refund.** Held amounts count only toward the UTC calendar month in which they were reserved, including earlier months.

The Budgets page shows **Held, not charged** totals and counts by month, followed by each request's start time, age, month, model, member, held amount and cause. Owners, administrators, billing administrators and auditors retain the organization-wide reservation scope; group administrators see their request-time group, and developers see only their own holds. The newest requests appear first, with stable request-ID ordering for equal or unrecorded start times. Paging does not change the monthly totals: they cover the whole scoped read, not only the current page. The API defaults to 50 rows per page and accepts `limit` up to 200 with a returned `next_cursor`.

Each read is limited to 10,000 pending rows, filtered by scope before reading and sorted in memory afterward. At that bound, `read_limit_reached` is true and the page warns that **both rows and totals may be incomplete**. It does not read a 10,001st row to determine whether more exist. A bounded result is not proof that all reservations, or the newest ones outside that read, have been shown.

When recorded by the runtime, `hold_reason` and the UTC ISO 8601 `held_at` timestamp describe why the request stayed reserved. These fields are written only while the request is reserved. Older holds without them show **Not recorded**; gateway telemetry does not fill in missing hold metadata.

| Hold reason | Meaning |
| --- | --- |
| `dispatch_unknown` | The request to Azure failed before any response. |
| `client_disconnected` | The client went away before the stream's terminal event. |
| `stream_incomplete` | The upstream stream ended without trustworthy terminal usage. |
| `rejected_possibly_billed` | Azure returned a non-success status outside the unbilled allowlist. |
| `usage_unreadable` | A success response's usage could not be read or priced. |
| `settlement_failed` | Usage was read but the ledger write failed. |

The page also makes one read-only Application Insights query for the listed request IDs, restricted to the deployment's API Management role. A 429 is **Throttled, not billed**; 401, 403 and 404 are **Rejected, not billed**. A 400 is **Rejected, may be billed**. A 200 marked “not sent in full” is **Client disconnected**, while an ordinary 200 is **Completed, not settled**. A missing request is **No gateway record**; retention or ingestion delay may explain it, but missing telemetry never proves that a request was unbilled. If monitoring is unconfigured or unavailable, holds and totals still load, with an unavailable-outcome notice.

An **owner only** can choose **Release (not billed)** and give an audited reason of 5–300 characters. The server independently re-reads the reserved row and re-queries Insights; it requires one unambiguous record for the exact request ID and deployment role, with a timestamp and **exactly 401, 403, 404 or 429**. It never trusts a displayed status or client-supplied evidence. **Never release based on 400, 413, 415 or 422**: without the response body a billable content-filter rejection cannot be ruled out. A 200, disconnect, duplicate/ambiguous record, unavailable outcome or already-settled request cannot authorize release either. Investigating completed-but-held requests is a separate runtime/log follow-up, not permission to refund them.

The server saves the `budget.release_unbilled` audit event, including the reason, gateway status, gateway timestamp and amount, using the existing portal-state ETag before calling zero settlement. Settlement uses the existing atomic ledger ETags: that hold's original-month organization, group and user `reserved` counters each drop by exactly the hold, and their `spent` counters remain unchanged. Other months are unaffected. This is allowance reconciliation, not an Azure billing refund. The portal reloads after a successful release or conservative closure so its state revision, hold list and allowance balances refresh together.

An audit-save failure prevents settlement. A conflict or outage after the audit was saved can leave an audit entry for an attempted release without a completed settlement: reload the hold and verify its counters before retrying. The ledger does not apply the reduction twice, and a competing nonzero settlement is never overwritten by a refund.

Owners retain **Close at maximum** for holds without unbilled proof. After an audited reason, this records the **full reserved amount as spent**; it never refunds money. Settlements are idempotent and a conflicting second charge is rejected. If reported usage exceeds the verified bound, admission pauses rather than silently treating the request as within budget, and publishing a new policy cannot clear that pause. An overrun incident requires investigation before operator-controlled recovery. Do not edit counters manually or restore an old ledger backup over a live ledger: both can erase liabilities.

## Identity, permissions and bypass prevention

APIM validates the user's cognitive-services token, generates the request ID, and overwrites caller headers. The budget service accepts only an application token for its dedicated audience from the configured APIM managed identity, with the correct tenant and issuer. Direct user tokens and forged caller headers cannot invoke it. Only the runtime managed identity invokes Foundry.

Policy snapshots are content-addressed JSON Blobs. Their hash is checked on download, then their immutable bytes may be reused from a bounded local cache and an optional machine-only APIM value cache. The active digest and pause state are read fresh from the Table; admission rechecks the version and reserves all three scopes atomically. Cached documents never bypass current grants, price expiry or the ledger. Neither prices, group identity nor balances come from browser request headers. Portal role changes are checked server-side; group publication starts from the active snapshot and preserves owner ceilings, delegation, administrative roles, prices and all other teams. See [APIM, policy caching and Table consistency](budget-architecture.md#how-apim-caching-and-azure-table-work-together).

Raw log scope uses the trusted **request-time group ID** emitted by the budget gateway, not current membership. Moving a user cannot expose their previous team's history. Unattributed older records are excluded from group-admin queries. Every content detail read requires a reason, an audit write before querying, and a role/group recheck afterward. Seven-day opt-in content retention remains a separate deployment.

The isolated budget portal template uses a dedicated managed identity with storage and monitoring permissions; it grants no APIM policy-write or Foundry inference role. On an empty portal store it imports the active ledger's policy only if its configured owner is already an active owner there. Existing drafts and audit history survive redeployment. Portal membership, role and suspension changes take effect on the next portal request; inference access and limits require budget publication.

Azure operators with write access to runtime settings, deployment code, role assignments or ledger storage are inside the trust boundary. The portal audit is versioned organizational state, not an immutable compliance certification.

For a customer rollout, remove users' direct Foundry inference roles and do not distribute baseline API routes or alternate model credentials. Keep Foundry local keys disabled. Azure control-plane administrators can deliberately change these controls; a portal budget cannot constrain a subscription owner. The existing demonstration environment deliberately retains baseline access until its migration is reviewed.

## Deploy an isolated evaluation

See [the budget runtime deployment guide](../budget/README.md). It adds two APIs with distinct paths; the baseline and existing portal APIs are preserved. The hosted admin preview does not automatically activate these resources. When a portal is connected to the budget ledger, its legacy APIM publication endpoint is disabled so it cannot replace the budget route with a token-only policy.

## Release acceptance

Read the [dated validation record](budget-validation.md) for completed Azure checks and their limits.

Local verification covers atomic concurrency, all three limits including owner requests, duplicate admission/settlement, ambiguous commits, month rollover, user moves, lower-limit rejection, price expiry, unsupported paid features, terminal stream usage, gateway identity validation and group authorization.

Before customer release, record live evidence for:

- Both providers, JSON and streaming; independent organization/group/user exhaustion; no model dependency after refusal.
- Multiple runtime instances racing against a tiny balance, with Azure Table state proving no overdraw.
- Disconnects, restart, ambiguous provider outcomes and ledger outage without unsafe refunds.
- Current native Claude and Codex clients, including long-context/tool sessions and unsupported-feature messages.
- Two real group-admin accounts across every portal route, especially direct content IDs and revocation during reads. Use the [hosted permission-test procedure](budget-permission-test.md).
- Every offered model's actual price categories, capacity, pinned version and deployment configuration.
- Removal of alternate inference paths in the customer's installation.
- Latency, contention, throughput and failure recovery under the intended organization load. A 2,000-member policy check is **not** a 2,000-concurrent-user capacity claim.

Until those checks are recorded, this remains an evaluation implementation rather than a customer-ready spending guarantee.
