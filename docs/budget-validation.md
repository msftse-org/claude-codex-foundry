# Budget hierarchy validation — October 15, 2026

**Unreleased feature branch: `codex/budget-hierarchy`.** This record covers an isolated budget runtime and new standard APIM APIs. The existing demo portal, baseline endpoints and workstation profiles have not been migrated to these controls.

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

All 177 Python tests pass under Python 3.11 and 3.13 on macOS. All 14 frontend tests pass; frontend type checking/build, lint/format checks and budget Bicep compilation also pass. CI repeats the Python suite on macOS/Linux and Python 3.11/3.13.

After both native file-read sessions, all three original evaluation counters showed 6,537 micro-USD spent and zero reserved, consistent with the reported token usage and per-request upward rounding.

The native tests used temporary configurations and the reviewed two-model catalog, with no changes to normal workstation profiles or existing conversations. Codex used the documented [custom Responses provider](https://learn.chatgpt.com/docs/config-file/config-reference) and HTTP streaming. These short checks do not qualify long sessions or desktop UI behavior.

## Remaining release checks

The hosted portal is not connected to this ledger yet. Two real group-admin identities, full hosted administration journeys, long native-client/tool conversations, failure injection across runtime instances, and organization-scale load remain required. Only Haiku 4.5 and GPT-4.1 nano have reviewed prices and capacities in this live evaluation; this is not validation of every deployed model.

See the complete [release checklist and trust boundary](budget-enforcement.md#release-acceptance). Do not distribute the evaluation endpoint as a customer spending guarantee or merge this branch into a release before those checks and bypass removal are complete.
