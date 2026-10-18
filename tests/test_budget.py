"""Money boundaries: concurrency, failure ambiguity, owner limits and provider usage."""

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from claude_foundry.portal.budget_ledger import (
    BudgetDenied,
    Contended,
    Ledger,
    MemoryRows,
    policy_digest,
)
from claude_foundry.portal.budget_policy import snapshot
from claude_foundry.portal.budget_pricing import BudgetPrice, micros, usage_tokens
from claude_foundry.portal.budget_proxy import BudgetProxy
from claude_foundry.portal.budget_stream import UsageStream
from claude_foundry.portal.domain import seed


@pytest.fixture
def policy(org):
    state = seed(org, str(uuid4()))
    state["settings"].update(budget_enforcement=True, spend_limit_usd="1")
    state["groups"][0].update(
        spend_ceiling_usd="1",
        delegated_models=["claude-sonnet-4-6"],
        model_ids=["claude-sonnet-4-6"],
    )
    state["budget_prices"] = [
        dict(
            model="claude-sonnet-4-6",
            provider="anthropic",
            deployment_version="1",
            max_input_tokens=1000,
            max_output_tokens=1000,
            default_output_tokens=100,
            input_ceiling_per_million="3",
            output_ceiling_per_million="15",
            source_url="https://example.com/price",
            verified_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            valid_until=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
            note="Synthetic test pricing and capacity, never production rates",
        )
    ]
    return snapshot(state)


@pytest.fixture
def ledger(policy):
    ledger = Ledger(MemoryRows())
    ledger.activate(policy)
    return ledger


def admit(ledger, policy, amount=100_000, request=None, at=None):
    return ledger.reserve(
        request or str(uuid4()),
        policy["members"][0]["id"],
        "claude-sonnet-4-6",
        amount,
        policy,
        "hash",
        at,
    )


def test_atomic_concurrent_all_three_scopes(ledger, policy):
    def attempt(_):
        try:
            return admit(ledger, policy)
        except BudgetDenied:
            return None

    with ThreadPoolExecutor(max_workers=32) as pool:
        admitted = [x for x in pool.map(attempt, range(100)) if x]
    assert len(admitted) == 10
    balances = ledger.balances(policy, policy["members"][0]["id"])
    assert all(
        s["reserved_micro_usd"] == 1_000_000 and s["remaining_micro_usd"] == 0
        for s in balances["scopes"]
    )


@pytest.mark.parametrize("scope", ["organization", "group", "user"])
def test_each_limit_applies_to_owner(policy, scope):
    if scope == "organization":
        policy["settings"]["spend_limit_usd"] = "0"
    elif scope == "group":
        policy["groups"][0]["spend_ceiling_usd"] = "0"
    else:
        policy["members"][0]["spend_limit_usd"] = "0"
    ledger = Ledger(MemoryRows())
    ledger.activate(policy)
    with pytest.raises(BudgetDenied, match="insufficient"):
        admit(ledger, policy)


def test_settle_once_no_ttl_refund_or_duplicate_dispatch(ledger, policy):
    reservation = admit(ledger, policy)
    request = reservation["request_id"]
    with pytest.raises(BudgetDenied, match="already"):
        admit(ledger, policy, request=request)
    ledger.settle(request, 12_345)
    ledger.settle(request, 12_345)
    with pytest.raises(ValueError, match="Conflicting"):
        ledger.settle(request, 12_346)
    assert all(
        s["spent_micro_usd"] == 12_345 and s["reserved_micro_usd"] == 0
        for s in ledger.balances(policy, policy["members"][0]["id"])["scopes"]
    )


def test_month_rollover_keeps_original_reservation(ledger, policy):
    before = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    after = before + timedelta(minutes=2)
    reservation = admit(ledger, policy, at=before)
    assert (
        ledger.balances(policy, policy["members"][0]["id"], after)["scopes"][0][
            "reserved_micro_usd"
        ]
        == 0
    )
    ledger.settle(reservation["request_id"], 500)
    assert (
        ledger.balances(policy, policy["members"][0]["id"], before)["scopes"][0]["spent_micro_usd"]
        == 500
    )


def test_lowering_limit_and_group_moves_cannot_reset_spend(ledger, policy):
    reservation = admit(ledger, policy, amount=900_000)
    ledger.settle(reservation["request_id"], 900_000)
    lower = copy.deepcopy(policy)
    lower["settings"]["spend_limit_usd"] = ".5"
    with pytest.raises(BudgetDenied, match="below"):
        ledger.activate(lower)
    moved = copy.deepcopy(policy)
    group = {**moved["groups"][0], "id": str(uuid4())}
    moved["groups"].append(group)
    moved["members"][0]["group_id"] = group["id"]
    ledger.activate(moved)
    with pytest.raises(BudgetDenied):
        admit(ledger, moved, amount=200_000)
    assert (
        ledger.balances(moved, moved["members"][0]["id"])["scopes"][2]["spent_micro_usd"] == 900_000
    )


def test_ambiguous_commit_keeps_reservation_and_never_authorizes_retry(ledger, policy):
    commit = ledger.rows.commit

    def ambiguous(rows):
        commit(rows)
        raise TimeoutError("response lost after commit")

    ledger.rows.commit = ambiguous
    request = str(uuid4())
    with pytest.raises(TimeoutError):
        admit(ledger, policy, request=request)
    ledger.rows.commit = commit
    with pytest.raises(BudgetDenied, match="already"):
        admit(ledger, policy, request=request)
    assert (
        ledger.balances(policy, policy["members"][0]["id"])["scopes"][0]["reserved_micro_usd"]
        == 100_000
    )


def test_overrun_pauses_and_cannot_be_unpaused_by_publish(ledger, policy):
    reservation = admit(ledger, policy)
    with pytest.raises(BudgetDenied, match="paused"):
        ledger.settle(reservation["request_id"], 100_001)
    with pytest.raises(BudgetDenied):
        admit(ledger, policy)
    with pytest.raises(BudgetDenied, match="incident"):
        ledger.activate(policy)


def test_concurrent_publication_rejects_stale_policy(ledger, policy):
    changed = copy.deepcopy(policy)
    changed["settings"]["spend_limit_usd"] = "2"
    ledger.activate(changed, expected=policy_digest(policy))
    with pytest.raises(BudgetDenied, match="changed"):
        admit(ledger, policy)
    with pytest.raises(Contended):
        ledger.activate(policy, expected=policy_digest(policy))


def test_price_bounds_expiry_and_no_unpriced_tools(policy):
    price = BudgetPrice.model_validate(policy["budget_prices"][0])
    request, held, _ = price.prepare({"model": price.model, "max_tokens": 100})
    assert held == 4500 and request["max_tokens"] == 100
    assert price.cost(1, 1) == 18
    for field in [
        {"tools": [{"type": "web_search_20250305"}]},
        {"speed": "fast"},
        {"previous_response_id": "opaque"},
        {"max_tokens": 1001},
        {"stream": "true"},
        {"tools": None},
    ]:
        with pytest.raises(ValueError):
            price.prepare({"model": price.model, **field})
    with pytest.raises(ValueError, match="expired"):
        price.check_current(datetime.now(UTC) + timedelta(days=10))
    assert micros("0.000001") == 1
    for amount in ["0.0000001", "-1", "NaN", "Infinity"]:
        with pytest.raises(ValueError):
            micros(amount)


def test_usage_cache_reasoning_and_split_utf8_streams():
    assert usage_tokens(
        "anthropic",
        {
            "input_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
            "output_tokens": 5,
        },
    ) == (9, 5)
    assert usage_tokens(
        "openai",
        {
            "input_tokens": 20,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 15},
            "output_tokens_details": {"reasoning_tokens": 8},
        },
    ) == (20, 10)
    for provider, events in [
        (
            "anthropic",
            [
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 2, "output_tokens": 0}},
                },
                {"type": "content_block_delta", "delta": {"text": "שלום"}},
                {"type": "message_delta", "usage": {"output_tokens": 3}},
                {"type": "message_stop"},
            ],
        ),
        (
            "openai",
            [
                {
                    "type": "response.completed",
                    "response": {"usage": {"input_tokens": 2, "output_tokens": 3}},
                }
            ],
        ),
    ]:
        parser = UsageStream(provider)
        wire = "".join(
            "data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n" for e in events
        ).encode()
        for byte in wire:
            parser.feed(bytes([byte]))
        assert parser.tokens() == (2, 3)
    parser = UsageStream("openai")
    parser.feed(b'data: {"type":"response.output_text.delta","delta":"abc"}\n\n')
    with pytest.raises(ValueError):
        parser.tokens()


def test_shared_ledger_blocks_second_provider_before_backend(policy, ledger):
    openai_price = {**policy["budget_prices"][0], "model": "gpt-test", "provider": "openai"}
    policy["budget_prices"].append(openai_price)
    policy["groups"][0]["model_ids"].append("gpt-test")
    policy["groups"][0]["delegated_models"].append("gpt-test")
    ledger.activate(policy)
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200, json={"usage": {"input_tokens": 2, "output_tokens": 3}, "output": []}
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            proxy = BudgetProxy(
                ledger,
                SimpleNamespace(get=lambda digest: policy),
                SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="synthetic")),
                "https://test.services.ai.azure.com",
                {
                    p["model"]: {
                        "provider": p["provider"],
                        "version": "1",
                        "max_input_tokens": 1000,
                        "max_output_tokens": 1000,
                    }
                    for p in policy["budget_prices"]
                },
                client,
            )
            response = await proxy.infer(
                "anthropic",
                policy["members"][0]["id"],
                str(uuid4()),
                {"model": "claude-sonnet-4-6", "max_tokens": 100},
            )
            assert response.status_code == 200
            held = admit(ledger, policy, amount=995_500)
            with pytest.raises(BudgetDenied):
                await proxy.infer(
                    "openai",
                    policy["members"][0]["id"],
                    str(uuid4()),
                    {"model": "gpt-test", "max_output_tokens": 100},
                )
            assert held["state"] == "reserved"

    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0].url.path == "/anthropic/v1/messages"


def test_snapshot_rejects_undelegated_grants_and_missing_prices(policy):
    invalid = copy.deepcopy(policy)
    invalid["members"][0]["extra_models"] = ["unapproved"]
    with pytest.raises(ValueError, match="delegated"):
        snapshot(invalid)
    invalid = copy.deepcopy(policy)
    invalid["budget_prices"] = []
    with pytest.raises(ValueError, match="prices"):
        snapshot(invalid)


def test_scoped_publication_cannot_include_owner_or_other_team_drafts(policy):
    from claude_foundry.portal.budget_policy import group_publication

    draft = copy.deepcopy(policy)
    group = draft["groups"][0]
    actor = {**draft["members"][0], "id": str(uuid4()), "role": "group_admin"}
    draft["settings"]["spend_limit_usd"] = "999999"
    group["spend_ceiling_usd"] = "999999"
    group["spend_limit_usd"] = ".5"
    draft["members"][0]["spend_limit_usd"] = "999999"
    draft["budget_prices"][0]["input_ceiling_per_million"] = ".00001"
    published = group_publication(policy, draft, actor)
    assert published["settings"] == policy["settings"]
    assert published["groups"][0]["spend_ceiling_usd"] == "1"
    assert published["groups"][0]["spend_limit_usd"] == "0.5"
    assert published["members"] == policy["members"]
    assert published["budget_prices"] == policy["budget_prices"]


def test_anthropic_stop_without_final_usage_cannot_refund():
    parser = UsageStream("anthropic")
    for event in [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "content_block_delta", "delta": {"text": "billable output"}},
        {"type": "message_stop"},
    ]:
        parser.feed(("data: " + json.dumps(event) + "\n\n").encode())
    with pytest.raises(ValueError):
        parser.tokens()


def test_pending_reservations_keep_request_time_group(ledger, policy):
    admitted = admit(ledger, policy)
    assert ledger.rows.pending(group_id=str(uuid4())) == []
    assert ledger.rows.pending(principal=str(uuid4())) == []
    assert (
        ledger.rows.pending(group_id=admitted["group_id"])[0]["request_id"]
        == admitted["request_id"]
    )
    ledger.settle(admitted["request_id"], admitted["reserved"])
    assert ledger.rows.pending() == []


def test_bootstrap_cannot_replace_existing_policy(ledger, policy):
    with pytest.raises(Contended):
        ledger.activate(policy, expected="")


def test_native_thinking_preservation_does_not_enable_compaction(policy):
    price = BudgetPrice.model_validate(policy["budget_prices"][0])
    preserve = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
    request, held, _ = price.prepare({"model": price.model, "context_management": preserve})
    assert request["context_management"] == preserve and held == 4500
    for unsafe in [
        None,
        {},
        {"edits": preserve["edits"] + [{"type": "compact_20260112"}]},
        {"edits": [{"type": "clear_thinking_20251015", "keep": "all", "unreviewed": True}]},
    ]:
        with pytest.raises(ValueError):
            price.prepare({"model": price.model, "context_management": unsafe})


def test_client_namespaces_cannot_hide_server_tools(policy):
    price = BudgetPrice.model_validate({**policy["budget_prices"][0], "provider": "openai"})
    safe = {
        "type": "namespace",
        "name": "functions",
        "tools": [{"type": "function", "name": "read_file"}],
    }
    request, _, _ = price.prepare(
        {"model": price.model, "tools": [safe], "client_metadata": {"originator": "codex"}}
    )
    assert request["tools"] == [safe]
    for child in [
        {"type": "web_search"},
        {"type": "code_interpreter"},
        {"type": "namespace", "tools": [safe]},
        42,
    ]:
        with pytest.raises(ValueError):
            price.prepare({"model": price.model, "tools": [{**safe, "tools": [child]}]})


@pytest.mark.parametrize(
    "mode", ["disconnect", "missing_usage", "provider_error", "settlement_unavailable"]
)
def test_proxy_unknown_outcomes_never_release_reserved_allowance(policy, ledger, mode):
    from fastapi import HTTPException

    def handler(req):
        if mode == "disconnect":
            raise httpx.ReadTimeout("Connection lost after dispatch")
        if mode == "provider_error":
            return httpx.Response(400, json={"error": {"message": "Invalid input"}})
        return httpx.Response(
            200,
            json={}
            if mode == "missing_usage"
            else {"usage": {"input_tokens": 2, "output_tokens": 3}},
        )

    if mode == "settlement_unavailable":

        def unavailable(*args):
            raise TimeoutError("Ledger outage")

        ledger.settle = unavailable

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            proxy = BudgetProxy(
                ledger,
                SimpleNamespace(get=lambda digest: policy),
                SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test")),
                "https://test.services.ai.azure.com",
                {
                    "claude-sonnet-4-6": {
                        "version": "1",
                        "provider": "anthropic",
                        "max_input_tokens": 1000,
                        "max_output_tokens": 1000,
                    }
                },
                client,
            )
            call = proxy.infer(
                "anthropic",
                policy["members"][0]["id"],
                str(uuid4()),
                {"model": "claude-sonnet-4-6", "max_tokens": 100},
            )
            if mode == "disconnect":
                with pytest.raises(HTTPException):
                    await call
            else:
                await call

    asyncio.run(run())
    assert all(
        s["reserved_micro_usd"] == 4500 and s["spent_micro_usd"] == 0
        for s in ledger.balances(policy, policy["members"][0]["id"])["scopes"]
    )


def test_portal_initialization_preserves_active_policy_and_ledger(org, ledger, policy):
    from claude_foundry.portal.budget_policy import portal_seed

    owner = policy["members"][0]["id"]
    initial = seed(org, owner)
    snapshots = SimpleNamespace(get=lambda digest: copy.deepcopy(policy))
    held = admit(ledger, policy)
    before = ledger.balances(policy, owner)
    restored = portal_seed(initial, ledger, snapshots, owner)
    assert restored["groups"] == policy["groups"]
    assert restored["members"] == policy["members"]
    assert restored["budget_prices"] == policy["budget_prices"]
    assert restored["audit"] == []
    assert ledger.balances(policy, owner) == before
    assert ledger.rows.read("request-" + held["request_id"])[0]["state"] == "reserved"
    with pytest.raises(ValueError, match="active owner"):
        portal_seed(initial, ledger, snapshots, str(uuid4()))
    assert portal_seed(initial, Ledger(MemoryRows()), snapshots, owner) == initial
