# Hosted group-administrator acceptance test

This is a release gate for the budget hierarchy preview. Automated role tests do not replace independent Entra sessions. Keep account names, directory IDs, tokens and captured conversations out of public validation reports.

## Prepare an isolated evaluation

1. Connect a separate [budget portal](../budget/README.md#configure-and-publish-policy) to the evaluation ledger. Keep its content-log API allowlist restricted to the evaluation routes.
2. The owner creates two portal groups, A and B. Give each a small finite ceiling and operating limit, delegate only reviewed models, and add one existing resource-tenant Entra identity as `group_admin` in each group. Give the administrators finite personal limits as well.
3. Add two consenting developer test identities, one in each group, when testing membership changes and per-user allowances. Do not use unrelated employees as synthetic fixtures.
4. Review and publish the budget policy as the owner. Check the active hash and balances. A saved member role changes portal access immediately; inference permissions and budgets change only after publication.
5. Each tester opens the portal in their own browser profile and signs in themselves. The operator must not impersonate them, fabricate tokens, reuse another person's session, or disable Conditional Access. Group administrators need portal membership, not Azure RBAC on storage, APIM or Foundry.

## Test both directions

Repeat every row with A and B swapped. Record the result and correlation ID privately. Mark a row passed only after observing the actual hosted response.

| Test | Required result |
| --- | --- |
| Sign-in and state | Each administrator sees their own group and members; no other team's identities, audit events, requests, balances or organization enforcement prices. |
| Delegated models | Enable/disable a delegated model for their team, publish it, and confirm the resulting gateway catalog. A model outside the owner's delegation is rejected. |
| Group operating limit | Lower and restore the operating limit within the owner's ceiling. Raising the ceiling or exceeding it is rejected. |
| Developer allowance | Change an authorized developer's personal limit and publish. A request exceeding any remaining organization/group/user allowance is refused before inference. |
| Administrative boundaries | Owner promotion, another administrator's role change, moving an existing user between groups, organization-limit edits and price changes are rejected. |
| Cross-team mutations | Submit the other group's ID or member ID to the hosted change/import endpoints using the tester's own session. Expect 403 and no changed state. |
| Scoped publication | Team A publishes while unrelated owner/team B drafts exist. Only A's allowed changes become active; owner ceilings, prices and B's active configuration remain unchanged. |
| Content search | Generate a harmless unique marker through each evaluation gateway. Each administrator sees only their team's request-time-attributed captures. Unattributed historic records remain hidden. |
| Direct content ID | Attempt to open B's correlation ID as A, and vice versa. Expect 404 without content. Every detail-read intent is audited. |
| Scoped costs and balances | Cross-team filters cannot widen scope. Direct balance queries for another team's member return 403. Missing telemetry is not a passing result. |
| Revocation | Owner suspends or changes a tester's role/group. The next portal request uses current authorization. Publish the change and verify inference is blocked or reassigned accordingly. |
| In-flight content revocation | Revoke access while a content query is outstanding. The response must not disclose content after the final role/group check. |

Some negative cases require an HTTP test client using the tester's own legitimately obtained portal token. Browser visibility alone does not prove server authorization. Never put a token in a URL, screenshot, shared command, test fixture, or Git.

## Record and clean up

Record portal build/commit, active policy hash, test time, identity aliases, expected and observed status codes, and ledger balances. Separate owner-only checks from independent group-admin checks. Keep raw prompts and responses in the approved private log workspace, not the public repository.

After testing, the owner suspends temporary memberships and publishes the change. Retain the audit and ledger evidence; do not reset counters or refund uncertain reservations as cleanup. Remove temporary app redirects only when their associated preview is retired.

A signed-in owner journey, two configured names, or synthetic JWT unit tests are **not** proof that the two group-admin journeys passed. Keep incomplete rows pending in [the validation record](budget-validation.md).
