"""Offline held-reservation scope, gateway evidence and atomic release boundaries."""

import base64
import copy
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from claude_foundry.portal import gateway_outcome
from claude_foundry.portal.app import create_app
from claude_foundry.portal.auth import Authenticator
from claude_foundry.portal.budget_ledger import (
    PENDING_READ_LIMIT,
    AzureRows,
    Contended,
    Ledger,
    MemoryRows,
)
from claude_foundry.portal.domain import seed
from claude_foundry.portal.store import Conflict, LocalStore

OWNER = str(UUID(int=3))
OCTOBER = datetime(2026, 10, 15, 12, tzinfo=UTC)
NOVEMBER = datetime(2026, 11, 1, 12, tzinfo=UTC)
GATEWAY_AT = "2026-10-15T12:01:00Z"


def table_payload(rows):
    return {
        "tables": [
            {
                "columns": [
                    {"name": name}
                    for name in (
                        "request_id",
                        "gateway_role",
                        "records",
                        "result_code",
                        "timestamp",
                    )
                ],
                "rows": rows,
            }
        ]
    }


def gateway_record(request_id, status="429", *, role="test-apim", count=1, at=GATEWAY_AT):
    return [request_id, role, count, status, at]


@pytest.fixture
def holds(org, monkeypatch, tmp_path):
    data = seed(org, OWNER, "Test owner", "owner@example.test")
    data["settings"].update(budget_enforcement=True, spend_limit_usd="1000")
    data["groups"][0].update(
        id=str(UUID(int=100)),
        spend_ceiling_usd="1000",
        model_ids=["claude-sonnet-4-6"],
    )
    data["members"][0]["group_id"] = data["groups"][0]["id"]
    data["groups"].append({**data["groups"][0], "id": str(UUID(int=200)), "name": "Other team"})
    members = {"owner": data["members"][0]}
    for index, role in enumerate(("admin", "billing", "auditor", "group_admin", "developer"), 10):
        member = {
            **data["members"][0],
            "id": str(UUID(int=index)),
            "name": "Test " + role,
            "role": role,
        }
        members[role] = member
        data["members"].append(member)
    outsider = {
        **members["developer"],
        "id": str(UUID(int=20)),
        "group_id": data["groups"][1]["id"],
        "name": "Other team member",
    }
    data["members"].append(outsider)
    members["outsider"] = outsider
    store = LocalStore(tmp_path / "state.json", data)
    ledger = Ledger(MemoryRows())
    ledger.activate(data)
    credential = SimpleNamespace(
        get_token=Mock(return_value=SimpleNamespace(token="synthetic-monitoring-token"))
    )
    monkeypatch.setenv("PORTAL_OWNER_ID", OWNER)
    monkeypatch.setenv("PORTAL_INSIGHTS_APP_ID", "test-insights")
    monkeypatch.delenv("PORTAL_PROVIDER_CONFIGS_JSON", raising=False)
    monkeypatch.delenv("PORTAL_BUDGET_MODELS_JSON", raising=False)
    monkeypatch.delenv("PORTAL_CONTENT_LOGS_JSON", raising=False)

    def identity(self, authorization):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "A test identity is required")
        return authorization.removeprefix("Bearer ")

    monkeypatch.setattr(Authenticator, "identity", identity)
    app = create_app(org, store, credential, local=False, budget=(ledger, None))
    fixture = SimpleNamespace(
        app=app,
        ledger=ledger,
        store=store,
        policy=data,
        members=members,
        credential=credential,
        records=[],
        queries=[],
        next_id=1000,
    )

    def query(request, timeout):
        assert request.get_method() == "POST"
        assert request.full_url == "https://api.applicationinsights.io/v1/apps/test-insights/query"
        assert request.headers["Authorization"] == "Bearer synthetic-monitoring-token"
        assert timeout == 45
        fixture.queries.append(json.loads(request.data)["query"])
        return io.BytesIO(json.dumps(table_payload(fixture.records)).encode())

    monkeypatch.setattr(gateway_outcome, "urlopen", query)
    with TestClient(app) as client:
        fixture.client = client
        yield fixture


def auth(holds, role="owner"):
    return {"Authorization": "Bearer " + holds.members[role]["id"]}


def admit(holds, role="developer", amount=100_000, at=OCTOBER):
    holds.next_id += 1
    return holds.ledger.reserve(
        str(UUID(int=holds.next_id)),
        holds.members[role]["id"],
        "claude-sonnet-4-6",
        amount,
        holds.policy,
        "synthetic-fingerprint",
        at=at,
    )


def release(holds, row, role="owner", **body):
    return holds.client.post(
        "/api/budgets/release-unbilled",
        headers=auth(holds, role),
        json={
            "request_id": row["request_id"],
            "reason": "Verified synthetic gateway rejection",
            **body,
        },
    )


def counters(holds, row):
    return {key: holds.ledger.rows.read(key)[0] for key in row["counters"]}


def test_listing_pages_all_months_newest_first_and_totals_are_not_page_totals(holds):
    rows = [
        admit(holds, amount=index + 1, at=OCTOBER + timedelta(days=index)) for index in range(105)
    ]
    settled = admit(holds)
    holds.ledger.settle(settled["request_id"], 10)
    expected_totals = {}
    for row in rows:
        value = expected_totals.setdefault(row["month"], [0, 0])
        value[0] += row["reserved"]
        value[1] += 1
    cursor, found, sizes = None, [], []
    while True:
        params = {"cursor": cursor} if cursor else {}
        response = holds.client.get("/api/budgets/reservations", params=params, headers=auth(holds))
        assert response.status_code == 200
        page = response.json()
        assert {
            item["month"]: [item["held_micro_usd"], item["count"]] for item in page["totals"]
        } == expected_totals
        assert not page["read_limit_reached"]
        assert page["read_limit"] == 10_000
        sizes.append(len(page["items"]))
        found.extend(item["request_id"] for item in page["items"])
        for item in page["items"]:
            assert item["hold_reason"] is None and item["held_at"] is None
            assert not {"policy", "fingerprint", "counters", "charged"} & item.keys()
        cursor = page["next_cursor"]
        assert page["has_more"] is bool(cursor)
        if not cursor:
            break
    assert sizes == [50, 50, 5]
    assert found == [row["request_id"] for row in reversed(rows)]
    assert len(holds.queries) == 3
    assert holds.credential.get_token.call_count == 3


@pytest.mark.parametrize(
    "role", ["owner", "admin", "billing", "auditor", "group_admin", "developer", "outsider"]
)
def test_listing_preserves_scope_for_rows_totals_names_and_gateway_query(holds, role):
    rows = [
        admit(holds, "owner", amount=100),
        admit(holds, "developer", amount=200),
        admit(holds, "developer", amount=300, at=NOVEMBER),
        admit(holds, "admin", amount=400, at=NOVEMBER),
        admit(holds, "outsider", amount=50_000),
    ]
    viewer = holds.members[role]
    expected = [
        row
        for row in rows
        if (
            viewer["role"] not in {"group_admin", "developer"}
            or viewer["role"] == "group_admin"
            and row["group_id"] == viewer["group_id"]
            or viewer["role"] == "developer"
            and row["principal_id"] == viewer["id"]
        )
    ]
    response = holds.client.get(
        "/api/budgets/reservations",
        headers=auth(holds, role),
        params={
            "limit": 1,
            "principal": holds.members["outsider"]["id"],
            "group_id": str(UUID(int=200)),
        },
    )
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) == 1
    assert page["items"][0]["request_id"] in {row["request_id"] for row in expected}
    assert sum(total["count"] for total in page["totals"]) == len(expected)
    assert sum(total["held_micro_usd"] for total in page["totals"]) == sum(
        row["reserved"] for row in expected
    )
    assert len(holds.queries) == 1
    query = holds.queries[0]
    for row in rows:
        assert (json.dumps(row["request_id"]) in query) == (
            row["request_id"] == page["items"][0]["request_id"]
        )
    if role in {"group_admin", "developer"}:
        assert "Other team member" not in response.text
        assert holds.members["outsider"]["id"] not in response.text
        assert str(UUID(int=200)) not in response.text


def test_historical_group_scope_does_not_add_a_moved_members_current_identity(holds):
    row = admit(holds)
    data, etag = holds.store.read()
    member = next(m for m in data["members"] if m["id"] == row["principal_id"])
    member.update(group_id=str(UUID(int=200)), name="New team identity")
    data["revision"] += 1
    holds.store.save(data, etag)
    result = holds.client.get(
        "/api/budgets/reservations", headers=auth(holds, "group_admin")
    ).json()
    assert result["items"][0]["request_id"] == row["request_id"]
    assert result["items"][0]["member_name"] is None
    assert "New team identity" not in json.dumps(result)


def test_stable_ties_utc_offsets_unknown_times_and_cursor_after_removal(holds):
    first, second, old = [admit(holds) for _ in range(3)]
    for row, at in ((first, "2026-10-15T14:00:00+02:00"), (old, None)):
        value, etag = holds.ledger.rows.read("request-" + row["request_id"])
        value["at"] = at
        holds.ledger.rows.commit([("request-" + row["request_id"], value, etag)])
    page = holds.client.get(
        "/api/budgets/reservations", params={"limit": 1}, headers=auth(holds)
    ).json()
    assert page["items"][0]["request_id"] == second["request_id"]
    holds.ledger.settle(second["request_id"], 12)
    page = holds.client.get(
        "/api/budgets/reservations",
        params={"limit": 1, "cursor": page["next_cursor"]},
        headers=auth(holds),
    ).json()
    assert page["items"][0]["request_id"] == first["request_id"]
    page = holds.client.get(
        "/api/budgets/reservations",
        params={"limit": 1, "cursor": page["next_cursor"]},
        headers=auth(holds),
    ).json()
    assert page["items"][0]["request_id"] == old["request_id"]
    assert page["next_cursor"] is None


def test_hold_metadata_is_preserved_without_changing_reserved_amount(holds):
    row = admit(holds)
    key = "request-" + row["request_id"]
    saved, etag = holds.ledger.rows.read(key)
    saved.update(hold_reason="stream_incomplete", held_at=GATEWAY_AT)
    holds.ledger.rows.commit([(key, saved, etag)])
    item = holds.client.get("/api/budgets/reservations", headers=auth(holds)).json()["items"][0]
    assert item["hold_reason"] == "stream_incomplete" and item["held_at"] == GATEWAY_AT
    assert item["reserved"] == row["reserved"]


@pytest.mark.parametrize("limit", ["0", "-1", "201", "many", "1.5"])
def test_invalid_page_limit_is_rejected_without_query(holds, limit):
    response = holds.client.get(
        "/api/budgets/reservations", params={"limit": limit}, headers=auth(holds)
    )
    assert response.status_code == 422
    assert holds.queries == []


@pytest.mark.parametrize("cursor", ["", "not-base64", "a" * 1025, "e30=", "WzEsW10sW11d", "////"])
def test_invalid_cursor_is_rejected_without_query(holds, cursor):
    response = holds.client.get(
        "/api/budgets/reservations", params={"cursor": cursor}, headers=auth(holds)
    )
    assert response.status_code == 422 and holds.queries == []


def test_cursor_is_bound_to_viewer_scope_and_validates_key(holds):
    admit(holds)
    admit(holds)
    cursor = holds.client.get(
        "/api/budgets/reservations", params={"limit": 1}, headers=auth(holds)
    ).json()["next_cursor"]
    for role in ("group_admin", "developer"):
        response = holds.client.get(
            "/api/budgets/reservations", params={"cursor": cursor}, headers=auth(holds, role)
        )
        assert response.status_code == 422
    decoded = json.loads(base64.urlsafe_b64decode(cursor))
    for version in (None, True, 1.0, "1", 2):
        invalid = base64.urlsafe_b64encode(
            json.dumps([version, decoded[1], decoded[2]]).encode()
        ).decode()
        response = holds.client.get(
            "/api/budgets/reservations", params={"cursor": invalid}, headers=auth(holds)
        )
        assert response.status_code == 422
    for key in (
        ["invalid-time", str(UUID(int=1001))],
        ["", "not-a-uuid"],
        [None, str(UUID(int=1001))],
    ):
        invalid = base64.urlsafe_b64encode(json.dumps([1, decoded[1], key]).encode()).decode()
        response = holds.client.get(
            "/api/budgets/reservations", params={"cursor": invalid}, headers=auth(holds)
        )
        assert response.status_code == 422
    assert len(holds.queries) == 1


@pytest.mark.parametrize("change", ["role", "group", "suspended", "removed"])
def test_scope_revocation_during_outcome_query_discloses_no_rows_or_totals(
    holds, monkeypatch, change
):
    admit(holds)
    original = gateway_outcome.urlopen

    def revoked(request, timeout):
        data, etag = holds.store.read()
        member = next(m for m in data["members"] if m["id"] == holds.members["group_admin"]["id"])
        if change == "role":
            member["role"] = "developer"
        elif change == "group":
            member["group_id"] = str(UUID(int=200))
        elif change == "suspended":
            member["status"] = "suspended"
        else:
            data["members"].remove(member)
        data["revision"] += 1
        holds.store.save(data, etag)
        return original(request, timeout)

    monkeypatch.setattr(gateway_outcome, "urlopen", revoked)
    response = holds.client.get("/api/budgets/reservations", headers=auth(holds, "group_admin"))
    assert response.status_code == 403
    assert response.json() == {"detail": "Your reservation scope changed; reload before retrying"}
    assert len(holds.queries) == 1


def test_empty_and_unconfigured_listing_does_not_query_insights(holds, org):
    for app in (
        holds.app,
        create_app(org, holds.store, holds.credential, local=False),
    ):
        with TestClient(app) as client:
            response = client.get("/api/budgets/reservations", headers=auth(holds))
        assert response.status_code == 200
        assert response.json()["items"] == response.json()["totals"] == []
        assert response.json()["next_cursor"] is None
    assert holds.queries == [] and not holds.credential.get_token.called


def test_azure_pending_query_is_scoped_selected_bounded_and_sorted():
    store = object.__new__(AzureRows)
    store.partition = str(UUID(int=1))
    group, principal = str(UUID(int=2)), str(UUID(int=3))
    seen = []

    def entities(query, **options):
        assert (
            query
            == "PartitionKey eq @organization and state eq 'reserved' and group_id eq @group and principal_id eq @principal"
        )
        assert options == {
            "parameters": {"organization": store.partition, "group": group, "principal": principal},
            "select": ["data"],
            "results_per_page": 1000,
        }
        for index in range(PENDING_READ_LIMIT + 1):
            assert index < PENDING_READ_LIMIT, "Read exceeded the 10,000-row bound"
            seen.append(index)
            yield {"data": json.dumps({"request_id": str(UUID(int=index)), "at": GATEWAY_AT})}

    store.table = SimpleNamespace(query_entities=entities)
    rows = store.pending(group_id=group, principal=principal)
    assert len(rows) == len(seen) == PENDING_READ_LIMIT
    assert rows[0]["request_id"] == str(UUID(int=PENDING_READ_LIMIT - 1))
    assert rows[-1]["request_id"] == str(UUID(int=0))


@pytest.mark.parametrize("count,reached", [(9999, False), (10_000, True), (10_001, True)])
def test_memory_read_cutoff_is_honest_and_totals_cover_only_read_scope(holds, count, reached):
    prototype = admit(holds)
    holds.ledger.rows.rows = {
        "request-" + str(UUID(int=index)): (
            {**prototype, "request_id": str(UUID(int=index)), "reserved": 7},
            index,
        )
        for index in range(count)
    }
    page = holds.client.get(
        "/api/budgets/reservations", params={"limit": 200}, headers=auth(holds)
    ).json()
    assert len(page["items"]) == 200
    assert page["read_limit_reached"] is reached
    assert page["totals"] == [
        {
            "month": "2026-10",
            "held_micro_usd": min(count, 10_000) * 7,
            "count": min(count, 10_000),
        }
    ]
    assert page["has_more"] and page["next_cursor"]


def test_memory_scope_filters_before_the_read_cap(holds):
    foreign, own = admit(holds, "outsider"), admit(holds)
    holds.ledger.rows.rows.update(
        {
            "foreign-" + str(index): (
                {**foreign, "request_id": str(UUID(int=index + 20_000))},
                index,
            )
            for index in range(10_001)
        }
    )
    page = holds.client.get("/api/budgets/reservations", headers=auth(holds, "developer")).json()
    assert [row["request_id"] for row in page["items"]] == [own["request_id"]]
    assert page["totals"][0]["count"] == 1 and not page["read_limit_reached"]


@pytest.mark.parametrize(
    "status,cause,releasable",
    [
        ("401", "Rejected, not billed", True),
        ("403", "Rejected, not billed", True),
        ("404", "Rejected, not billed", True),
        ("429", "Throttled, not billed", True),
        ("400", "Rejected, may be billed", False),
        ("413", "Rejected, may be billed", False),
        ("415", "Rejected, may be billed", False),
        ("422", "Rejected, may be billed", False),
        ("200", "Completed, not settled", False),
        ("200 [not sent in full…]", "Client disconnected", False),
        ("200 [not sent in full (see exception telemetry)]", "Client disconnected", False),
        ("201", "Gateway response, billing unknown", False),
        ("302", "Gateway response, billing unknown", False),
        ("500", "Gateway response, billing unknown", False),
        ("502", "Gateway response, billing unknown", False),
    ],
)
def test_gateway_outcomes_map_each_status_without_inferring_release(
    holds, status, cause, releasable
):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"], status)]
    item = holds.client.get("/api/budgets/reservations", headers=auth(holds)).json()["items"][0]
    assert item["gateway_outcome"] == {
        "result_code": status,
        "timestamp": "2026-10-15T12:01:00+00:00",
        "cause": cause,
        "releasable": releasable,
    }
    assert len(holds.queries) == 1


def test_outcome_query_uses_exact_deployment_role_ids_and_safe_kql_literals():
    role = 'apim"); union secrets; //'
    request_id = 'request"\\\n| union secrets'
    query = gateway_outcome.build_query({"apim_name": role}, [request_id])
    assert "requests\n| where cloud_RoleName == " + json.dumps(role) in query
    assert (
        'tostring(customDimensions["Request Id"]) in (dynamic(' + json.dumps([request_id]) + "))"
        in query
    )
    assert "records=count()" in query and "arg_max(timestamp, resultCode)" in query
    assert "operation_Id" not in query and "ago(" not in query


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "foreign_id",
        "wrong_role",
        "duplicates",
        "count",
        "bad_count",
        "bool_count",
        "missing_timestamp",
        "naive_timestamp",
        "missing_status",
        "numeric_status",
    ],
)
def test_missing_or_ambiguous_evidence_never_allows_release(holds, kind):
    row = admit(holds)
    record = gateway_record(row["request_id"])
    holds.records = [record]
    if kind == "missing":
        holds.records = []
    elif kind == "foreign_id":
        record[0] = str(UUID(int=999))
    elif kind == "wrong_role":
        record[1] = "another-apim"
    elif kind == "duplicates":
        holds.records.append(record[:])
    elif kind in {"count", "bad_count", "bool_count"}:
        record[2] = {"count": 2, "bad_count": "1", "bool_count": True}[kind]
    elif kind in {"missing_timestamp", "naive_timestamp"}:
        record[4] = None if kind == "missing_timestamp" else "2026-10-15T12:01:00"
    elif kind == "missing_status":
        record[3] = ""
    else:
        record[3] = 429
    before = copy.deepcopy(holds.ledger.rows.rows)
    response = release(holds, row, gateway_status="429", gateway_timestamp=GATEWAY_AT)
    assert response.status_code == 422
    assert "hold was not released" in response.json()["detail"]
    assert holds.ledger.rows.rows == before
    assert holds.store.read()[0]["audit"] == []
    assert len(holds.queries) == 1


@pytest.mark.parametrize("failure", ["not_configured", "query", "credential", "partial", "columns"])
def test_unavailable_outcomes_leave_listing_usable_and_release_closed(holds, monkeypatch, failure):
    row = admit(holds)
    if failure == "not_configured":
        monkeypatch.delenv("PORTAL_INSIGHTS_APP_ID")
    elif failure == "query":
        monkeypatch.setattr(gateway_outcome, "urlopen", Mock(side_effect=OSError("unavailable")))
    elif failure == "credential":
        holds.credential.get_token.side_effect = OSError("no monitoring access")
    else:
        payload = (
            {
                "error": {"code": "PartialError"},
                **table_payload([gateway_record(row["request_id"])]),
            }
            if failure == "partial"
            else {"tables": [{"columns": [{"name": "result_code"}], "rows": [["429"]]}]}
        )
        monkeypatch.setattr(
            gateway_outcome,
            "urlopen",
            lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()),
        )
    before = copy.deepcopy(holds.ledger.rows.rows)
    page = holds.client.get("/api/budgets/reservations", headers=auth(holds))
    assert page.status_code == 200
    result = page.json()
    assert result["outcomes_status"] == (
        "not_configured" if failure == "not_configured" else "unavailable"
    )
    assert result["items"][0]["gateway_outcome"]["cause"] == "Gateway outcome unavailable"
    assert not result["items"][0]["gateway_outcome"]["releasable"]
    assert result["totals"][0]["held_micro_usd"] == row["reserved"]
    response = release(holds, row)
    assert response.status_code == 503
    assert holds.ledger.rows.rows == before and holds.store.read()[0]["audit"] == []


@pytest.mark.parametrize("status", ["401", "403", "404", "429"])
def test_release_is_audited_and_etag_saved_before_atomic_zero_settlement(
    holds, monkeypatch, status
):
    previous = admit(holds, amount=7000)
    holds.ledger.settle(previous["request_id"], 4321)
    row = admit(holds, amount=123456)
    admit(holds, amount=8888)
    november = admit(holds, amount=9999, at=NOVEMBER)
    holds.records = [gateway_record(row["request_id"], status)]
    before, other_month = counters(holds, row), counters(holds, november)
    assert len(before) == 3 and all(value["spent"] == 4321 for value in before.values())
    revision = holds.store.read()[1]
    original_save, original_settle, original_commit = (
        holds.store.save,
        holds.ledger.settle,
        holds.ledger.rows.commit,
    )
    steps = []

    def save(data, etag):
        assert etag == revision and data["revision"] == revision + 1
        assert counters(holds, row) == before
        original_save(data, etag)
        steps.append("audit")

    def settle(request_id, amount):
        assert steps == ["audit"]
        event = holds.store.read()[0]["audit"][-1]
        assert event["action"] == "budget.release_unbilled"
        assert event["actor_id"] == OWNER and event["target"] == row["request_id"]
        assert event["group_id"] == row["group_id"]
        assert event["after"] == {
            "reason": "Verified synthetic gateway rejection",
            "gateway_status": status,
            "gateway_timestamp": "2026-10-15T12:01:00+00:00",
            "released_micro_usd": 123456,
            "charge_micro_usd": 0,
        }
        assert request_id == row["request_id"] and amount == 0
        steps.append("settle")
        return original_settle(request_id, amount)

    def commit(writes):
        assert len(writes) == 4
        assert {key for key, _, _ in writes} == {*row["counters"], "request-" + row["request_id"]}
        assert all(
            etag is not None and holds.ledger.rows.read(key)[1] == etag for key, _, etag in writes
        )
        original_commit(writes)

    monkeypatch.setattr(holds.store, "save", save)
    monkeypatch.setattr(holds.ledger, "settle", settle)
    monkeypatch.setattr(holds.ledger.rows, "commit", commit)
    response = release(holds, row)
    assert response.status_code == 200
    assert response.json() == {
        "released": True,
        "released_micro_usd": 123456,
        "charged_micro_usd": 0,
    }
    assert steps == ["audit", "settle"]
    assert counters(holds, row) == {
        key: {"spent": value["spent"], "reserved": value["reserved"] - 123456}
        for key, value in before.items()
    }
    assert counters(holds, november) == other_month
    stored = holds.ledger.rows.read("request-" + row["request_id"])[0]
    assert stored["state"] == "settled" and stored["charged"] == 0


@pytest.mark.parametrize(
    "status",
    [
        "200",
        "200 [not sent in full…]",
        "400",
        "413",
        "415",
        "422",
        "201",
        "302",
        "408",
        "500",
        "502",
        " 429",
        "429 ",
        "429.0",
    ],
)
def test_release_refuses_every_other_status_and_ignores_client_evidence(holds, status):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"], status)]
    before = copy.deepcopy(holds.ledger.rows.rows)
    response = release(holds, row, gateway_status="429", releasable=True)
    assert response.status_code == 422
    assert holds.ledger.rows.rows == before and holds.store.read()[0]["audit"] == []


@pytest.mark.parametrize(
    "role", ["admin", "billing", "auditor", "group_admin", "developer", "outsider"]
)
def test_release_is_owner_only_on_the_server(holds, role):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"])]
    before = copy.deepcopy(holds.ledger.rows.rows)
    response = release(holds, row, role)
    assert response.status_code == 403
    assert holds.ledger.rows.rows == before and holds.store.read()[0]["audit"] == []
    assert holds.queries == []


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": None},
        {"request_id": 123},
        {"request_id": "bad-id"},
        {"reason": "tiny"},
        {"reason": " " * 20},
        {"reason": "x" * 301},
        {"reason": None},
    ],
)
def test_release_validates_identifiers_and_audit_reason(holds, body):
    row = admit(holds)
    before = copy.deepcopy(holds.ledger.rows.rows)
    assert release(holds, row, **body).status_code == 422
    assert holds.ledger.rows.rows == before and holds.store.read()[0]["audit"] == []
    assert holds.queries == []


def test_release_refuses_unknown_settled_and_replayed_reservations(holds):
    row = admit(holds)
    assert release(holds, {"request_id": str(UUID(int=999))}).status_code == 422
    holds.records = [gateway_record(row["request_id"])]
    assert release(holds, row).status_code == 200
    before = copy.deepcopy(holds.ledger.rows.rows)
    assert release(holds, row).status_code == 422
    assert holds.ledger.rows.rows == before
    assert len(holds.queries) == len(holds.store.read()[0]["audit"]) == 1


def test_release_requeries_instead_of_trusting_listed_status(holds):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"])]
    page = holds.client.get("/api/budgets/reservations", headers=auth(holds)).json()
    assert page["items"][0]["gateway_outcome"]["releasable"]
    holds.records = [gateway_record(row["request_id"], "400")]
    before = copy.deepcopy(holds.ledger.rows.rows)
    assert (
        release(holds, row, gateway_outcome=page["items"][0]["gateway_outcome"]).status_code == 422
    )
    assert len(holds.queries) == 2 and holds.queries[0] == holds.queries[1]
    assert holds.ledger.rows.rows == before and holds.store.read()[0]["audit"] == []


@pytest.mark.parametrize(
    "failure,expected", [(OSError("audit unavailable"), 503), (Conflict("stale audit etag"), 409)]
)
def test_failed_audit_or_state_conflict_cannot_change_any_counter(
    holds, monkeypatch, failure, expected
):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"])]
    before = copy.deepcopy(holds.ledger.rows.rows)
    original_state = holds.store.read()
    monkeypatch.setattr(holds.store, "save", Mock(side_effect=failure))
    settle = Mock(side_effect=AssertionError("Settlement must follow durable audit"))
    monkeypatch.setattr(holds.ledger, "settle", settle)
    assert release(holds, row).status_code == expected
    assert holds.ledger.rows.rows == before and holds.store.read() == original_state
    assert not settle.called


def test_owner_revocation_during_gateway_query_fails_the_audit_etag(holds, monkeypatch):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"])]
    original = gateway_outcome.urlopen

    def revoked(request, timeout):
        data, etag = holds.store.read()
        data["members"][0]["role"] = "admin"
        data["revision"] += 1
        holds.store.save(data, etag)
        return original(request, timeout)

    monkeypatch.setattr(gateway_outcome, "urlopen", revoked)
    before = copy.deepcopy(holds.ledger.rows.rows)
    assert release(holds, row).status_code == 409
    assert holds.ledger.rows.rows == before and holds.store.read()[0]["audit"] == []


@pytest.mark.parametrize(
    "failure,expected,attempts",
    [(Contended("synthetic etag conflict"), 409, 12), (OSError("ledger unavailable"), 503, 1)],
)
def test_failed_ledger_writes_leave_all_three_counters_unchanged_with_a_durable_audit(
    holds, monkeypatch, failure, expected, attempts
):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"])]
    before = copy.deepcopy(holds.ledger.rows.rows)
    commit = Mock(side_effect=failure)
    monkeypatch.setattr(holds.ledger.rows, "commit", commit)
    assert release(holds, row).status_code == expected
    assert commit.call_count == attempts
    assert holds.ledger.rows.rows == before
    assert len(holds.store.read()[0]["audit"]) == 1
    for call in commit.call_args_list:
        assert len(call.args[0]) == 4
        assert all(etag is not None for _, _, etag in call.args[0])


def test_row_is_rechecked_after_gateway_query_before_auditing(holds, monkeypatch):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"])]
    original = gateway_outcome.urlopen

    def settle_during_query(request, timeout):
        holds.ledger.settle(row["request_id"], 789)
        return original(request, timeout)

    monkeypatch.setattr(gateway_outcome, "urlopen", settle_during_query)
    assert release(holds, row).status_code == 422
    assert holds.store.read()[0]["audit"] == []
    assert all(value == {"spent": 789, "reserved": 0} for value in counters(holds, row).values())


def test_concurrent_paid_settlement_is_not_refunded_after_audit(holds, monkeypatch):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"])]
    original = holds.ledger.settle

    def concurrent(request_id, amount):
        assert holds.store.read()[0]["audit"][-1]["action"] == "budget.release_unbilled"
        original(request_id, 789)
        return original(request_id, amount)

    monkeypatch.setattr(holds.ledger, "settle", concurrent)
    assert release(holds, row).status_code == 422
    assert len(holds.store.read()[0]["audit"]) == 1
    assert all(value == {"spent": 789, "reserved": 0} for value in counters(holds, row).values())


def test_concurrent_releases_have_one_etag_winner_and_one_counter_reduction(holds, monkeypatch):
    row = admit(holds)
    holds.records = [gateway_record(row["request_id"])]
    original = holds.store.save
    ready = threading.Barrier(2)

    def save(data, etag):
        ready.wait(timeout=10)
        original(data, etag)

    monkeypatch.setattr(holds.store, "save", save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: release(holds, row), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(holds.store.read()[0]["audit"]) == 1
    assert all(value == {"spent": 0, "reserved": 0} for value in counters(holds, row).values())
