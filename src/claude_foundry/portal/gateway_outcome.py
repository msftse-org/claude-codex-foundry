"""Read-only APIM evidence for held reservations, never inferred billing refunds."""

import json
from datetime import UTC, datetime
from urllib.request import Request, urlopen

UNBILLED_STATUSES = frozenset({"401", "403", "404", "429"})


def outcome_cause(result_code):
    if result_code == "429":
        return "Throttled, not billed"
    if result_code in {"401", "403", "404"}:
        return "Rejected, not billed"
    if result_code == "200":
        return "Completed, not settled"
    if result_code.startswith("200 [not sent in full"):
        return "Client disconnected"
    if result_code.startswith("4"):
        return "Rejected, may be billed"
    return "Gateway response, billing unknown"


def build_query(config, request_ids):
    # Keep both the deployment role and request IDs as JSON-encoded KQL literals.
    return f"""requests
| where cloud_RoleName == {json.dumps(config["apim_name"])}
| where tostring(customDimensions["Request Id"]) in (dynamic({json.dumps(request_ids)}))
| summarize records=count(), arg_max(timestamp, resultCode) by request_id=tostring(customDimensions["Request Id"]), gateway_role=tostring(cloud_RoleName)
| project request_id, gateway_role, records, result_code=tostring(resultCode), timestamp"""


def gateway_outcomes(app_id, credential, config, request_ids):
    request_ids = list(dict.fromkeys(request_ids))

    def empty(cause):
        return {
            request_id: {
                "result_code": None,
                "timestamp": None,
                "cause": cause,
                "releasable": False,
            }
            for request_id in request_ids
        }

    result = {
        "status": "connected" if app_id else "not_configured",
        "items": empty("No gateway record" if app_id else "Gateway outcome unavailable"),
    }
    if not app_id or not request_ids:
        return result
    try:
        if not config.get("apim_name") or len(request_ids) > 200:
            raise ValueError("A deployment role and at most 200 request IDs are required")
        token = credential.get_token("https://api.applicationinsights.io/.default").token
        request = Request(
            f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            data=json.dumps({"query": build_query(config, request_ids)}).encode(),
        )
        with urlopen(request, timeout=45) as response:
            data = json.load(response)
        if data.get("error") or not data.get("tables"):
            raise ValueError("Application Insights returned an incomplete result")
        table = data["tables"][0]
        columns = [column["name"] for column in table["columns"]]
        required = {"request_id", "gateway_role", "records", "result_code", "timestamp"}
        if len(set(columns)) != len(columns) or not required.issubset(columns):
            raise ValueError("Application Insights returned incomplete columns")
        records = {request_id: [] for request_id in request_ids}
        for raw in table["rows"]:
            row = dict(zip(columns, raw, strict=True))
            if row["request_id"] in records:
                records[row["request_id"]].append(row)
        for request_id, rows in records.items():
            if not rows:
                continue
            if len(rows) != 1 or type(rows[0]["records"]) is not int or rows[0]["records"] != 1:
                result["items"][request_id]["cause"] = "Ambiguous gateway records"
                continue
            row = rows[0]
            if row["gateway_role"] != config["apim_name"]:
                continue
            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
                if timestamp.tzinfo is None or not isinstance(row["result_code"], str):
                    raise ValueError("Missing gateway evidence")
                if not row["result_code"]:
                    raise ValueError("Missing gateway status")
            except (TypeError, ValueError):
                result["items"][request_id]["cause"] = "Gateway outcome unavailable"
                continue
            result["items"][request_id] = {
                "result_code": row["result_code"],
                "timestamp": timestamp.astimezone(UTC).isoformat(),
                "cause": outcome_cause(row["result_code"]),
                "releasable": row["result_code"] in UNBILLED_STATUSES,
            }
    except Exception:
        result.update(status="unavailable", items=empty("Gateway outcome unavailable"))
    return result
