"""Restricted native APIM message logs; query scopes never come from the browser."""

import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

from azure.storage.blob import BlobServiceClient
from pydantic import AwareDatetime, Field, field_validator, model_validator

from .domain import StrictModel


class LogSource(StrictModel):
    workspace_id: UUID
    resource_id: str = Field(
        pattern=r"(?i)^/subscriptions/[0-9a-f-]+/resourcegroups/[^/]+/providers/microsoft\.apimanagement/service/[^/]+$"
    )
    api_ids: list[str] = Field(min_length=1, max_length=10)
    retention_days: int = Field(default=7, ge=4, le=30)
    max_message_bytes: int = Field(default=262144, ge=1024, le=262144)
    capture_enabled: bool = False
    stream_capture_account: str = Field(default="", pattern=r"^$|^[a-z0-9]{3,24}$")
    stream_capture_container: str = Field(default="content-logs", pattern=r"^[a-z0-9-]{3,63}$")


class LogQuery(StrictModel):
    start: AwareDatetime
    end: AwareDatetime
    api_id: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=128)
    correlation_id: UUID | None = None
    principal_id: UUID | None = None
    before: AwareDatetime | None = None
    before_id: UUID | None = None

    @model_validator(mode="after")
    def bounds(self):
        if bool(self.before) != bool(self.before_id):
            raise ValueError("A page cursor requires both its timestamp and request ID.")
        if self.end <= self.start or self.end - self.start > timedelta(days=30):
            raise ValueError("Choose a time range of at most 30 days.")
        if self.end > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("The end time cannot be in the future.")
        return self


class LogRead(StrictModel):
    correlation_id: UUID
    reason: str = Field(min_length=5, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if len(value) < 5:
            raise ValueError("Provide a reason for viewing this content.")
        return value


def run_query(source, credential, query):
    token = credential.get_token("https://api.loganalytics.io/.default").token
    request = Request(
        f"https://api.loganalytics.azure.com/v1/workspaces/{source.workspace_id}/query",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=45) as response:
            result = json.load(response)
    except HTTPError as error:
        # Operational diagnostics must never include the query, token, or returned content.
        logging.getLogger(__name__).warning(
            "Content log workspace query failed (HTTP %s).", error.code
        )
        raise
    if result.get("error"):
        raise ValueError("Log Analytics returned a partial result.")
    table = result["tables"][0]
    columns = [c["name"] for c in table["columns"]]
    return [dict(zip(columns, row, strict=True)) for row in table["rows"]]


def scoped_query(source, start, end, group_id=None):
    # Clamp every read, including direct IDs, to the configured retention window.
    start = max(start, datetime.now(UTC) - timedelta(days=source.retention_days))
    scope = json.dumps(source.resource_id.lower())
    apis = ",".join(json.dumps(a) for a in source.api_ids)
    period = f"TimeGenerated between (datetime({start.isoformat()}) .. datetime({end.isoformat()}))"
    group_filter = f'| where group_id == "{UUID(group_id)}"' if group_id else ""
    return f"""let gateway = ApiManagementGatewayLogs
| where {period} and tolower(_ResourceId) == {scope}
| where ApiId in ({apis})
| mv-apply trace=parse_json(tostring(TraceRecords)) on (
    summarize principal_id=take_anyif(tostring(trace.metadata.principal_id), tostring(trace.source) in ("claude-foundry", "codex-foundry")), group_id=take_anyif(tostring(trace.metadata.group_id), tostring(trace.source) == "budget-governance"))
| summarize arg_max(TimeGenerated, ApiId, ResponseCode, principal_id, group_id) by CorrelationId
{group_filter};
let messages = ApiManagementGatewayLlmLog
| where {period} and tolower(_ResourceId) == {scope}
| join kind=inner gateway on CorrelationId;
"""


def list_logs(source, credential, filters, group_id=None):
    if filters.api_id and filters.api_id not in source.api_ids:
        raise ValueError("Choose a configured API.")
    query = scoped_query(source, filters.start, filters.end, group_id)
    query += "messages\n"
    if filters.api_id:
        query += f"| where ApiId == {json.dumps(filters.api_id)}\n"
    if filters.correlation_id:
        query += f'| where CorrelationId == "{filters.correlation_id}"\n'
    if filters.principal_id:
        query += f'| where principal_id == "{filters.principal_id}"\n'
    query += """| summarize timestamp=min(TimeGenerated), api_id=take_any(ApiId),
model=take_anyif(DeploymentName, isnotempty(DeploymentName)), model_name=take_anyif(ModelName, isnotempty(ModelName)), status=take_any(ResponseCode),
has_request=countif(isnotempty(RequestMessages)) > 0, has_response=countif(isnotempty(ResponseMessages)) > 0,
chunks=count(), principal_id=take_any(principal_id) by correlation_id=CorrelationId
"""
    # Native content fragments omit the model; filter AFTER correlation/aggregation.
    if filters.model:
        query += f"| where model == {json.dumps(filters.model)} or model_name == {json.dumps(filters.model)}\n"
    if filters.before:
        query += f'| where timestamp < datetime({filters.before.isoformat()}) or (timestamp == datetime({filters.before.isoformat()}) and correlation_id > "{filters.before_id}")\n'
    query += "| order by timestamp desc, correlation_id asc | take 51"
    rows = run_query(source, credential, query)
    if source.stream_capture_account:
        # A governed request can be represented by both APIM's native LLM log
        # and our complete streaming-proxy capture. Keep one list entry per
        # request, preferring the stream record because it is the authoritative
        # detail source for streamed responses.
        rows_by_id = {str(row["correlation_id"]): row for row in rows}
        rows_by_id.update(
            {
                str(row["correlation_id"]): row
                for row in _stream_records(source, credential, filters, group_id)
            }
        )
        rows = list(rows_by_id.values())
    rows.sort(key=lambda row: (row["timestamp"], row["correlation_id"]), reverse=True)
    return {"items": rows[:50], "has_more": len(rows) > 50}


def read_log(source, credential, correlation_id, group_id=None):
    if source.stream_capture_account:
        captured = _stream_record(source, credential, correlation_id)
        if captured and (not group_id or captured.get("group_id") == str(UUID(group_id))):
            return {
                "correlation_id": str(correlation_id),
                "chunks": [
                    {
                        "timestamp": captured["timestamp"],
                        "api_id": captured["api_id"],
                        "model": captured["model"],
                        "model_name": captured["model"],
                        "status": captured["status"],
                        "principal_id": captured["principal_id"],
                        "sequence": 0,
                        "request_id": captured["request_id"],
                        "request": captured["request"],
                        "response": captured["response"],
                    }
                ],
                "max_message_bytes": source.max_message_bytes,
                "note": "Captured by the governed streaming proxy. Prompt and response are size-limited, and the completion flag records whether authoritative stream settlement finished.",
            }
    end = datetime.now(UTC)
    query = scoped_query(source, end - timedelta(days=source.retention_days), end, group_id)
    query += f"""messages | where CorrelationId == "{correlation_id}"
| project timestamp=TimeGenerated, api_id=ApiId, model=DeploymentName, model_name=ModelName,
status=ResponseCode, principal_id, sequence=SequenceNumber, request_id=RequestId,
request=RequestMessages, response=ResponseMessages
| order by sequence asc, timestamp asc | take 129"""
    rows = run_query(source, credential, query)
    if len(rows) > 128:
        raise ValueError("Too many message fragments for a single detail view.")
    if not rows:
        return None
    # Preserve native fragments and sequence numbers; never silently stitch or summarize them.
    return {
        "correlation_id": str(correlation_id),
        "chunks": rows,
        "max_message_bytes": source.max_message_bytes,
        "note": "Captured messages may include conversation context and tool data. Native capture is size-limited; interrupted streams or ingestion delays can leave missing fragments. This is not a complete conversation archive.",
    }


def _stream_container(source, credential):
    return BlobServiceClient(
        f"https://{source.stream_capture_account}.blob.core.windows.net",
        credential=credential,
    ).get_container_client(source.stream_capture_container)


def _stream_record(source, credential, correlation_id):
    suffix = f"{UUID(str(correlation_id))}.json"
    container = _stream_container(source, credential)
    for blob in container.list_blobs():
        if blob.name.endswith(suffix):
            return json.loads(container.download_blob(blob.name).readall())
    return None


def _stream_records(source, credential, filters, group_id=None):
    container = _stream_container(source, credential)
    rows = []
    earliest = max(filters.start, datetime.now(UTC) - timedelta(days=source.retention_days))
    day = earliest.date()
    final_day = filters.end.date()
    blobs = []
    while day <= final_day:
        blobs.extend(container.list_blobs(name_starts_with=f"{day:%Y/%m/%d}/"))
        day += timedelta(days=1)
    blobs.sort(key=lambda blob: blob.name, reverse=True)
    for blob in blobs:
        if len(rows) >= 500:
            break
        try:
            record = json.loads(container.download_blob(blob.name).readall())
            timestamp = datetime.fromisoformat(record["timestamp"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if not earliest <= timestamp <= filters.end:
            continue
        if group_id and record.get("group_id") != str(UUID(group_id)):
            continue
        if filters.api_id and record.get("api_id") != filters.api_id:
            continue
        if filters.model and record.get("model") != filters.model:
            continue
        if filters.correlation_id and record.get("correlation_id") != str(filters.correlation_id):
            continue
        if filters.principal_id and record.get("principal_id") != str(filters.principal_id):
            continue
        if filters.before and not (
            timestamp < filters.before
            or (timestamp == filters.before and record["correlation_id"] > str(filters.before_id))
        ):
            continue
        rows.append(
            {
                "timestamp": record["timestamp"],
                "api_id": record["api_id"],
                "model": record["model"],
                "model_name": record["model"],
                "status": record.get("status"),
                "has_request": record.get("request") is not None,
                "has_response": record.get("response") is not None,
                "chunks": 1,
                "principal_id": record["principal_id"],
                "correlation_id": record["correlation_id"],
            }
        )
    return rows
