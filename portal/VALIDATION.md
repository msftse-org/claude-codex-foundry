# Portal verification record

Status: **preview**. The portal, Codex and content-log sources are maintained in `main`. This record is evidence for the isolated Azure preview and for one authorized shared-route acceptance, not a production certification or a 2,000-user capacity guarantee. Customer configuration, raw logs, downloaded profiles, and identity details remain outside Git.

## Verification recorded on 2026-10-23: one shared Claude route

[PR #7](https://github.com/msftse-org/claude-codex-foundry/pull/7) makes the existing Claude API the default target for clients, portal reporting, profile export and governance publication. An isolated API remains an explicit testing option. The rollout below was performed in one authorized environment; backups, configuration, telemetry and recovery snapshots remain private.

| Check | Evidence |
| --- | --- |
| Isolated destructive tests | Before the shared route changed, `verify_portal_preview.py --all-models` passed all 18 checks: authentication rejection, catalog, inference, token counting, streaming, denied model, suspended member, organization/group/member token quotas and member token/request rates. Both original preview policies were restored. |
| Retargeted deployment | ARM what-if was reviewed. The retargeted configuration and locally built Functions package were deployed with the existing resource names, identity, private storage and networking. No resource replacement was required. |
| Before publication | Sign-in, preserved member/group state, a canonical-route profile export and historical dashboard traffic were verified. `claude-foundry verify --all-models` passed against the baseline policy. |
| Publication | After explicit approval and impact review, the existing access policy was published to the canonical API. Publication/audit state, client JWT validation and APIM managed-identity backend authentication were confirmed. |
| Governed verification | `claude-foundry verify --all-models --governed` passed after publication and again after preview removal: missing/invalid tokens rejected; catalog, inference, streaming, token counting and the governed model allowlist passed. |
| Installed client | Claude Code, started from an empty temporary directory, returned its expected marker in a text-only test and in a tool-enabled test that ran only `pwd`. |
| Reporting | The authenticated portal displayed canonical-route requests, including the client tests. New governed token metrics carried member/group attribution; historical metrics remain unattributed. |
| Preview cleanup | After confirming no preview traffic, only the obsolete preview API and its API-scoped writer assignment were removed. Exactly one Claude API remains, and the portal policy-edit role targets only it. |
| Deployed security checks | Portal health returned 200 and anonymous state 401. Storage public networking and shared keys are disabled, with an approved private endpoint, Blob versioning and 30-day deletion protection. |
| Source checks | 269 Python tests, 45 frontend tests, Ruff, the frontend production build and all five Bicep templates pass. |

This is shared-route acceptance in one environment, not production certification, cross-tenant testing or load testing. Analytics remain subject to Application Insights ingestion and caching delays, and streaming tokens are estimates. Direct Foundry RBAC was reviewed, not revoked, and still bypasses gateway enforcement. Historical member/group attribution is not backfilled. Optional Codex, dollar-budget and content-log deployments were not redeployed or recertified by this rollout.

## Verification on 2026-10-15: restricted content logs

The `feat/codex-azure` preview adds native APIM message capture for Claude and Codex. Capture remains opt-in in the template; it was explicitly enabled on the isolated preview Claude API and the Codex API for this verification. Existing demo resources were not renamed or migrated.

| Check | Evidence |
| --- | --- |
| Native capture | Synthetic Claude Messages and OpenAI Responses requests returned HTTP 200 in both JSON and streaming modes. All four had correlated prompt and response records in `ApiManagementGatewayLlmLog`. Gateway traces supplied the authenticated developer object ID. |
| Hosted content view | The existing signed-in Edge owner session opened both streaming captures through the hosted portal's managed identity. Prompt, response and raw-record controls worked; the access reason was required before opening. No new sign-in was needed. Combined API/model/developer filters returned three expected Codex records; an unknown model showed the empty state. |
| Access audit | Hosted `content.view_requested` events contained the actor, UTC time, correlation ID and reason, without message bodies. Automated tests prove audit-storage failure prevents the content query. |
| Query boundaries | API/resource allowlists, retention clamping, escaped model filters, stable pagination, invalid inputs and fragment limits tested. Model filtering happens after correlation so native fragments without model fields are retained. |
| Authorization | All five portal roles tested with signed-token authentication. Only owners and administrators can configure-view, search or open content. Revocation during a query denies its result. Real customer administrator/billing/auditor/developer sessions remain acceptance work. |
| Azure configuration | Dedicated content workspace; resource-only access disabled. Both native message and gateway correlation tables report seven-day Analytics and total retention. Portal managed identity has Log Analytics Reader scoped to this workspace. Conventional HTTP body capture is zero; headers are unselected and credentials masked. |
| Build and tests | **143 Python tests and 14 frontend tests pass**; Ruff, strict TypeScript/Vite build and all three Bicep templates pass. |

A seven-day table setting is not proof of immediate physical erasure: Azure retention/deletion behavior still applies. Direct Azure workspace readers can bypass portal access auditing. The state audit is mutable/versioned, not immutable. No sustained customer-scale throughput or logging-cost claim is made. See [content logging](../docs/content-logs.md) for deployment, stop-capture instructions and limitations.

The same day's README routing investigation **did not validate the Azure Desktop UI path**: the three existing-conversation turns used the normal OpenAI provider, with no matching APIM requests. A separate controlled Responses request did appear in APIM. See [Codex routing evidence](../docs/codex-validation.md).

## Verification on 2026-10-14

The `fix/portal-validation-security` review checked every implemented portal feature area at the API/domain layer and exercised the hosted owner interface. The hosted app uses Entra authentication, managed identity, private Blob storage, and the separate preview APIM API. Local owner bypass was **not** used as authentication evidence.

| Feature area | Automated verification | Hosted / live evidence |
| --- | --- | --- |
| Sign-in and access control | RSA-signed token validation; invalid signature, issuer, tenant, audience, client, scope, expiry, not-before, object ID and malformed claim types; spoofed identity header rejected | Existing owner session reused without another login. Every private route rejects anonymous requests. Public bootstrap/health work. |
| Roles and permissions | All five roles traverse state, impact, telemetry, client export, import preview, member/group/settings/rate writes, requests, decisions and publication. Denied writes leave state unchanged. | Owner journey verified. Other real Entra role sessions are still required. |
| Members | Ownership protection, self-lockout prevention, suspension, unknown model/group rejection, stale revisions | Existing owner's member draft saved without changing access. |
| CSV import | 2,000-member import/policy-size check; malformed rows, duplicate IDs, unknown groups, role elevation and partial-write rejection | Form/template controls inspected. Browser file upload blocked by Edge extension file-URL access setting; no directory access was created for testing. |
| Groups/model grants | Group/default validation, effective grants, filtered catalog, denied model/member checks, scale of generated policy | Existing group saved; model/default controls inspected. Nine models shown. Live filtered catalog and suspension tests pass. |
| Budgets/rates | Billing can change dollar threshold/rates but not token controls; sourced rate metadata validated; estimates and cache invalidation tested | Budget changed and restored. Shared rate drawer saved a sourced rate without erasing its source/date/notes. Nine sourced rates remain configured. |
| Access requests | All roles can request; pending duplicates rejected; approval grants and decline behavior; decisions audited and cannot repeat | Synthetic validation request submitted and declined. No new effective model access was granted. |
| Publication | Reviewed revision/name required; active publication blocks edits/duplicate publish; failed draft retained; all attempted policy writes restored after uncertain failures; rollback continues if one restore fails | **Hosted managed identity successfully published both preview policies.** Audit records show started/published. Catalog, streaming and model denial passed afterward. |
| Quotas/rates | Generated policy covers organization/group/member monthly token scopes and per-member token/request rates | Each monthly scope returned the expected 403 quota response; token and request rate limits returned 429. |
| Overview/usage | Reporting periods, API scope, aggregation, partial-result rejection | Dashboard, live managed-identity telemetry, 7-day selection, table view and request CSV export work. Errors include deliberate rejection tests. |
| Cost analysis | Date bounds, JSON-escaped KQL filters, role enforcement, query/rate cache behavior, previous-period math, truncation notices and CSV safety | Combined model/developer filters returned one model; 56 input at $2/M plus 20 output at $10/M equaled $0.000312. CSV downloaded; browser Back restored unfiltered results. Earlier group/date/table/mobile checks remain recorded below. |
| Audit | Successful/failed changes preserve actor, before/after values and revision; denied writes do not mutate state | Hosted changes and publication visible; event JSON and audit CSV downloaded. History remains mutable/versioned, not immutable. |
| Onboarding | Own/group profile authorization, no-access/unknown-group handling and family-default tests | Downloaded profile validates: nine models, correct Sonnet/Haiku/Opus defaults, no credentials. This preview currently exports Azure CLI mode because no separate desktop client ID is configured. A fresh installation from this portal export is not yet verified. |
| Organization/navigation | Settings validation; frontend authentication, CSV, estimate and query-state tests | Organization name changed and restored; all navigation pages, drawers and role reference inspected. |
| HTTP/browser boundary | Actual received body bytes limited before JSON parsing, malformed/mismatched lengths rejected, protection headers on rejection; concurrent role revocation cannot authorize against newer state | Hosted oversized body returns 413, anonymous API returns 401, CSP/no-store/HSTS present, and cross-origin preflight does not grant access. |

### Results and fixes

- **88 Python tests and 12 frontend tests pass**; strict TypeScript production build, Ruff, Bicep compilation and package build pass.
- **26 live preview gateway checks pass**, including small inference calls to all nine published models, streaming, token counting, authentication failures, model denial, suspension, three quota scopes and two rate limits. The test restores both original preview policies; the hosted policy pair was restored and matched its backup exactly afterward. DNS/connection-refusal failures are retried only before HTTP delivery; ambiguous HTTP timeouts are not retried. Each quota/rate scope is separated by a verified successful request with all limits disabled, so a delayed rejection from the previous policy cannot count as a pass.
- After the hosted publication, catalog, streaming inference and denied-model checks passed again. A later local DNS failure interrupted a stricter test and its automatic restore; the hosted owner successfully republished the reviewed policy. The test now retains a private policy backup and explicitly reports any restore failure. The production API policy matched its pre-test hash exactly.
- Fixed a rate editor that discarded provenance, stale authorization across state reads, a body-size check that trusted only Content-Length, malformed claim-type errors, duplicate open access requests, and incomplete recovery attempts after uncertain Azure writes. The dashboard now distinguishes portal conversation storage from independently configured Azure diagnostics. Usage loading is explicit, stale downloads are disabled, and an older reporting request cannot overwrite the latest selected period.
- Dependency audit: 33 pinned Python runtime packages and the npm production dependency tree report **no known vulnerabilities** as of this run. The npm mirror's advisory endpoint failed; the npm registry audit succeeded. Python auditing used the complete pinned runtime export with dependency resolution disabled, with no skipped packages. This is an advisory check, not proof that dependencies contain no vulnerabilities.

### Deployed Azure security checks

- Entra registration: single tenant, v2 tokens, exact hosted SPA redirect, no client secrets/certificates, only its own delegated API scope. This preview also has localhost development redirects; customer production registrations should not retain them.
- Runtime role assignments: custom policy editor scoped to one preview API; Monitoring Reader/Metrics Publisher on the selected Insights component; Storage Blob Data Owner on the dedicated storage account. The [identity reference](../docs/identity-and-permissions.md) documents the authority these roles actually provide.
- Function: HTTPS only; TLS and SCM TLS 1.2; FTP disabled; FTP/SCM basic publishing credentials disabled; VNet integration enabled. The portal endpoint remains public and authenticates in application code.
- Storage: public network disabled, shared keys disabled, public blobs disabled, HTTPS/TLS 1.2, Blob versioning and 30-day Blob/container soft deletion.
- Foundry local-key authentication disabled. Direct Foundry access by separately authorized identities remains a bypass boundary to review.
- Preview and Application Insights service diagnostics: zero body capture, no captured headers, query parameters hidden, credential-header masking. Existing `azuremonitor` service diagnostics retain client-IP logging. They were inspected and preserved.

## Earlier UI evidence retained

The walkthrough's screenshots were captured from the actual hosted preview on 2026-10-14, with pointers and customer identifiers excluded. Earlier checks covered group/model filtering, custom UTC dates, trend/daily tables, unsaved-form discard, command search with Enter navigation, focus restoration, and a 390-pixel light/dark layout. See the [portal walkthrough](../docs/portal-guide.md) and [capture notes](../docs/images/README.md). Those screenshots illustrate features; the table above is the current test record.

## Remaining customer acceptance work

- **Required spending control is not implemented:** hard organization/group/user dollar limits, shared Claude/Codex accounting, and scoped group-administrator permissions. The existing token-quota tests do not validate these requirements. See the [release-blocking acceptance criteria](../docs/budget-enforcement.md).

- Real administrator, billing, auditor and developer sessions under the customer's Conditional Access, consent and device policies; account disablement/expired-token journeys.
- Shared-route migration in each customer environment: back up both policy surfaces and portal state, review all intended callers and direct Foundry RBAC, verify reporting before publication, and run `claude-foundry verify --governed` afterward.
- Complete the hosted browser CSV upload once extension file access is available, and a fresh Mac installation from the exported portal profile. Verify MSAL mode with a separate desktop registration if Azure CLI-free onboarding is required.
- Process termination during publication, live two-session administrator conflicts, rollback failures, and a real Blob-version restore from an authorized private-network operator. Offline failure/concurrency tests do not replace these operational drills.
- Sustained load, latency and cost measurement for the intended user count; state/audit growth, log retention, alerts, and recovery objectives. A 2,000-member configuration test does not establish 2,000 concurrent developer throughput.
- Full keyboard/screen-reader assessment and independent penetration testing if required by the customer's security process. The portal has no distributed administration API rate limiter or immutable audit sink.

Source merges support continued development. Production rollout remains a separate decision based on this evidence, the customer's threat model, direct Foundry permissions, and the [operations acceptance checklist](../docs/operations.md).
