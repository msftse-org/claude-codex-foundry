"""Bounded, read-only cost exploration over APIM token metrics.

Rates are customer-entered planning rates, never presented as an Azure invoice.
All totals are aggregated in Kusto before breakdowns are limited.
"""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UNATTRIBUTED = "__unattributed__"
BREAKDOWN_LIMIT = 2500


def planning_prices(state):
    """Use explicit planning rates first, then reviewed list rates or ceilings."""
    prices = list(state.get("prices", []))
    explicit = {p["model"] for p in prices}
    for value in state.get("budget_prices", []):
        if value["model"] not in explicit:
            rates = value.get("rates")
            prices.append(
                {
                    "model": value["model"],
                    "input_per_million": float(
                        rates["input"] if rates else value["input_ceiling_per_million"]
                    ),
                    "output_per_million": float(
                        rates["output"] if rates else value["output_ceiling_per_million"]
                    ),
                    "source_url": value["source_url"],
                    "verified_on": value["verified_at"][:10],
                    "note": (
                        "Planning estimate at reviewed list rates; not an Azure invoice."
                        if rates
                        else "Planning estimate at reviewed enforcement ceilings; not an Azure invoice."
                    ),
                }
            )
    return prices


class CostQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    model_ids: list[str] = Field(default_factory=list, max_length=100)
    group_ids: list[str] = Field(default_factory=list, max_length=2500)
    member_ids: list[str] = Field(default_factory=list, max_length=2500)
    grouping: Literal["model", "group", "member"] = "model"

    @field_validator("model_ids", "group_ids", "member_ids")
    @classmethod
    def identifiers(cls, values):
        if any(
            not value or len(value) > 160 or any(ord(c) < 32 for c in value) for value in values
        ):
            raise ValueError("Filter identifiers must contain 1–160 printable characters")
        return sorted(set(values))

    @model_validator(mode="after")
    def date_range(self):
        if not 0 <= (self.end - self.start).days < 93:
            raise ValueError("Choose a date range of 1–93 days")
        if self.end > datetime.now(UTC).date() or self.start < date(2000, 1, 1):
            raise ValueError("Choose dates between 2000 and today (UTC)")
        return self

    @property
    def previous_start(self):
        return self.start - timedelta(days=(self.end - self.start).days + 1)


def build_query(config, filters, prices):
    # JSON-encoded literals prevent user-controlled filters/rates becoming KQL syntax.
    rates = {p["model"]: [p["input_per_million"], p["output_per_million"]] for p in prices}
    cuts = []
    api_scope = f"== {json.dumps(config['api_id'])}"
    if config.get("reporting_apis"):
        api_scope = f"in (dynamic({json.dumps([p['api_id'] for p in config['reporting_apis']])}))"
    for field, values in (
        ("model", filters.model_ids),
        ("group_id", filters.group_ids),
        ("principal_id", filters.member_ids),
    ):
        if values:
            cuts.append(f"| where {field} in (dynamic({json.dumps(values)}))")
    group = {"model": "model", "group": "group_id", "member": "principal_id"}[filters.grouping]
    aggregate = """input_tokens=sum(input_tokens), output_tokens=sum(output_tokens), cached_tokens=sum(cached_tokens), usd=sum(usd), unpriced_tokens=sum(unpriced_tokens), unattributed_tokens=sum(unattributed_tokens), missing_models=make_set_if(model, unpriced_tokens > 0, 100)"""
    return f"""let rates = dynamic({json.dumps(rates)});
let usage = materialize(customMetrics
| where timestamp >= datetime({filters.previous_start.isoformat()}) and timestamp < datetime({(filters.end + timedelta(days=1)).isoformat()})
| where tostring(customDimensions["API ID"]) {api_scope} and tostring(customDimensions["Service ID"]) == {json.dumps(config["apim_name"])}
| where name in ("Prompt Tokens", "Completion Tokens", "Prompt Cached Tokens")
| extend model=tostring(customDimensions.Model), group_id=tostring(customDimensions["Group ID"]), principal_id=tostring(customDimensions["Principal ID"])
| extend model=iff(isempty(model), "{UNATTRIBUTED}", model), group_id=iff(isempty(group_id), "{UNATTRIBUTED}", group_id), principal_id=iff(isempty(principal_id), "{UNATTRIBUTED}", principal_id)
{" ".join(cuts)}
| summarize input_tokens=sumif(coalesce(valueSum, value), name == "Prompt Tokens"), output_tokens=sumif(coalesce(valueSum, value), name == "Completion Tokens"), cached_tokens=sumif(coalesce(valueSum, value), name == "Prompt Cached Tokens") by day=bin(timestamp, 1d), model, group_id, principal_id
| extend period=iff(day >= datetime({filters.start.isoformat()}), "current", "previous"), rate=rates[model]
| extend usd=iff(isnull(rate), 0.0, (input_tokens * todouble(rate[0]) + output_tokens * todouble(rate[1])) / 1000000.0), unpriced_tokens=iff(isnull(rate), input_tokens + output_tokens, 0.0), unattributed_tokens=iff(group_id == "{UNATTRIBUTED}" or principal_id == "{UNATTRIBUTED}", input_tokens + output_tokens, 0.0));
let breakdown = usage | summarize {aggregate} by period, key={group} | extend tokens=input_tokens + output_tokens;
let selected = breakdown | summarize ranking=sum(tokens) by key | top {BREAKDOWN_LIMIT + 1} by ranking desc;
union
(usage | summarize {aggregate} by period | project bucket="totals", data=pack_all()),
(usage | summarize {aggregate} by period, key=format_datetime(day, "yyyy-MM-dd") | project bucket="daily", data=pack_all()),
(breakdown | join kind=inner selected on key | order by ranking desc, key asc | project bucket="breakdown", data=pack_all())"""


def cost_analysis(app_id, credential, config, filters, prices):
    result = {
        "status": "not_configured" if not app_id else "connected",
        "start": filters.start.isoformat(),
        "end": filters.end.isoformat(),
        "previous_start": filters.previous_start.isoformat(),
        "previous_end": (filters.start - timedelta(days=1)).isoformat(),
        "grouping": filters.grouping,
        "generated_at": datetime.now(UTC).isoformat(),
        "totals": [],
        "daily": [],
        "breakdown": [],
        "truncated": False,
        "breakdown_limit": BREAKDOWN_LIMIT,
    }
    if not app_id:
        return result
    token = credential.get_token("https://api.applicationinsights.io/.default").token
    req = Request(
        f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        data=json.dumps({"query": build_query(config, filters, prices)}).encode(),
    )
    with urlopen(req, timeout=45) as response:
        data = json.load(response)
    if data.get("error") or not data.get("tables"):
        raise ValueError("Application Insights returned an incomplete result")
    table = data["tables"][0]
    columns = [c["name"] for c in table["columns"]]
    retained_keys = set()
    for raw in table["rows"]:
        row = dict(zip(columns, raw, strict=True))
        bucket = row["bucket"]
        if bucket not in {"totals", "daily", "breakdown"}:
            continue
        item = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        if bucket == "breakdown":
            if item["key"] not in retained_keys:
                if len(retained_keys) >= BREAKDOWN_LIMIT:
                    result["truncated"] = True
                    continue
                retained_keys.add(item["key"])
        result[bucket].append(item)
    return result
