# Operating Claude & Codex on Azure

[← README](../README.md) · [Deployment](administrator.md) · [Troubleshooting](troubleshooting.md)

The organization owns its Azure subscription, Entra registrations, gateway configuration, user distribution, and operating costs. Assign owners before a pilot so an expired login, denied model, or failed policy publication has a clear support path.

## Assign ownership

| Responsibility | Typical owner |
| --- | --- |
| Subscription, Marketplace eligibility, quota, and billed spend | Azure subscription/billing administrator |
| Entra consent, MFA, Conditional Access, and directory account lifecycle | Identity team |
| APIM policies, Foundry deployments, monitoring, and recovery | Platform team |
| Portal membership, groups, and model grants | Portal owner/administrator |
| Planning rates and budget estimates | Billing administrator |
| Claude installation, MDM precedence, and settings recovery | Desktop support |

Portal roles do not grant Azure subscription or Entra administrator permissions.

## Pilot acceptance checklist

**Spending-control gate:** a customer requiring enforced dollar limits must not treat the current preview as satisfying that requirement. Complete the [budget enforcement and delegated-administration acceptance checks](budget-enforcement.md) before such a rollout. Current token quotas and dollar planning thresholds do not establish hard monetary enforcement.

- Deploy with the customer's own parameters and review Azure `what-if` output.
- Verify representative models, streaming, token counting, rejected credentials, and denied model access.
- Complete Microsoft sign-in under the customer's actual Conditional Access policy.
- Install the generated bundle on a pilot Mac; test Chat, Code, CLI, and settings restore.
- Confirm requests and token metrics arrive in the intended Application Insights resource.
- For portal governance, review all intended callers before publishing on the shared Claude API. Verify allowed requests there; use an explicitly isolated API for disruptive suspension/quota tests. Review direct Foundry RBAC separately.
- Set Azure billing alerts separately from portal planning thresholds. Record model SKU, geography, quota, and the source/date of planning rates.
- Exercise recovery and measure representative workload latency, throttling, and cost before expanding the pilot.

The [portal validation record](../portal/VALIDATION.md) lists outstanding production checks. Configuration tests involving 2,000 members are not a throughput guarantee for 2,000 concurrent developers.

## Monitor the service

Use the [observability queries](observability.md) to track requests, failures, latency, and tokens. Monitor 401/403 authorization failures separately from 429 throttling and 5xx backend errors. An empty Live Metrics pane alone is not proof of no traffic; inspect ingested Logs and the configured resource/API scope.

The portal's usage page reads operational metadata. Cost analysis uses retained token metrics, current planning rates, and a short query cache. It does not ingest invoices. Document log retention, sampling, access to identity data, and alert ownership in your organization's operating procedures.

The portal can scale down independently of inference. Published APIM policies continue to operate if the portal is unavailable; administrators cannot make new publications until it recovers.

### Diagnose held budget reservations

The shared budget runtime sends warnings to the deployment's existing Application Insights component using its managed identity, the same monitoring role module and authentication settings as the portal. The default runtime log level remains **Warning**; `Trace` and `Exception` telemetry are excluded from host sampling. This is diagnostic telemetry, not a second spend ledger, and ingestion outages or retention can still make a trace unavailable.

On an authorized test deployment, upgrade with the existing settings before relying on these diagnostics:

```sh
uv run --frozen --extra auth --extra portal python scripts/deploy.py --settings "<test-settings.json>" --upgrade-portal
```

This command changes Azure resources and restarts the runtime and portal; it is not an offline check. The upgrade reuses the portal's Application Insights name and resource group, adds the runtime identity's monitoring access and both Application Insights settings, and preserves existing reservations and monthly counters. Allow for the upgrade outage described below. A source merge alone does not enable telemetry.

In that Application Insights component, open **Logs** and run this query, replacing the runtime Function app placeholder:

```kusto
traces
| where timestamp > ago(24h)
| where cloud_RoleName == "<runtime-function-app>"
| where severityLevel >= 2
| where message startswith "Reservation held: "
| parse message with "Reservation held: request=" request_id " reason=" hold_reason " error=" error_detail
| extend exception_type = tostring(split(error_detail, " message=")[0])
| project timestamp, request_id, hold_reason, exception_type, message
| order by timestamp desc
```

The warning identifies one held outcome per request. It contains only the generated request identifier, the reason and exception type (`None` when no exception occurred). Settlement failures also include a sanitized message: fixed application-authored ledger messages are retained; other exception messages are redacted because SDK errors can include bodies, headers or resource identifiers. Storage errors may include a numeric HTTP status. Prompts, responses, usage bodies, headers and exception tracebacks are not added to these warnings.

An authorized ledger reader can match `request_id` to the `request-<request-id>` reservation in the organization's `BudgetLedger` partition. The row's JSON `data` contains the following optional metadata:

| `hold_reason` | Meaning |
| --- | --- |
| `dispatch_unknown` | Dispatch failed before a response was obtained; the outcome is unknown. |
| `client_disconnected` | The caller left before terminal usage could be settled. |
| `stream_incomplete` | The upstream stream ended or failed without trustworthy terminal usage. |
| `rejected_possibly_billed` | The upstream rejection was not confirmed to be unbilled. |
| `usage_unreadable` | A successful response's usage could not be read or priced. |
| `settlement_failed` | The settlement operation failed after usage was read, or an existing safe rejection-release write failed. |

`held_at` is the UTC time of the metadata attempt that succeeded. Missing metadata means **Not recorded**: older reservations are not backfilled, and a metadata-store failure cannot interrupt a response. These writes use etag checks, retry contention only a bounded number of times, never change counters or reservation state, and stop if settlement has already won the race. A later successful settlement may retain the diagnostic fields, so always check the current row's `state`; a historical warning is not evidence that the reservation is still open.

The monthly `<YYYY-MM>-org`, `<YYYY-MM>-group-<id>` and `<YYYY-MM>-user-<id>` counters remain authoritative. Do not clear, refund or reduce holds to repair missing telemetry. A completed HTTP 200 response does not itself prove settlement; collect its sanitized failure classification in an authorized live session before proposing a root-cause repair. This change adds evidence, not that repair. If a rollback is needed, redeploy the previous reviewed package without deleting the ledger, diagnostics or published policy.

## Change models or quota

1. Retain the current organization config, model report, client profile, and gateway policies in restricted storage.
2. Discover available models and review cost, eligibility, retirement, and quota. Deploy only the reviewed selection.
3. Run `sync-models` and review the resulting catalog and family defaults.
4. For the baseline API, publish with the administrator gateway command and verify it. For a portal-governed API, update the portal's configured catalog and review/publish through the portal workflow; do not overwrite its policy with the baseline gateway command.
5. Update sourced planning rates as needed, export fresh client profiles, rebuild bundles, and distribute them internally.

Client model catalogs do not refresh automatically. Catalog changes must also be reflected in existing portal groups/defaults and personal grants; there is no automatic model-retirement migration. Validate changes in the preview first.

Model capacity allocation is not a spending cap. The deployment command preserves existing capacity; review and change existing allocations explicitly through Azure. Additional resources do not create additional subscription-wide model quota.

## Upgrade application code

Use a reviewed commit and keep the previous package/commit available. Re-run relevant checks, inspect configuration/schema changes, and validate in the preview environment before rollout. `main` updates and GitHub CI do not deploy Azure automatically.

Build portal code with `scripts/build_portal.py` and deploy the explicit package as described in the [portal guide](../portal/README.md#azure-deployment). Verify health, Microsoft sign-in, authenticated state and telemetry after deployment.

Flex Consumption apps keep running the previous package after an upload succeeds. `scripts/deploy.py` therefore stops each Function app (the budget runtime first, then the portal) for 75 seconds after uploading, starts it again, and waits until its health route reports the uploaded package's build ID (`/health` on the runtime, `/api/health` on the portal). Plan for about two minutes without governed inference, and then without the portal, during an upgrade. If you upload a package by hand, do the same stop, wait and start, and compare the `build` in the health response with `build.json` in the package. A deployment that times out still serving the old build has not upgraded that app. Exercise publication changes on an isolated API first; on a governed shared route, confirm the reviewed publication and run `claude-foundry verify --governed`. Record the deployed commit and verification outcome.

For Mac updates, distribute a newly built organization bundle. Users rerun `Install.command`, then restart Claude. Existing unrelated settings and later user changes must be preserved; [restore](macos.md#settings-and-recovery) is available if required.

## Recovery and rollback

| Situation | Recovery approach |
| --- | --- |
| Mac configuration problem | Run the installed `claude-foundry restore`, then restart Claude. This restores managed settings, not credentials or imported history. Review copied batches separately in Claude's import history UI. |
| Baseline gateway regression | Reapply the last reviewed baseline configuration/policies to that API and verify allowed/denied requests. |
| Portal policy publication failure | Retain the draft and follow the publication recovery procedure below. Restore attempts are best effort; do not assume that an error means Azure changed nothing. |
| Portal code regression | Redeploy the last verified application package after checking state compatibility. |
| Accidental portal state change | Private Blob versioning and 30-day soft deletion support recovery. Have an authorized operator select and validate a prior version, and reconcile it with the policy currently published in APIM. Exercise this procedure in preview before relying on it. |

Blob recovery and gateway-policy recovery are separate actions. Restoring one does not automatically restore the other. Audit records share the mutable state Blob and are not an immutable archive. Preserve authorized incident evidence before a recovery operation.

### Portal publication recovery

1. Stop other publication jobs and manual policy edits for the affected API. Record the API ID, UTC time, reviewed revision, status, and audit events. Keep snapshots private because policies contain principal object IDs and grants.
2. An Azure operator should read **both** the API-level `policies/policy` resource and `operations/models/policies/policy`. Compare them with the last reviewed policy pair and retained backup. A timeout can occur after a write was applied. The application tries to restore every attempted policy, but abrupt termination or another Azure failure can prevent recovery.
3. Inspect ARM activity and the portal managed identity's API-scoped role assignment. A portal `failed` result does not certify successful rollback. A `publishing` state can remain after termination; the server blocks another publication for ten minutes from `publish_started_at`.
4. Choose either the last known-good pair or the current reviewed draft. Restore both old policies through an authorized operator, or, after the ten-minute stale-publication window, reload and explicitly review/publish the current draft. Do not clear the marker or change a revision manually merely to bypass the lock.
5. Verify an allowed caller, a denied model/member, the filtered model catalog, and streaming. Confirm the portal's published hash/status and retain a recovery audit. Successful ARM writes and actual gateway propagation are different events.

Publication is serialized within the portal using Blob ETags, but the two ARM writes are not transactional and do not protect against external manual policy writers. Designate one management path per API. A process-kill recovery drill and a real Blob-version restore drill remain customer acceptance requirements.

For Blob recovery, use an operator with data-plane rights from a network that can reach the private endpoint. List versions of `portal-state/organization.json`, download the current and selected versions to restricted storage, validate membership/ownership/model references, then restore the reviewed version with a conditional write. Do not enable a public endpoint or shared keys to make recovery easier. Restored portal state can differ from APIM's active policy; review and reconcile the pair before reopening administration.

## Offboarding and retirement

For the baseline gateway, revoke the user's/group's Foundry inference permission and any applicable APIM object-ID entry. For a quick-start deployment, run `uv run python scripts/onboard_users.py --remove PERSON` to remove them from the developer group. For portal governance, suspend the member, review/publish the policy, and verify denial of a new request. Portal sign-in uses the current active-member state; gateway enforcement uses the last published state. In-flight requests may finish.

Review direct Foundry RBAC and any separately granted permissions. Portal suspension does not disable an Entra account, erase a user's local conversations, or uninstall Claude. Coordinate those actions with identity and desktop support.

To retire the deployment, first restore affected client settings, stop new onboarding, and retain required configuration/log/audit evidence. Inventory dedicated and shared Azure resources and app registrations. Remove only resources approved for deletion; never delete a shared resource group as a shortcut. Stopping the portal alone does not remove APIM's ongoing charges.
