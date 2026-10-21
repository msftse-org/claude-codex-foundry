# Enforced USD limits and delegated administration

**Feature branch: `codex/budget-hierarchy`. Unreleased until the live acceptance checks below pass.** The existing demo and baseline gateway routes do not gain spending enforcement by updating source code. Legacy `budget_usd` and `monthly_budget_usd` values remain planning amounts.

Start with [How a budgeted request travels through Azure](budget-architecture.md) for the service diagram, identity checks, storage map and request-by-request walkthrough. This page specifies the hierarchy, monetary semantics and release requirements.

The agreed hierarchy is **organization → group → developer**, in **USD per UTC calendar month**. Every model request must fit all three limits, including requests made by an organization owner. Child limits are ceilings, not prepaid allocations: their sum can exceed the parent, but aggregate admitted usage cannot.

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

- `settings.spend_limit_usd`: finite organization ceiling when enforcement is enabled.
- `group.spend_ceiling_usd`: owner-controlled group maximum. Required before publication.
- `group.spend_limit_usd`: operating allowance; blank inherits the ceiling, otherwise it cannot exceed the ceiling.
- `member.spend_limit_usd`: personal allowance; blank inherits the group allowance.
- **Zero means blocked**, unlike the legacy token fields where zero means no extra token quota. Suspending a member also blocks new requests after publication.

Each request reserves **the verified model input capacity plus its enforced output maximum**, priced at reviewed upper-bound token rates. Token-count estimates are deliberately not used to authorize a smaller reservation. As a result, a request may be refused while some money remains: the remaining allowance must cover the maximum request liability, including other active requests.

After complete provider usage arrives, the ledger charges at the same reviewed conservative rates and releases the difference. Anthropic cache reads/writes are included; OpenAI cached input and reasoning are already included in its input/output totals and are not double counted. Cached input may therefore consume more budget allowance than the eventual discounted invoice. The UI separates this ledger from planning estimates.

The owner must verify model version, input/output capacity, deployment SKU/geography and rates, including cache-write and long-context premiums. Verification expires after at most 31 days; missing or expired verification blocks new inference. Deployment upgrades require renewed verification and runtime configuration. Pin deployments and do not enable automatic upgrades for governed models.

Only synchronous, replayed conversation input and client-executed tools are supported in this preview. Unknown request fields, premium tiers, server tools, background jobs, opaque conversation references and server-managed compaction are rejected. Client function/custom tools may be grouped in an OpenAI namespace; nested server tools are still rejected. Claude Code’s exact thinking-preservation setting (`clear_thinking_20251015`, `keep: all`) is supported with the documented [context-management header](https://platform.claude.com/docs/en/build-with-claude/context-editing); it does not enable compaction. The gateway forces standard service tiers. Anthropic's [free token-counting endpoint](https://platform.claude.com/docs/en/build-with-claude/token-counting) requires active model access but does not debit an inference reservation. Its count is not a spending guarantee.

Limits cover supported model inference through the budget routes at the configured ceilings. They do **not** cap APIM, Functions, storage, Private Link, logging, taxes, exchange-rate changes, provisioned capacity or the entire Azure bill. Incorrect owner-entered rates/capacities invalidate the monetary bound; this is why review and pinned deployments are release prerequisites.

## Failure and recovery

No ledger, no valid price, no active membership, or no remaining allowance means no model dispatch. There is no fallback to a baseline API. Each gateway request ID is admitted once; uncertain dispatches are not automatically retried by the service.

A disconnect, missing terminal usage, process failure, provider error or settlement outage leaves the reservation held. **There is no timer-based refund.** Settlements are idempotent; a conflicting second charge is rejected. If reported usage exceeds the verified bound, admission pauses rather than silently treating the request as within budget. Publishing a new policy cannot clear that pause.

Owners can close an uncertain reservation at its **full reserved amount**, after an audited reason. This safely converts reserved allowance to spent allowance; it never refunds money. Releasing a held reservation for less than its maximum requires a future verified reconciliation workflow. Do not edit counters manually or restore an old ledger backup over a live ledger: both can erase liabilities. An overrun incident requires investigation before any operator-controlled recovery.

The current pending-reservation API returns at most 50 rows and signals additional rows. It is an operational preview, not a complete reconciliation product.

## Identity, permissions and bypass prevention

APIM validates the user's cognitive-services token, generates the request ID, and overwrites caller headers. The budget service accepts only an application token for its dedicated audience from the configured APIM managed identity, with the correct tenant and issuer. Direct user tokens and forged caller headers cannot invoke it. Only the runtime managed identity invokes Foundry.

Policy snapshots are content-addressed JSON Blobs. Their hash is checked on download, then their immutable bytes may be reused from a bounded local cache. The active digest and pause state are read fresh from the Table; admission rechecks the version and reserves all three scopes atomically. Cached documents never bypass current grants, price expiry or the ledger. Neither prices, group identity nor balances come from browser request headers. Portal role changes are checked server-side; group publication starts from the active snapshot and preserves owner ceilings, delegation, administrative roles, prices and all other teams. See [APIM, policy caching and Table consistency](budget-architecture.md#how-apim-caching-and-azure-table-work-together).

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

Until those checks are recorded, this remains an unreleased implementation rather than a customer-ready spending guarantee.
