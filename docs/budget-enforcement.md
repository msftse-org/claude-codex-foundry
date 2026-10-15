# Dollar enforcement and delegated administration

**Release blocker — design requirements, not implemented functionality.** Do not use the current portal's dollar fields as a spending stop. The current implementation has organization/group planning thresholds, no individual dollar limit, and no group-administrator role. Its Claude token quotas are not a concurrent dollar-spend ledger. Codex does not inherit those portal quota policies.

## Required hierarchy

The following is the proposed policy. The owner’s delegation ceiling and owner inference exemptions still require a product decision; the conservative defaults below grant no spending exemption.

| Role | Required authority |
| --- | --- |
| Organization owner | Manage the organization, administrators, group ceilings, model allowances, member limits, and content-access policy. All changes are audited. Administrative authority does not silently exempt inference requests from spending limits. |
| Group administrator | Manage developers within the assigned group, allocate individual budgets, adjust the group operating budget within the owner's ceiling, and grant models from the owner's delegated allowance. Inspect only that group's captured requests, with an audited reason. Cannot appoint organization administrators, move other groups' members, or increase their own delegation ceiling. |
| Developer | Use assigned models within all applicable limits; see their own remaining allowance and usage; manage their client setup and request additional access. No access to other developers' prompts or administration. |

Existing global administrator, billing and auditor roles require an explicit migration/compatibility policy. Do not silently turn an existing global administrator into a group administrator or grant content access to billing/auditor roles.

Group-scoped authorization must cover state, imports, member changes, access decisions, model catalogs, exports, telemetry, cost queries, audit records, and content searches/direct-ID reads. Frontend hiding is insufficient. Log scope uses the group recorded at request time; moving a member later must not expose the previous group's history. Records without trustworthy group attribution are unavailable to group administrators. Recheck authorization after a content query.

## Required spending semantics

Proposed default: USD, UTC calendar months. Each request consumes the **same** organization, group, and user allowance across Claude and Codex. All applicable limits must pass. For example, a developer with $100 left is blocked when their group has exhausted its allowance.

Distinguish a zero allowance (blocked) from an unset child limit (inherits its parents). Child limits are ceilings, not necessarily prepaid allocations; their sum may exceed the parent, but aggregate admitted spend cannot exceed it. Group administrators may allocate within a ceiling delegated by the owner. Budget reductions must account for both spent funds and outstanding reservations; moving a user must not reset that user's monthly spend or reassign past charges.

These limits cover priced, metered model inference through the governed gateway. They are not a cap on APIM, Functions, storage, logging, provisioned capacity, taxes, or the entire Azure invoice. Customer rates/discounts and currency must be explicit.

## Required enforcement architecture

1. Authenticate the caller and resolve current membership, model permission, pricing version, and all budget scopes on the server. Never accept a group, price, or remaining balance supplied by the client.
2. Before any billable backend dispatch, atomically reserve a defensible maximum cost against all applicable limits in a durable, shared ledger. Include in-flight requests in admission decisions. Use integer monetary units and idempotent request identifiers.
3. Bound the actual provider request consistently with the reservation. Account for input, output/reasoning, cache reads/writes, long-context tiers, service tiers and any paid server tools. Unknown pricing or unbounded billable features must be rejected, not treated as free. Token estimates alone are not a hard cost bound.
4. Settle against authoritative provider usage, including streamed completion usage. Settle once and return only the proven unused reservation. After a disconnect, timeout, process crash, ambiguous dispatch, missing usage, or settlement failure, retain the conservative reservation until reconciliation establishes the charge. Do not automatically refund uncertain requests on a timer.
5. If the ledger, authorization, or required pricing check is unavailable, reject inference. Do not fall back to an unmetered path. The administration website can remain separate, but this enforcement service becomes part of the inference availability boundary.
6. Scope and audit rate changes, budget changes, ownership changes, refunds and recovery. Do not recalculate previous charges with today's rates. A new calendar month must not clear unresolved liability or cause cross-month requests to be charged twice.

Application Insights is for reporting, not the admission ledger. APIM token policies do not supply atomic monetary accounting; Microsoft also documents concurrency/streaming limits in the [token-limit policy](https://learn.microsoft.com/en-us/azure/api-management/llm-token-limit-policy). A direct Foundry endpoint, baseline API, alternative credential or legacy client route must not bypass required customer enforcement. Existing demo access should not be revoked without a reviewed migration plan.

## Release acceptance

None of the following is satisfied by the existing token-quota tests:

- Prove organization, group and user exhaustion independently, for Claude and Codex, with both JSON and streaming responses. A refused request must not reach the billable backend.
- Race concurrent requests against a tiny remaining balance across multiple service instances; total committed spend plus reservations must remain within every applicable limit.
- Test duplicate delivery, provider retries, failed/ambiguous dispatch, missing usage, cancelled streams, settlement retries, process restart, ledger outage and month rollover without double charging or unsafe refunds.
- Test price changes, cache/long-context/reasoning categories, unsupported paid features, lowered ceilings and member moves. Reject unknown or incomplete pricing.
- Use two real groups and verify all group-admin routes, imports, grants, promotion attempts, exports, dashboards and direct request-ID reads cannot cross scope. Test owner-controlled delegation ceilings and access revocation during a query.
- Verify approved clients have no alternate ungoverned inference route. Measure latency and throughput under the intended load before claiming support for 2,000 developers.
- Present enforced spend, active reservations, remaining allowance, reset time and denied-request reason separately from estimated Azure invoice costs.

The feature branch remains a preview until an implementation and live evidence satisfy these checks. This document does not enable a hard cap or change anyone's permissions.
