"""Bounded, read-only cost exploration over APIM token metrics.

Rates are planning rates, never presented as an Azure invoice.
All totals are aggregated in Kusto before breakdowns are limited.
"""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UNATTRIBUTED = "__unattributed__"
BREAKDOWN_LIMIT = 2500


def model_providers(state):
    aliases = {
        "anthropic": "anthropic",
        "openai": "openai",
        "claude": "anthropic",
        "codex": "openai",
    }
    providers = {}
    for model in state.get("models", []):
        provider = aliases.get(str(model.get("provider") or model.get("format") or "").lower())
        if provider:
            providers[model["name"]] = provider
    for price in state.get("budget_prices", []):
        provider = aliases.get(str(price.get("provider", "")).lower())
        if provider:
            providers[price["model"]] = provider
    return providers


def planning_prices(state):
    """Use explicit planning rates first, then reviewed list rates or ceilings."""
    prices = [dict(p) for p in state.get("prices", [])]
    explicit = {p["model"] for p in prices}
    published = {p["model"]: p for p in state.get("budget_prices", [])}
    providers = model_providers(state)
    for value in published.values():
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
    for price in prices:
        published_price = published.get(price["model"], {})
        provider = providers.get(price["model"])
        if provider:
            price["provider"] = provider
        else:
            price.pop("provider", None)
        if price.get("cache_read_per_million") is None:
            cache_rate = (published_price.get("rates") or {}).get("cache_read")
            if cache_rate is not None:
                price["cache_read_per_million"] = float(cache_rate)
    return prices


def reporting_context(state, budget, viewer, models):
    """Use one published snapshot, intersected with the authenticated portal scope."""
    policy = None
    if budget is not None:
        meta, _ = budget[0].rows.read("policy")
        if meta.get("digest"):
            policy = budget[1].get(meta["digest"])
    pricing_state = {
        **state,
        "models": models,
        "budget_prices": (policy or {}).get("budget_prices", []),
    }
    context = {
        "prices": planning_prices(pricing_state),
        "model_providers": model_providers(pricing_state),
        "principal_groups": (
            {m["id"]: m["group_id"] for m in (policy or {}).get("members", [])}
            if budget is not None
            else None
        ),
        "token_member_ids": None,
        "scope_ids": None,
        "known_scopes": ["org"],
        "status": "connected" if policy else "not_published",
    }
    if policy:
        context["known_scopes"] += ["group-" + g["id"] for g in policy["groups"]]
        context["known_scopes"] += ["user-" + m["id"] for m in policy["members"]]
    if budget is not None and viewer["role"] in {"developer", "group_admin"}:
        current = {m["id"]: m for m in state["members"]}
        published = {m["id"]: m for m in (policy or {}).get("members", [])}
        matching = {
            oid
            for oid, member in current.items()
            if oid in published
            and published[oid]["status"] == "active"
            and member["group_id"] == published[oid]["group_id"]
        }
        if viewer["id"] not in matching:
            identifiers, scopes = [], []
            if policy:
                context["status"] = "pending_publication"
        elif viewer["role"] == "developer":
            identifiers, scopes = [viewer["id"]], ["user-" + viewer["id"]]
        else:
            identifiers = sorted(
                oid for oid in matching if current[oid]["group_id"] == viewer["group_id"]
            )
            scopes = ["group-" + viewer["group_id"]] + ["user-" + oid for oid in identifiers]
        context.update(token_member_ids=identifiers, scope_ids=scopes)
    return context


def calendar_months(start, end):
    """Inclusive UTC dates select whole calendar months, never prorated counters."""
    month = start.replace(day=1)
    while month <= end:
        yield month.strftime("%Y-%m")
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)


def monthly_charges(budget, context, start, end):
    result = {
        "configured": budget is not None,
        "status": context["status"] if budget is not None else "not_configured",
        "currency": "USD",
        "months": [],
    }
    if budget is None:
        return result
    if context["status"] != "connected":
        result["note"] = (
            "Membership changes await budget publication."
            if context["status"] == "pending_publication"
            else "A published budget policy is required to read calendar-month charges."
        )
        return result
    for month in calendar_months(start, end):
        counters = budget[0].rows.month_counters(month)
        values = {key[len(month) + 1 :]: value for key, value in counters.items()}
        scopes = context["scope_ids"]
        if scopes is None:
            scopes = sorted(
                set(context["known_scopes"])
                | {
                    scope
                    for scope in values
                    if scope == "org" or scope.startswith(("group-", "user-"))
                }
            )
        result["months"].append(
            {
                "month": month,
                "scopes": [
                    {
                        "scope": scope,
                        "spent_micro_usd": values.get(scope, {}).get("spent", 0),
                        "reserved_micro_usd": values.get(scope, {}).get("reserved", 0),
                    }
                    for scope in scopes
                ],
            }
        )
    return result


def token_dimensions(config):
    """Published membership attributes governed metrics; unknown principals stay unknown."""
    query = """| extend model=tostring(customDimensions.Model), group_id=tostring(customDimensions["Group ID"]), principal_id=tostring(customDimensions["Principal ID"])"""
    if config.get("principal_groups") is not None:
        query += f"""
| extend group_id=tostring(dynamic({json.dumps(config["principal_groups"])})[principal_id])"""
    query += f"""
| extend model=iff(isempty(model), "{UNATTRIBUTED}", model), group_id=iff(isempty(group_id), "{UNATTRIBUTED}", group_id), principal_id=iff(isempty(principal_id), "{UNATTRIBUTED}", principal_id)"""
    if config.get("token_member_ids") is not None:
        query += f"""
| where principal_id in (dynamic({json.dumps(config["token_member_ids"])}))"""
    return query


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
    rates = {
        p["model"]: [
            p["input_per_million"],
            p["output_per_million"],
            p.get("cache_read_per_million"),
            p.get("provider", ""),
        ]
        for p in prices
    }
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
    aggregate = """input_tokens=sum(input_tokens), output_tokens=sum(output_tokens), cached_tokens=sum(cached_tokens), total_tokens=sum(total_tokens), usd=sum(usd), unpriced_tokens=sum(unpriced_tokens), unattributed_tokens=sum(unattributed_tokens), missing_models=make_set_if(model, isnull(rate) and total_tokens > 0, 100), cache_rate_missing_models=make_set_if(model, isnotnull(rate) and isnull(rate[2]) and cached_tokens > 0, 100), provider_missing_models=make_set_if(model, isempty(provider) and cached_tokens > 0, 100)"""
    providers = config.get("model_providers", model_providers(config))
    return f"""let rates = dynamic({json.dumps(rates)});
let providers = dynamic({json.dumps(providers)});
let usage = materialize(customMetrics
| where timestamp >= datetime({filters.previous_start.isoformat()}) and timestamp < datetime({(filters.end + timedelta(days=1)).isoformat()})
| where tostring(customDimensions["API ID"]) {api_scope} and tostring(customDimensions["Service ID"]) == {json.dumps(config["apim_name"])}
| where name in ("Prompt Tokens", "Completion Tokens", "Prompt Cached Tokens")
{token_dimensions(config)}
{" ".join(cuts)}
| summarize input_tokens=sumif(coalesce(valueSum, value), name == "Prompt Tokens"), output_tokens=sumif(coalesce(valueSum, value), name == "Completion Tokens"), cached_tokens=sumif(coalesce(valueSum, value), name == "Prompt Cached Tokens") by day=bin(timestamp, 1d), model, group_id, principal_id
| extend input_tokens=iff(isfinite(input_tokens), max_of(0.0, input_tokens), 0.0), output_tokens=iff(isfinite(output_tokens), max_of(0.0, output_tokens), 0.0), cached_tokens=iff(isfinite(cached_tokens), max_of(0.0, cached_tokens), 0.0)
| extend period=iff(day >= datetime({filters.start.isoformat()}), "current", "previous"), rate=rates[model]
| extend provider=iff(isempty(tostring(rate[3])), tostring(providers[model]), tostring(rate[3]))
| extend priced_cached_tokens=iff(provider == "openai", min_of(input_tokens, cached_tokens), cached_tokens), total_tokens=input_tokens + output_tokens + iff(provider == "openai", 0.0, cached_tokens)
| extend unpriced=isnull(rate) or (cached_tokens > 0 and isempty(provider))
| extend usd=iff(unpriced, 0.0, ((input_tokens - iff(provider == "openai", priced_cached_tokens, 0.0)) * todouble(rate[0]) + priced_cached_tokens * coalesce(todouble(rate[2]), todouble(rate[0])) + output_tokens * todouble(rate[1])) / 1000000.0)
| extend unpriced_tokens=iff(unpriced, total_tokens, 0.0), unattributed_tokens=iff(group_id == "{UNATTRIBUTED}" or principal_id == "{UNATTRIBUTED}", total_tokens, 0.0));
let breakdown = usage | summarize {aggregate} by period, key={group} | extend tokens=total_tokens;
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
