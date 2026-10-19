# Budget hierarchy validation

**Unreleased feature branch: `codex/budget-hierarchy`.** This record covers an isolated budget runtime, a separate budget-connected portal and new standard APIM APIs. The existing demo portal, baseline endpoints and workstation profiles have not been migrated to these controls.

## Live Azure evidence

| Check | Result |
| --- | --- |
| Backend authentication | Anonymous calls and a valid user Cognitive Services token were rejected with 401. Only the configured APIM managed identity can call the runtime. |
| APIM authentication | Anonymous requests were rejected with 401. Authenticated catalog and balance requests returned 200. |
| Forged identity headers | Client-supplied principal/group/request headers did not change the authenticated account or its ledger scope. |
| Anthropic Messages | Claude Haiku 4.5 JSON and streaming requests returned 200; each reported 11 input / 5 output tokens. |
| OpenAI Responses | GPT-4.1 nano JSON and streaming requests returned 200; each reported 11 input / 2 output tokens. |
| Shared settlement | Those four requests charged a combined 98 micro-USD at the reviewed conservative rates to organization, group and user. All reservations settled; none remained held. This is ledger accounting, not invoice verification. |
| Organization exhaustion | Eight concurrent requests against a $0.105 organization cap: one admission, seven 403 budget refusals. |
| Group exhaustion | The same test with only the group cap restrictive: one admission, seven 403 budget refusals. |
| User exhaustion | The same test with only the personal cap restrictive: one admission, seven 403 budget refusals. The caller was the organization owner. |
| Uncertain provider outcome | The admitted exhaustion-test request deliberately used invalid provider input and received 400. Its entire 104,784-micro-USD reservation remained held in every counter; no automatic refund occurred. |
| Invalid/unpriced features | Unpriced model returned 403; a server tool and an opaque conversation reference returned 422 before reservation. Balances did not change. |
| Claude token counting | The authenticated token-count endpoint returned 200 without changing the inference balance. |
| Native Claude Code 2.1.296 | An isolated `--bare` session read a synthetic README with the Read tool and returned its marker, in two turns. No file changes; complete usage settled. |
| Native Codex CLI 0.145.0 | An ephemeral read-only session used the shell to read a synthetic README and returned its marker. It completed with no errors and no file changes. |
| Native message capture | Log Analytics contained request and response fragments for successful calls on both budget APIs, with trusted `budget-governance` group attribution. Raw content was not copied into this public record. |

Each exhaustion scenario used a fresh, isolated ledger partition with the same reviewed price policy. All evidence partitions were retained. The original evaluation ledger was restored afterward and its one-time bootstrap setting was disabled. The admitted request's maximum reservation was 1,047,576 input tokens plus 64 output tokens at the reviewed GPT-4.1 nano rates, rounded upward to micro-USD.

These eight-way tests exercised real Azure Table conditional transactions. They do not establish multi-instance scale, a 2,000-developer service level, or production throughput. The private logs retain correlation IDs and ledger partition IDs for operator investigation.

## Portal and automated checks

The local portal exercised the organization-limit drawer and separate policy publication: saving a $9 draft left the active $10 limit unchanged, and publishing activated $9. This used a synthetic organization and in-memory ledger; it is not a hosted-portal screenshot or Entra sign-in test.

Automated checks cover:

- 100 concurrent admission attempts, the three independent ceilings, owner requests, duplicate admission/settlement, ambiguous committed writes, UTC month changes and retained personal spend after a group move.
- Lowering limits below outstanding liability, stale publication, expired rates, unsupported billable features and an overrun that pauses new admission.
- Cache/reasoning usage, UTF-8 stream boundaries and missing terminal usage without refunds.
- Group-admin membership, grant, budget, publication, cost-query and content-query isolation; direct content IDs and revocation during reads.
- Owner-only full-reservation closure, durable audit before closure/publication, and exclusion of unrelated owner/team drafts from group publication.
- Backend token signature/issuer/tenant/audience/caller checks, and rejection of legacy gateway publication from a budget-connected portal.

All 179 Python tests pass under Python 3.11 and 3.13 on macOS. The two new tests cover importing the active policy into an empty portal without resetting liabilities, rejecting an unrelated configured owner, and preserving existing state during competing initializations. All 14 frontend tests pass; frontend type checking/build, lint/format checks and both budget Bicep templates compile. CI repeats the Python suite on macOS/Linux and Python 3.11/3.13.

After both native file-read sessions, all three original evaluation counters showed 6,537 micro-USD spent and zero reserved, consistent with the reported token usage and per-request upward rounding.

The native tests used temporary configurations and the reviewed two-model catalog, with no changes to normal workstation profiles or existing conversations. Codex used the documented [custom Responses provider](https://learn.chatgpt.com/docs/config-file/config-reference) and HTTP streaming. These short checks do not qualify long sessions or desktop UI behavior.

## Hosted portal follow-up — October 18, 2026

A separate budget-connected portal was deployed with private storage, a dedicated managed identity, and no APIM policy-write or Foundry inference role. Its package uses a separate deployment container. Anonymous state and balance requests returned 401; health and non-sensitive sign-in configuration returned 200 with `Cache-Control: no-store`.

The actual owner completed Entra sign-in using the existing browser session. The empty portal imported the active ledger policy. The owner created two independent test groups, assigned two directory-verified guest identities as group administrators, and published the reviewed budget policy through the hosted UI. Each test administrator has a $1 personal limit; each group has a $1 operating limit and $2 owner-controlled ceiling. Only the two reviewed models are delegated. The organization remains capped at $10. No Azure roles or directory invitations were granted to the testers.

Hosted reads after publication preserved the owner's original group/account limits and all three counters at 6,537 micro-USD spent with zero reserved. The audit recorded both group changes, both member changes and the publication request; all five events survived a code redeployment. Account names, directory IDs and screenshots containing them remain private.

**The two group administrators have not yet completed their independent sign-ins or cross-team tests.** Configuring their roles is not evidence of successful isolation. Follow the [hosted permission test](budget-permission-test.md), including negative direct-ID requests and revocation during content reads.

## Live $1 personal-budget test — October 19, 2026

The runtime and isolated portal were rebuilt and redeployed from `748dd2e`; both deployments succeeded. The deployed APIM budget policies matched that source. Through the signed-in hosted portal, the owner saved and published a $1 monthly personal limit. The organization remained at $10 and the owner's group at $5. Existing spend of 6,537 micro-USD was preserved, with no outstanding reservations.

- GPT-4.1 nano JSON and streaming requests both returned 200, each reporting 14 input and 2 output tokens. Together they charged six micro-USD.
- Three simultaneous Haiku 4.5 streaming requests each requested a 64,000-token output maximum but asked for a short, harmless response. Each required a 720,000-micro-USD reservation. One returned 200; two returned 403 `budget_denied`, explicitly identifying insufficient **user** allowance.
- A balance read during the successful stream showed exactly one pending reservation: 720,000 micro-USD held in all three scopes. The personal remaining allowance was 273,457 micro-USD, while the organization and group had ample allowance.
- Terminal usage for the admitted request was 36 input and 301 output tokens. It settled at 1,577 micro-USD. Final spend was 8,120 micro-USD, reserved allowance was zero, and the personal balance was $0.991880. The full test added $0.001583 in conservative ledger charges.
- The hosted portal displayed the same final balances. Anonymous portal state/balance, runtime and APIM reads remained rejected with 401 after redeployment.

The test demonstrates personal-limit enforcement against concurrent reservations on the existing owner's account. It did not spend the full dollar, qualify multi-instance load, or complete the independent group-admin tests. The $1 policy remains active on the dedicated budget routes; normal workstation profiles and baseline routes were not changed. Screenshots, account identifiers and request correlation IDs remain private.

### Cumulative spending and captured logs

A follow-up used synthetic padding and short responses to consume the same account's allowance through real inference. The $1 personal, $5 group and $10 organization limits stayed unchanged. No ledger resets, artificial charges or manual reservation closures were used.

- Six Haiku 4.5 requests returned 200, each reporting 55,000 input and nine output tokens. Each settled at 110,045 micro-USD. At $0.668390 cumulative spend, the next Haiku request was refused with 403 `budget_denied` and no outstanding reservation.
- Two GPT-4.1 nano requests returned 200, each reporting 900,059 input and eight output tokens, charged at 90,010 micro-USD each. The client was interrupted during a third request. A fresh authenticated balance read afterward showed another 90,010 micro-USD spent and zero reserved; the interrupted request was not retried.
- Final cumulative spend was **$0.938420**, reserved allowance **$0**, and personal remaining allowance **$0.061580**. This follow-up added $0.930300 in conservative ledger charges. Ordinary short requests with a 64-token output maximum were then sent to both reviewed models: both returned 403 `budget_denied`, explicitly identifying the monthly **user** budget. Before/after balances were identical.
- The remaining allowance cannot cover either model's conservative reservation. In this implementation, reservation uses the reviewed input-capacity ceiling plus the requested output maximum, even for a short prompt. GPT-4.1 nano required $0.104784 for the final probe. Refusal before exactly $1 is expected; the remaining allowance was not charged. These values are budget-ledger accounting, not Azure invoice reconciliation.
- Nano's throughput allocation was temporarily increased to accommodate the large synthetic inputs, then restored to its original Global Standard capacity of 10. Azure reported successful provisioning and the original 10,000-token/minute and 10-request/minute limits; model version and upgrade policy were preserved.

Native APIM logs contained prompt and response fragments for the six completed Haiku calls and the two completed nano calls. The signed-in owner opened a synthetic test's prompt and response through **Prompt & response logs → Inspect → Record access & open content**; the audit recorded the content-view request. The interrupted third nano call had a prompt-only record with gateway status `0`, not a confirmed HTTP success or captured response. This demonstrates why content logs must not be used as the spending ledger.

Capture remains opt-in with seven-day retention and a 256 KiB limit per direction. Large prompts may be truncated; interrupted requests can lack response fragments. Capture is not a complete conversation archive. The $1 policy remains active on the dedicated budget routes, and the remaining release checks below still apply.

## Remaining release checks

Full hosted administration journeys, long native-client/tool conversations, failure injection across runtime instances, and organization-scale load remain required. Only Haiku 4.5 and GPT-4.1 nano have reviewed prices and capacities in this live evaluation; this is not validation of every deployed model. The evaluation's reviewed rates expire on October 22, 2026; later inference requires fresh verification and publication.

See the complete [release checklist and trust boundary](budget-enforcement.md#release-acceptance). Do not distribute the evaluation endpoint as a customer spending guarantee or merge this branch into a release before those checks and bypass removal are complete.
