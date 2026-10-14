"""Azure access with managed identity and fixed, server-owned resource scopes."""

import json
from urllib.request import Request, urlopen

from ..azure import Azure, api_path
from .governance import compile_policy


class ManagedAzure(Azure):
    def __init__(self, subscription_id, credential):
        super().__init__(subscription_id)
        self.credential = credential

    def token(self):
        return self.credential.get_token("https://management.azure.com/.default").token


def publish(config, state, azure):
    policy, catalog = compile_policy(config, state)
    paths = [
        api_path(config) + "/policies/policy",
        api_path(config) + "/operations/models/policies/policy",
    ]
    old = [azure.request("GET", p) for p in paths]
    changed = []
    try:
        for path, xml in zip(paths, [policy, catalog], strict=True):
            azure.request("PUT", path, {"properties": {"format": "rawxml", "value": xml}})
            changed.append(path)
    except Exception:
        # Restore both policies if the second update failed; report rollback failures too.
        for path, previous in zip(paths, old, strict=True):
            if path in changed:
                azure.request(
                    "PUT",
                    path,
                    {
                        "properties": {
                            "format": previous["properties"].get("format", "rawxml"),
                            "value": previous["properties"]["value"],
                        }
                    },
                )
        raise


def telemetry(app_id, credential, config, days=30):
    empty = {"daily": [], "models": [], "people": [], "requests": [], "month_tokens": []}
    if not app_id:
        return {"status": "not_configured", **empty}
    if days not in {7, 30, 90}:
        raise ValueError("Unsupported reporting period")
    gateway_path = json.dumps(config["gateway_url"].rstrip("/") + "/v1/")
    api_id, service_id = json.dumps(config["api_id"]), json.dumps(config["apim_name"])
    # Aggregate in Kusto: counts must not silently stop at the first 10,000 requests.
    query = f"""let calls = requests | where timestamp > ago({days}d) | where url startswith {gateway_path} | project timestamp, name, resultCode, duration, operation_Id, weight=tolong(coalesce(itemCount, 1));
let actors = traces | where timestamp > ago({days}d) | where message == "Claude inference request" | extend model=tostring(customDimensions.model), principal_id=tostring(customDimensions.principal_id) | summarize model=take_any(model), principal_id=take_any(principal_id) by operation_Id;
let joined = materialize(calls | join kind=leftouter actors on operation_Id | extend model=iff(isempty(model), "Unattributed", model), principal_id=iff(isempty(principal_id), "Unattributed", principal_id));
union
(joined | summarize requests=sum(weight), errors=sumif(weight, resultCode !startswith "2"), duration_ms=sum(duration * weight) by key=format_datetime(timestamp, "yyyy-MM-dd") | project bucket="daily", data=pack_all()),
(joined | summarize requests=sum(weight), errors=sumif(weight, resultCode !startswith "2"), duration_ms=sum(duration * weight) by key=model | project bucket="models", data=pack_all()),
(joined | summarize requests=sum(weight), errors=sumif(weight, resultCode !startswith "2"), duration_ms=sum(duration * weight) by key=principal_id | project bucket="people", data=pack_all()),
(joined | top 250 by timestamp desc | project timestamp, name, resultCode, duration, model, principal_id | project bucket="requests", data=pack_all()),
(customMetrics | where timestamp >= startofmonth(now()) | where tostring(customDimensions["API ID"]) == {api_id} and tostring(customDimensions["Service ID"]) == {service_id}
| where name in ("Prompt Tokens", "Completion Tokens", "Prompt Cached Tokens")
| summarize input_tokens=sumif(coalesce(valueSum, value), name == "Prompt Tokens"), output_tokens=sumif(coalesce(valueSum, value), name == "Completion Tokens"), cached_tokens=sumif(coalesce(valueSum, value), name == "Prompt Cached Tokens") by model=tostring(customDimensions.Model), group_id=tostring(customDimensions["Group ID"]), principal_id=tostring(customDimensions["Principal ID"])
| project bucket="month_tokens", data=pack_all())"""
    token = credential.get_token("https://api.applicationinsights.io/.default").token
    req = Request(
        f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        data=json.dumps({"query": query}).encode(),
    )
    with urlopen(req, timeout=45) as response:
        data = json.load(response)
    if data.get("error"):
        raise ValueError(
            "Application Insights returned a partial result; retry with a shorter period"
        )
    table = data["tables"][0]
    columns = [column["name"] for column in table["columns"]]
    result = {"status": "connected", **empty}
    for raw in table["rows"]:
        row = dict(zip(columns, raw, strict=True))
        if row["bucket"] in empty:
            result[row["bucket"]].append(
                json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            )
    result["daily"].sort(key=lambda row: row["key"])
    result["requests"].sort(key=lambda row: row["timestamp"], reverse=True)
    result["note"] = (
        "Application Insights ingestion is delayed. Counts include the selected period and honor telemetry sampling weights; recent request details show up to 250 entries. Token metrics cover the current UTC calendar month. Streaming tokens are estimates. No prompts or responses are collected."
    )
    return result
