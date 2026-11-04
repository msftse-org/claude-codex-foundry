# Logs and metrics

The gateway reuses the Application Insights connection on standard APIM. The optional `admin observability` command enables custom metrics and information traces for the dedicated Claude API without changing the service-wide diagnostics or logger. Its trace contains the selected model, Entra principal object ID and APIM request ID. Prompt/response bodies and credentials are not added by this policy.

Run `admin observability --enable-dimensions` to configure dimensions on the connected Application Insights component, or in Application Insights, enable **Usage and estimated costs → Custom metrics → With dimensions** to retain the custom dimensions. The APIM diagnostic must have `metrics: true` and information verbosity. The supplied Bicep configures dimensions and those diagnostic properties for new installations; the observability command configures them for the dedicated API on existing installations. Review [Microsoft's setup instructions](https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-app-insights).

The policy emits token metrics under `ClaudeFoundry`, with API ID and model dimensions. Anthropic Messages support requires an APIM v2 tier. Interrupted streams may produce incomplete usage measurements. These metrics do not enforce a budget or replace Azure billing records. See [policy behavior and limits](https://learn.microsoft.com/en-us/azure/api-management/llm-emit-token-metric-policy).

The shared governed routes emit under `ClaudeCodexFoundry`, with API ID, Model and **Principal ID** dimensions. Principal ID comes from the validated token's `oid`, using the same expression as the budget principal header; it is not taken from a caller-supplied header. Reporting maps that principal to its group in the published budget policy. Unknown principals remain unattributed. Usage recorded before this upgrade stays unattributed when the principal dimension was not retained: publishing a policy does not backfill old metrics. Mapping uses the current published membership, not a reconstruction of past group moves.

Use Application Insights **Logs**, choosing its resource context, to run:

```kusto
requests
| where timestamp > ago(1h)
| where url has "/claude/v1/"
| summarize Requests=count(), Errors=countif(success == false), P95=percentile(duration, 95) by bin(timestamp, 5m)
```

```kusto
traces
| where timestamp > ago(1h)
| where message == "Claude inference request"
| project timestamp, operation_Id, customDimensions
| order by timestamp desc
```

```kusto
customMetrics
| where timestamp > ago(1h)
| summarize Samples=count(), Total=sum(valueSum) by name, tostring(customDimensions.Model)
```

Metric names and token categories depend on the provider response. Inspect the returned dimensions before building persistent workbooks. In a Log Analytics workspace context, use the corresponding `AppRequests`, `AppTraces` and `AppMetrics` tables and workspace column names instead.

Application Insights ingestion is asynchronous. Generate a verification request, allow a few minutes for ingestion, and inspect Requests/Logs. APIM integration does not by itself guarantee a populated **Live Metrics** stream: do not use an empty Live Metrics pane as evidence that gateway logging failed. Diagnose the logger destination, API diagnostic override, sampling, network restrictions and log-reader permissions first.

Existing diagnostic configurations may log more data than the new foundation defaults. Inspect both API and service diagnostics, including frontend/backend headers and body-byte counts. Set body counts to zero and avoid recording authorization or API-key headers unless your organization has explicitly approved a different policy.

## Token estimates and calendar-month charges

Cost analysis and Spend distinguish **Estimated from token metrics** from **Charged by the budget service (calendar month)**. Neither is an Azure invoice.

Planning estimates use the owner's input/output planning rates when entered, otherwise the published budget price's base list rates (or its reviewed ceilings when list rates are absent). An explicit `cache_read_per_million` in a planning price takes precedence; otherwise reporting uses the published `rates.cache_read`, never an unpublished budget-price draft or an inferred discount. Provider comes from the published price or the state's model metadata, never a deployment name or token magnitude.

For rates in USD per million tokens:

| Provider | Estimate |
| --- | --- |
| OpenAI | `((input − cached) × input_rate + cached × cache_read_rate + output × output_rate) / 1,000,000` |
| Anthropic | `(input × input_rate + cached × cache_read_rate + output × output_rate) / 1,000,000` |

OpenAI cache reads are part of reported input. Claude cache reads are additional to reported input. If the cache-read rate is missing, cached tokens use the input rate and the model appears under **Cache rate missing** in rate coverage. Missing provider metadata with cache usage makes the estimate incomplete rather than guessing a provider. Inconsistent metrics are normalized identically in Kusto and the Spend calculator: negative or non-finite counts become zero, and OpenAI cached counts cannot exceed reported input.

The regression fixtures use the repository's reviewed catalog: GPT-6 Astra Data Zone Standard is $11 input / $1.10 cache read / $55 output per million, including the 1.1× Data Zone multiplier; Claude Opus 5.5 fast Global Standard is $8 / $0.40 / $40. The recorded token fixtures produce $86.5089368 and $33.266584 respectively, instead of $603.775205 and $7.03592 with the old formula. These are fixture estimates, not claims about a live deployment.

Limitations:

- The gateway's token metrics do not report Claude cache-write usage. Those charges are absent from this estimate.
- Aggregated metrics cannot choose a long-context tier for each request. Planning estimates use base rates, not a premium inferred from a day's or month's aggregate token count.
- Streaming or interrupted usage can be estimated or incomplete. Application Insights ingestion, retention and sampling can differ from the ledger.
- Changing planning rates recalculates estimates across retained dates. It never rewrites charges, releases a reservation or changes Azure prices. Taxes, infrastructure costs and billing adjustments are not included.

The ledger table reads the existing `<YYYY-MM>-org`, `<YYYY-MM>-group-<id>` and `<YYYY-MM>-user-<id>` counters, preserving their micro-USD amounts. `spent` is charged; `reserved` is **Held, not charged**. Each month touched by the selected range is read in full, including dates outside the selected days. The current month contains charges so far. The rows are not prorated or filtered by token-model/group/member selectors, and organization, group and member totals overlap: do not add them together.

Cost analysis uses its inclusive UTC date selection. Spend's token estimate remains month-to-date; its ledger table covers every calendar month touched by the selected 7-, 30- or 90-day request-reporting period, with the range printed separately.

Both reporting APIs apply the budget-balance scope to the authenticated state snapshot: owner, administrator, billing and auditor can see all counters; group administrators can see their group and its members; developers can see only their own member counters. Restricted scopes intersect current and published membership, so an unpublished group move cannot expose an old team's totals or newly moved members. Restricted telemetry responses omit organization-wide request summaries and identities entirely, not just in the UI. Membership awaiting publication is disclosed without showing zero charges.

If no ledger is configured, only the estimate is shown. Storage or snapshot query failures are errors, never silently replaced with a zero balance. Token queries can be cached for 60 seconds; ledger counters are reread on each reporting request. Reporting remains read-only, including while admission is paused.

For an authorized upgrade of your own test deployment, use `uv run python scripts/deploy.py --settings <test-settings.json> --upgrade-portal`. This updates the portal and governed metric policy; a source merge alone deploys nothing. Check dimension retention, then make one approved test request and allow for ingestion before checking member/group attribution. No historical metrics or counters are migrated.

## Optional prompt and response capture

Operational metrics remain metadata-only. The separate [content-log deployment and portal workspace](content-logs.md) supports opt-in native APIM message capture with restricted, audited reads. It uses a dedicated Log Analytics workspace and does not require enabling HTTP body capture in Application Insights.
