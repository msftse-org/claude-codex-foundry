"""Cache-aware reporting and read-only, scoped calendar-month ledger charges."""

import ast
import copy
import io
import json
import math
import operator
import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from claude_foundry.budget_gateway import policy_xml
from claude_foundry.portal.app import create_app
from claude_foundry.portal.budget_ledger import AzureRows, Ledger, MemoryRows, policy_digest
from claude_foundry.portal.costs import (
    CostQuery,
    build_query,
    calendar_months,
    planning_prices,
    reporting_context,
)
from claude_foundry.portal.domain import seed
from claude_foundry.portal.service import telemetry
from claude_foundry.price_catalog import price_for


def published_price(model, sku="GlobalStandard"):
    price, reason = price_for(
        model,
        {
            "sku": {"name": sku},
            "properties": {
                "model": {"name": model, "version": "fixture"},
                "provisioningState": "Succeeded",
                "versionUpgradeOption": "NoAutoUpgrade",
            },
        },
    )
    assert reason is None
    return price


@pytest.mark.parametrize(
    "model,sku,provider,rates",
    [
        ("gpt-6-astra", "DataZoneStandard", "openai", (11, 1.1, 55)),
        ("claude-opus-5-5-fast", "GlobalStandard", "anthropic", (8, 0.4, 40)),
    ],
)
def test_planning_rates_use_verified_catalog_provider_and_cache(model, sku, provider, rates):
    price = planning_prices({"budget_prices": [published_price(model, sku)]})[0]
    assert price["provider"] == provider
    assert (
        price["input_per_million"],
        price["cache_read_per_million"],
        price["output_per_million"],
    ) == rates


def test_query_prices_cache_categories_and_reports_missing_rates(org):
    query = build_query(org, CostQuery(start="2024-03-01", end="2024-03-02"), [])
    assert "priced_cached_tokens" in query
    assert "cache_rate_missing_models" in query


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_governed_metric_uses_the_validated_token_oid(provider):
    xml = ET.fromstring(
        policy_xml(
            "00000000-0000-0000-0000-000000000001",
            "https://synthetic-budget.azurewebsites.net",
            "api://00000000-0000-0000-0000-000000000002",
            provider,
        )
    )
    header = xml.find(".//set-header[@name='x-budget-principal-id']/value")
    dimension = xml.find(".//llm-emit-token-metric/dimension[@name='Principal ID']")
    assert dimension is not None
    assert dimension.attrib["value"] == header.text


def kql_value(expression, values):
    """Evaluate the generated arithmetic subset offline, not a second pricing formula."""
    calls = {
        "min_of": min,
        "max_of": max,
        "isnull": lambda value: value is None,
        "isempty": lambda value: not value,
        "isfinite": lambda value: value is not None and math.isfinite(value),
        "tostring": lambda value: "" if value is None else str(value),
        "todouble": lambda value: None if value is None else float(value),
        "coalesce": lambda *items: next((v for v in items if v is not None), None),
    }
    binary = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
    }
    compare = {ast.Eq: operator.eq, ast.Gt: operator.gt}

    def evaluate(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.Subscript):
            value = evaluate(node.value)
            if isinstance(value, dict):
                return value.get(evaluate(node.slice))
            return None if value is None else value[evaluate(node.slice)]
        if isinstance(node, ast.BinOp):
            return binary[type(node.op)](evaluate(node.left), evaluate(node.right))
        if isinstance(node, ast.BoolOp):
            items = [evaluate(value) for value in node.values]
            return all(items) if isinstance(node.op, ast.And) else any(items)
        if isinstance(node, ast.Compare):
            assert len(node.ops) == 1
            return compare[type(node.ops[0])](evaluate(node.left), evaluate(node.comparators[0]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "iff":
                return evaluate(node.args[1] if evaluate(node.args[0]) else node.args[2])
            return calls[node.func.id](*(evaluate(arg) for arg in node.args))
        raise AssertionError(f"Unexpected query arithmetic: {ast.dump(node)}")

    return evaluate(ast.parse(expression, mode="eval").body)


def query_estimate(query, model, input_tokens, cached_tokens, output_tokens):
    rates = json.loads(query.split("let rates = dynamic(", 1)[1].split(");\n", 1)[0])
    values = {
        "model": model,
        "providers": json.loads(query.split("let providers = dynamic(", 1)[1].split(");\n", 1)[0]),
        "rate": rates.get(model),
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
    }
    prefixes = ("input_tokens=", "provider=", "priced_cached_tokens=", "unpriced=", "usd=")
    for line in query.splitlines():
        if line.startswith("| extend ") and line[len("| extend ") :].startswith(prefixes):
            for binding in re.split(r", (?=\w+=)", line[len("| extend ") :]):
                name, expression = binding.split("=", 1)
                values[name] = kql_value(expression, values)
    return values


@pytest.mark.parametrize(
    "model,sku,input_tokens,cached_tokens,output_tokens,expected,old",
    [
        (
            "gpt-6-astra",
            "DataZoneStandard",
            53_868_980,
            52_249_118,
            203_935,
            86.5089368,
            603.775205,
        ),
        ("claude-opus-5-5-fast", "GlobalStandard", 89_065, 65_576_660, 158_085, 33.266584, 7.03592),
    ],
)
def test_reviewed_fixtures_execute_the_generated_query_math(
    org, model, sku, input_tokens, cached_tokens, output_tokens, expected, old
):
    prices = planning_prices({"budget_prices": [published_price(model, sku)]})
    query = build_query(org, CostQuery(start="2026-10-01", end="2026-10-31"), prices)
    value = query_estimate(query, model, input_tokens, cached_tokens, output_tokens)
    assert value["usd"] == pytest.approx(expected)
    assert (
        input_tokens * prices[0]["input_per_million"]
        + output_tokens * prices[0]["output_per_million"]
    ) / 1_000_000 == pytest.approx(old)


@pytest.mark.parametrize(
    "provider,input_tokens,cached_tokens,output_tokens,cache_rate,expected",
    [
        ("openai", 100, 80, 10, 0.3, 0.000234),
        ("anthropic", 100, 80, 10, 0.3, 0.000474),
        ("openai", 100, 80, 10, None, 0.00045),
        ("anthropic", 100, 80, 10, None, 0.00069),
        ("openai", 100, 80, 10, 0, 0.00021),
        ("anthropic", 100, 80, 10, 0, 0.00045),
        ("openai", 100, 200, 10, 0.3, 0.00018),
        ("anthropic", 100, 200, 10, 0.3, 0.00051),
        ("openai", 0, 200, 0, None, 0),
        ("anthropic", 0, 200, 0, None, 0.0006),
        ("openai", -100, 20, -10, 0.3, 0),
        ("anthropic", -100, 20, -10, 0.3, 0.000006),
        ("anthropic", math.nan, 20, math.inf, 0.3, 0.000006),
        ("", 100, 20, 10, 0.3, 0),
        ("", 100, 0, 10, None, 0.00045),
    ],
)
def test_query_math_handles_missing_zero_and_inconsistent_metrics(
    org, provider, input_tokens, cached_tokens, output_tokens, cache_rate, expected
):
    prices = [
        {
            "model": "opaque-deployment",
            "provider": provider,
            "input_per_million": 3,
            "cache_read_per_million": cache_rate,
            "output_per_million": 15,
        }
    ]
    query = build_query(org, CostQuery(start="2024-03-01", end="2024-03-02"), prices)
    value = query_estimate(query, "opaque-deployment", input_tokens, cached_tokens, output_tokens)
    assert value["usd"] == pytest.approx(expected)
    assert value["unpriced"] == (not provider and cached_tokens > 0)
    assert "isnull(rate[2]) and cached_tokens > 0" in query


def test_query_marks_cache_only_usage_without_prices_incomplete(org):
    query = build_query(org, CostQuery(start="2024-03-01", end="2024-03-02"), [])
    value = query_estimate(query, "unpriced", 0, 200, 0)
    assert value["usd"] == 0
    assert value["total_tokens"] == 200 and value["unpriced"]


def test_unpriced_model_still_uses_provider_metadata_for_token_coverage(org):
    query = build_query(
        {**org, "models": [{"name": "opaque", "provider": "codex"}]},
        CostQuery(start="2024-03-01", end="2024-03-02"),
        [],
    )
    value = query_estimate(query, "opaque", 100, 80, 10)
    assert value["provider"] == "openai"
    assert value["total_tokens"] == 110 and value["unpriced"]
    assert value["usd"] == 0


@pytest.mark.parametrize("override", [0, 0.07, None])
def test_owner_cache_override_wins_without_mutating_state(override):
    state = {
        "prices": [
            {
                "model": "gpt-6-astra",
                "input_per_million": 7,
                "output_per_million": 19,
                "cache_read_per_million": override,
                "provider": "anthropic",
            }
        ],
        "budget_prices": [published_price("gpt-6-astra", "DataZoneStandard")],
        "models": [{"name": "gpt-6-astra", "provider": "claude"}],
    }
    before = copy.deepcopy(state)
    price = planning_prices(state)[0]
    assert price["input_per_million"] == 7 and price["output_per_million"] == 19
    assert price["cache_read_per_million"] == (1.1 if override is None else override)
    assert price["provider"] == "openai"
    assert state == before


@pytest.mark.parametrize(
    "metadata,provider",
    [
        ({"provider": "codex"}, "openai"),
        ({"provider": "claude"}, "anthropic"),
        ({"format": "OpenAI"}, "openai"),
        ({"provider": None, "format": "OpenAI"}, "openai"),
        ({}, None),
    ],
)
def test_provider_uses_model_metadata_not_names_or_token_counts(metadata, provider):
    price = planning_prices(
        {
            "prices": [
                {
                    "model": "claude-looks-like-a-name",
                    "input_per_million": 3,
                    "output_per_million": 15,
                }
            ],
            "models": [{"name": "claude-looks-like-a-name", **metadata}],
        }
    )[0]
    assert price.get("provider") == provider
    assert "cache_read_per_million" not in price


def test_both_token_queries_map_published_principals_before_filtering(org, monkeypatch):
    config = {
        **org,
        "principal_groups": {"synthetic-member": "published-team", 'quoted"member': "team"},
        "token_member_ids": ["synthetic-member"],
    }
    filters = CostQuery(start="2024-03-01", end="2024-03-02", group_ids=["published-team"])
    cost_query = build_query(config, filters, [])
    captured = []

    def response(request, **kwargs):
        captured.append(json.loads(request.data)["query"])
        return io.BytesIO(
            json.dumps(
                {
                    "tables": [{"columns": [{"name": "bucket"}, {"name": "data"}], "rows": []}],
                }
            ).encode()
        )

    monkeypatch.setattr("claude_foundry.portal.service.urlopen", response)
    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="synthetic"))
    telemetry("synthetic-app", credential, config)
    for query in [cost_query, captured[0]]:
        assert (
            f"group_id=tostring(dynamic({json.dumps(config['principal_groups'])})[principal_id])"
            in query
        )
        assert 'group_id=iff(isempty(group_id), "__unattributed__", group_id)' in query
        assert 'principal_id in (dynamic(["synthetic-member"]))' in query
        assert "by day=bin(timestamp, 1d), model, group_id, principal_id" in query
    assert cost_query.index("group_id=tostring(dynamic(") < cost_query.index("| where group_id in")


def test_memory_month_rows_are_full_read_only_copies():
    rows = MemoryRows()
    fixtures = {
        "2026-11-org": {"spent": 3_199_344, "reserved": 4_728_770},
        "2026-11-group-synthetic": {"spent": 99, "reserved": 5},
        "2026-11-user-synthetic": {"spent": 99, "reserved": 5},
        "2026-10-org": {"spent": 1, "reserved": 0},
        "request-synthetic": {"state": "reserved"},
    }
    rows.commit([(key, value, None) for key, value in fixtures.items()])
    before, version = copy.deepcopy(rows.rows), rows.version
    result = rows.month_counters("2026-11")
    assert set(result) == {key for key in fixtures if key.startswith("2026-11-")}
    assert result["2026-11-org"] == {"spent": 3_199_344, "reserved": 4_728_770}
    result["2026-11-org"]["spent"] = 0
    assert rows.rows == before and rows.version == version


def test_azure_month_rows_use_exact_partition_and_prefix_without_truncation():
    rows = object.__new__(AzureRows)
    rows.partition = "00000000-0000-0000-0000-000000000001"
    calls = []

    def query(query_filter, parameters):
        calls.append((query_filter, parameters))
        return iter(
            [
                {"RowKey": f"2026-11-user-{i}", "data": json.dumps({"spent": i, "reserved": 4})}
                for i in range(120)
            ]
        )

    rows.table = SimpleNamespace(query_entities=query)
    assert len(rows.month_counters("2026-11")) == 120
    assert calls == [
        (
            "PartitionKey eq @organization and RowKey ge @start and RowKey lt @end",
            {"organization": rows.partition, "start": "2026-11-", "end": "2026-11."},
        )
    ]


@pytest.mark.parametrize(
    "month", ["2026-1", "2026-00", "2026-13", "2026-11' or true", "2026-11-org"]
)
@pytest.mark.parametrize("adapter", [MemoryRows, AzureRows])
def test_month_prefix_rejects_non_calendar_values(month, adapter):
    rows = object.__new__(adapter)
    with pytest.raises(ValueError):
        rows.month_counters(month)


@pytest.mark.parametrize(
    "start,end,months",
    [
        ("2024-02-29", "2024-02-29", ["2024-02"]),
        ("2024-02-29", "2024-03-01", ["2024-02", "2024-03"]),
        ("2023-12-31", "2024-01-01", ["2023-12", "2024-01"]),
        ("2023-12-31", "2024-03-01", ["2023-12", "2024-01", "2024-02", "2024-03"]),
    ],
)
def test_months_are_utc_inclusive_across_leap_days_and_years(start, end, months):
    assert list(calendar_months(date.fromisoformat(start), date.fromisoformat(end))) == months


ROLES = ("owner", "admin", "billing", "auditor", "group_admin", "developer")
GROUP = "00000000-0000-0000-0000-000000000101"
OTHER_GROUP = "00000000-0000-0000-0000-000000000102"
OWNER = "00000000-0000-0000-0000-000000000003"
OTHER_MEMBER = "00000000-0000-0000-0000-000000000999"


@pytest.fixture
def reporting_app(org, monkeypatch):
    monkeypatch.setenv("PORTAL_OWNER_ID", OWNER)
    monkeypatch.delenv("PORTAL_INSIGHTS_APP_ID", raising=False)
    monkeypatch.delenv("BUDGET_STORAGE_ACCOUNT", raising=False)
    models = [
        {"name": "gpt-6-astra", "provider": "codex", "labelOverride": "Synthetic GPT"},
        {"name": "claude-opus-5-5-fast", "provider": "claude", "labelOverride": "Synthetic Claude"},
    ]
    config = {**org, "models": models}
    state = seed(config, OWNER)
    template = state["members"][0]
    members = {
        role: {
            **template,
            "id": OWNER if role == "owner" else f"00000000-0000-0000-0000-{i + 10:012d}",
            "role": role,
            "name": f"Synthetic {role}",
            "group_id": GROUP,
        }
        for i, role in enumerate(ROLES)
    }
    state["members"] = list(members.values()) + [
        {
            **template,
            "id": OTHER_MEMBER,
            "role": "developer",
            "group_id": OTHER_GROUP,
            "name": "Outside member",
            "email": "outside@example.test",
        }
    ]
    group = {**state["groups"][0], "spend_ceiling_usd": "500"}
    state["groups"] = [
        {**group, "id": GROUP, "name": "Synthetic team"},
        {**group, "id": OTHER_GROUP, "name": "Outside team"},
    ]
    state["settings"].update(budget_enforcement=True, spend_limit_usd="1000")
    state["budget_prices"] = [
        published_price("gpt-6-astra", "DataZoneStandard"),
        published_price("claude-opus-5-5-fast"),
    ]
    policy = {
        key: copy.deepcopy(state[key]) for key in ("settings", "groups", "members", "budget_prices")
    }
    ledger = Ledger(MemoryRows())
    ledger.activate(policy)
    policies = {policy_digest(policy): policy}
    snapshots = SimpleNamespace(get=lambda digest: copy.deepcopy(policies[digest]))
    store = SimpleNamespace(read=lambda: (copy.deepcopy(state), "synthetic-etag"))

    def identity(self, authorization):
        if not authorization:
            raise HTTPException(401, "Sign in required")
        return authorization.removeprefix("Bearer ")

    monkeypatch.setattr("claude_foundry.portal.auth.Authenticator.identity", identity)
    app = create_app(
        config, store=store, credential=object(), local=False, budget=(ledger, snapshots)
    )
    for month in ["2026-10", "2026-11"]:
        counters = {
            "org": (3_199_344, 4_728_770),
            "group-" + GROUP: (1_200_000, 2_000_000),
            "group-" + OTHER_GROUP: (1_999_344, 2_728_770),
            "user-" + OWNER: (200_000, 500_000),
            "user-" + members["developer"]["id"]: (1_000_000, 1_500_000),
            "user-" + OTHER_MEMBER: (1_999_344, 2_728_770),
        }
        ledger.rows.commit(
            [
                (month + "-" + scope, {"spent": spent, "reserved": held}, None)
                for scope, (spent, held) in counters.items()
            ]
        )
    calls = []

    def token_rows(query_config):
        allowed = query_config["token_member_ids"]
        return [
            {
                "model": "gpt-6-astra",
                "principal_id": m["id"],
                "group_id": m["group_id"],
                "input_tokens": 100,
                "cached_tokens": 80,
                "output_tokens": 10,
            }
            for m in policy["members"]
            if allowed is None or m["id"] in allowed
        ]

    def usage(app_id, credential, query_config, days):
        calls.append(("usage", copy.deepcopy(query_config)))
        return {
            "status": "connected",
            "month_tokens": token_rows(query_config),
            "daily": [{"requests": 9000}],
            "models": [{"key": "all-models", "requests": 9000}],
            "people": [{"key": OTHER_MEMBER, "name": "Outside member"}],
            "requests": [{"principal_id": OTHER_MEMBER, "name": "Outside member"}],
        }

    def costs(app_id, credential, query_config, filters, prices):
        calls.append(("costs", copy.deepcopy(query_config), prices))
        tokens = [
            row
            for row in token_rows(query_config)
            if (not filters.group_ids or row["group_id"] in filters.group_ids)
            and (not filters.member_ids or row["principal_id"] in filters.member_ids)
        ]
        return {
            "status": "connected",
            "totals": [
                {"period": "current", "input_tokens": sum(row["input_tokens"] for row in tokens)}
            ],
            "breakdown": [{"key": row["principal_id"]} for row in tokens],
            "daily": [],
        }

    monkeypatch.setattr("claude_foundry.portal.app.telemetry", usage)
    monkeypatch.setattr("claude_foundry.portal.app.cost_analysis", costs)
    return SimpleNamespace(
        app=app,
        state=state,
        policy=policy,
        models=models,
        members=members,
        ledger=ledger,
        snapshots=snapshots,
        policies=policies,
        calls=calls,
        config=config,
        store=store,
    )


def report(client, endpoint, member, **filters):
    headers = {"Authorization": "Bearer " + member["id"]}
    if endpoint == "costs":
        return client.post(
            "/api/cost-analysis",
            headers=headers,
            json={
                "start": "2026-10-31",
                "end": "2026-11-01",
                **filters,
            },
        )
    return client.get("/api/telemetry?days=30", headers=headers)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("endpoint", ["costs", "usage"])
def test_both_reports_return_full_month_counters_for_every_role(
    reporting_app, role, endpoint, monkeypatch
):
    fixture = reporting_app

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 1, 0, 0, tzinfo=UTC)

    monkeypatch.setattr("claude_foundry.portal.app.datetime", Clock)
    before, version = copy.deepcopy(fixture.ledger.rows.rows), fixture.ledger.rows.version
    with TestClient(fixture.app) as client:
        response = report(client, endpoint, fixture.members[role])
    assert response.status_code == 200, response.text
    payload = response.json()
    ledger = payload["ledger_charges"]
    assert ledger["configured"] is True and ledger["status"] == "connected"
    assert [month["month"] for month in ledger["months"]] == ["2026-10", "2026-11"]
    for month in ledger["months"]:
        scopes = {row["scope"]: row for row in month["scopes"]}
        if role == "developer":
            assert set(scopes) == {"user-" + fixture.members[role]["id"]}
            assert scopes["user-" + fixture.members[role]["id"]]["spent_micro_usd"] == 1_000_000
            assert scopes["user-" + fixture.members[role]["id"]]["reserved_micro_usd"] == 1_500_000
        elif role == "group_admin":
            assert set(scopes) == {"group-" + GROUP} | {
                "user-" + member["id"] for member in fixture.members.values()
            }
            assert scopes["group-" + GROUP]["spent_micro_usd"] == 1_200_000
            assert scopes["group-" + GROUP]["reserved_micro_usd"] == 2_000_000
        else:
            assert scopes["org"]["spent_micro_usd"] == 3_199_344
            assert scopes["org"]["reserved_micro_usd"] == 4_728_770
            assert "group-" + OTHER_GROUP in scopes and "user-" + OTHER_MEMBER in scopes
    if role in {"developer", "group_admin"}:
        for forbidden in [
            OTHER_MEMBER,
            OTHER_GROUP,
            "Outside member",
            "Outside team",
            "3199344",
            "4728770",
        ]:
            assert forbidden not in response.text
        if endpoint == "usage":
            assert all(payload[key] == [] for key in ("daily", "models", "people", "requests"))
        allowed = fixture.calls[-1][1]["token_member_ids"]
        assert fixture.members[role]["id"] in allowed and OTHER_MEMBER not in allowed
    else:
        assert fixture.calls[-1][1]["token_member_ids"] is None
    assert fixture.ledger.rows.rows == before and fixture.ledger.rows.version == version


@pytest.mark.parametrize("role", ["developer", "group_admin"])
@pytest.mark.parametrize("endpoint", ["costs", "usage"])
def test_unpublished_membership_moves_fail_closed_without_old_team_leaks(
    reporting_app, role, endpoint
):
    fixture = reporting_app
    member = fixture.members[role]
    member["group_id"] = OTHER_GROUP
    with TestClient(fixture.app) as client:
        response = report(client, endpoint, member)
    assert response.status_code == 200
    assert response.json()["ledger_charges"] == {
        "configured": True,
        "status": "pending_publication",
        "currency": "USD",
        "months": [],
        "note": "Membership changes await budget publication.",
    }
    assert fixture.calls[-1][1]["token_member_ids"] == []
    assert "3199344" not in response.text and "Outside member" not in response.text


@pytest.mark.parametrize("role", ["developer", "group_admin"])
@pytest.mark.parametrize("endpoint", ["costs", "usage"])
def test_unpublished_reactivation_cannot_bypass_published_scope(reporting_app, role, endpoint):
    fixture = reporting_app
    member = fixture.members[role]
    policy = copy.deepcopy(fixture.policy)
    next(m for m in policy["members"] if m["id"] == member["id"])["status"] = "suspended"
    digest = policy_digest(policy)
    fixture.policies[digest] = policy
    _, etag = fixture.ledger.rows.read("policy")
    fixture.ledger.rows.commit([("policy", {"digest": digest}, etag)])
    with TestClient(fixture.app) as client:
        response = report(client, endpoint, member)
    assert response.status_code == 200
    assert response.json()["ledger_charges"]["status"] == "pending_publication"
    assert response.json()["ledger_charges"]["months"] == []
    assert fixture.calls[-1][1]["token_member_ids"] == []
    assert "3199344" not in response.text and OTHER_MEMBER not in response.text


@pytest.mark.parametrize("endpoint", ["costs", "usage"])
def test_warm_cache_cannot_reuse_owner_scope_after_downgrade(reporting_app, endpoint):
    fixture = reporting_app
    owner = fixture.members["owner"]
    with TestClient(fixture.app) as client:
        assert report(client, endpoint, owner).status_code == 200
        owner["role"] = "developer"
        response = report(client, endpoint, owner)
    assert response.status_code == 200
    assert fixture.calls[-1][1]["token_member_ids"] == [OWNER]
    assert OTHER_MEMBER not in response.text and "3199344" not in response.text
    assert all(
        [row["scope"] for row in month["scopes"]] == ["user-" + OWNER]
        for month in response.json()["ledger_charges"]["months"]
    )


def test_group_scope_intersects_published_and_current_membership(reporting_app):
    fixture = reporting_app
    fixture.members["developer"]["group_id"] = OTHER_GROUP
    with TestClient(fixture.app) as client:
        response = report(
            client,
            "costs",
            fixture.members["group_admin"],
            member_ids=[OTHER_MEMBER, fixture.members["developer"]["id"]],
            group_ids=[OTHER_GROUP],
        )
    assert response.status_code == 200
    assert response.json()["totals"][0]["input_tokens"] == 0
    assert fixture.members["developer"]["id"] not in fixture.calls[-1][1]["token_member_ids"]
    assert OTHER_MEMBER not in response.text


def test_reporting_uses_published_prices_not_owner_drafts(reporting_app):
    fixture = reporting_app
    fixture.state["budget_prices"][0]["rates"]["cache_read"] = "999"
    context = reporting_context(
        fixture.state, (fixture.ledger, fixture.snapshots), fixture.members["owner"], fixture.models
    )
    assert context["prices"][0]["cache_read_per_million"] == 1.1
    with TestClient(fixture.app) as client:
        response = report(client, "usage", fixture.members["owner"])
    assert response.json()["planning_prices"][0]["cache_read_per_million"] == 1.1


@pytest.mark.parametrize("endpoint", ["costs", "usage"])
@pytest.mark.parametrize("failure", ["policy", "snapshot", "counters"])
def test_ledger_runtime_failures_never_look_like_zero_charges(
    reporting_app, endpoint, failure, monkeypatch
):
    fixture = reporting_app

    def unavailable(*args):
        raise OSError("Synthetic storage failure")

    with TestClient(fixture.app) as client:
        assert report(client, endpoint, fixture.members["owner"]).status_code == 200
        if failure == "policy":
            monkeypatch.setattr(fixture.ledger.rows, "read", unavailable)
        elif failure == "snapshot":
            monkeypatch.setattr(fixture.snapshots, "get", unavailable)
        else:
            monkeypatch.setattr(fixture.ledger.rows, "month_counters", unavailable)
        response = report(client, endpoint, fixture.members["owner"])
    assert response.status_code == 503
    assert "no zero balance" in response.json()["detail"]
    assert "Synthetic storage failure" not in response.text


@pytest.mark.parametrize("endpoint", ["costs", "usage"])
def test_missing_ledger_configuration_preserves_estimate_only_response(reporting_app, endpoint):
    fixture = reporting_app
    app = create_app(fixture.config, store=fixture.store, credential=object(), local=False)
    with TestClient(app) as client:
        response = report(client, endpoint, fixture.members["owner"])
    assert response.status_code == 200
    assert response.json()["ledger_charges"] == {
        "configured": False,
        "status": "not_configured",
        "currency": "USD",
        "months": [],
    }


def test_no_published_policy_is_explicit_absence_not_zero(reporting_app):
    fixture = reporting_app
    fixture.ledger.rows.rows.pop("policy")
    with TestClient(fixture.app) as client:
        response = report(client, "costs", fixture.members["owner"])
    ledger = response.json()["ledger_charges"]
    assert ledger["configured"] and ledger["status"] == "not_published"
    assert ledger["months"] == [] and "published" in ledger["note"]


def test_ledger_reads_stay_fresh_even_when_token_query_is_cached(reporting_app):
    fixture = reporting_app
    with TestClient(fixture.app) as client:
        before = report(client, "costs", fixture.members["owner"]).json()
        counter, etag = fixture.ledger.rows.read("2026-11-org")
        fixture.ledger.rows.commit([("2026-11-org", {**counter, "spent": 4_000_000}, etag)])
        after = report(client, "costs", fixture.members["owner"]).json()
    assert before["ledger_charges"] != after["ledger_charges"]
    assert len(fixture.calls) == 1
    november = after["ledger_charges"]["months"][1]
    assert (
        next(row for row in november["scopes"] if row["scope"] == "org")["spent_micro_usd"]
        == 4_000_000
    )


def test_published_mapping_changes_invalidate_token_cache(reporting_app):
    fixture = reporting_app
    with TestClient(fixture.app) as client:
        report(client, "costs", fixture.members["owner"])
        next_policy = copy.deepcopy(fixture.policy)
        next_policy["members"][-1]["group_id"] = GROUP
        digest = policy_digest(next_policy)
        fixture.policies[digest] = next_policy
        _, etag = fixture.ledger.rows.read("policy")
        fixture.ledger.rows.commit([("policy", {"digest": digest}, etag)])
        report(client, "costs", fixture.members["owner"])
    assert len(fixture.calls) == 2
    assert fixture.calls[-1][1]["principal_groups"][OTHER_MEMBER] == GROUP


def test_monthly_ledger_is_not_filtered_by_model_or_partial_dates(reporting_app):
    fixture = reporting_app
    with TestClient(fixture.app) as client:
        response = report(
            client,
            "costs",
            fixture.members["owner"],
            model_ids=["unrelated-model"],
            member_ids=[OTHER_MEMBER],
        )
    months = response.json()["ledger_charges"]["months"]
    assert len(months) == 2
    for month in months:
        org = next(row for row in month["scopes"] if row["scope"] == "org")
        assert org["spent_micro_usd"] == 3_199_344 and org["reserved_micro_usd"] == 4_728_770


@pytest.mark.parametrize("endpoint", ["costs", "usage"])
def test_endpoints_include_both_sides_of_utc_year_boundary(reporting_app, endpoint, monkeypatch):
    fixture = reporting_app

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 0, 0, tzinfo=UTC)

    monkeypatch.setattr("claude_foundry.portal.app.datetime", Clock)
    fixture.ledger.rows.commit(
        [
            ("2025-12-org", {"spent": 1_234_567, "reserved": 89}, None),
            ("2026-01-org", {"spent": 42, "reserved": 123}, None),
        ]
    )
    with TestClient(fixture.app) as client:
        response = report(
            client,
            endpoint,
            fixture.members["owner"],
            start="2025-12-31",
            end="2026-01-01",
        )
    assert response.status_code == 200
    months = response.json()["ledger_charges"]["months"]
    assert [month["month"] for month in months] == ["2025-12", "2026-01"]
    assert [
        next(row for row in month["scopes"] if row["scope"] == "org")["spent_micro_usd"]
        for month in months
    ] == [1_234_567, 42]


@pytest.mark.parametrize("endpoint", ["costs", "usage"])
def test_reporting_still_requires_active_portal_membership(reporting_app, endpoint):
    fixture = reporting_app
    member = fixture.members["developer"]
    member["status"] = "suspended"
    with TestClient(fixture.app) as client:
        assert report(client, endpoint, member).status_code == 403
