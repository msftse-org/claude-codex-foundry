# Logs and metrics

The gateway reuses the Application Insights connection on standard APIM. The optional `admin observability` command enables custom metrics and information traces for the dedicated Claude API without changing the service-wide diagnostics or logger. Its trace contains the selected model, Entra principal object ID and APIM request ID. Prompt/response bodies and credentials are not added by this policy.

Run `admin observability --enable-dimensions` to configure dimensions on the connected Application Insights component, or in Application Insights, enable **Usage and estimated costs → Custom metrics → With dimensions** to retain the custom dimensions. The APIM diagnostic must have `metrics: true` and information verbosity. The supplied Bicep configures dimensions and those diagnostic properties for new installations; the observability command configures them for the dedicated API on existing installations. Review [Microsoft's setup instructions](https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-app-insights).

The policy emits token metrics under `ClaudeFoundry`, with API ID and model dimensions. Anthropic Messages support requires an APIM v2 tier. Interrupted streams may produce incomplete usage measurements. These metrics do not enforce a budget or replace Azure billing records. See [policy behavior and limits](https://learn.microsoft.com/en-us/azure/api-management/llm-emit-token-metric-policy).

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
