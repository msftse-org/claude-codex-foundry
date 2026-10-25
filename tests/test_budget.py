"""Money boundaries: concurrency, failure ambiguity, owner limits and provider usage."""

import asyncio
import copy
import json
import threading
import time
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
from claude_foundry.portal.budget_policy import Snapshots, snapshot
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


@pytest.mark.parametrize("output_limit", [None, 20000, 32000])
def test_openai_compaction_reserves_a_sufficient_verified_output_bound(policy, output_limit):
    price = BudgetPrice.model_validate(
        {
            **policy["budget_prices"][0],
            "provider": "openai",
            "model": "gpt-test",
            "max_output_tokens": 32000,
        }
    )
    body = {"model": price.model, "input": [{"type": "compaction_trigger"}]}
    if output_limit is not None:
        body["max_output_tokens"] = output_limit
    payload, held, bound = price.prepare(body)
    assert payload["max_output_tokens"] == bound == (output_limit or 20000)
    assert held == price.cost(price.max_input_tokens, bound)
    assert payload["input"] == body["input"] and payload["store"] is False
    # Ordinary requests retain the small default and lower reservation.
    normal, _, normal_bound = price.prepare({"model": price.model, "input": "Hello"})
    assert normal["max_output_tokens"] == normal_bound == price.default_output_tokens


def test_compaction_cannot_bypass_verified_capacity_or_explicit_output_bound(policy):
    data = {**policy["budget_prices"][0], "provider": "openai", "model": "gpt-test"}
    body = {"model": "gpt-test", "input": [{"type": "compaction_trigger"}]}
    with pytest.raises(ValueError, match="verified model maximum"):
        BudgetPrice.model_validate(data).prepare(body)
    data["max_output_tokens"] = 32000
    price = BudgetPrice.model_validate(data)
    for invalid in [True, 0, 19999, 32001]:
        with pytest.raises(ValueError):
            price.prepare({**body, "max_output_tokens": invalid})
    assert price.prepare({**body, "max_output_tokens": None})[2] == 20000


def test_compaction_gateway_dispatch_and_settlement_use_the_same_bound(policy):
    price = policy["budget_prices"][0]
    price.update(provider="openai", model="gpt-test", max_output_tokens=32000)
    group = policy["groups"][0]
    group.update(model_ids=["gpt-test"], delegated_models=["gpt-test"])
    ledger = Ledger(MemoryRows())
    ledger.activate(policy)
    principal = policy["members"][0]["id"]

    def handler(request):
        assert json.loads(request.content)["max_output_tokens"] == 20000
        assert ledger.balances(policy, principal)["scopes"][0]["reserved_micro_usd"] == 303000
        return httpx.Response(
            200,
            json={
                "output": [{"type": "compaction", "encrypted_content": "synthetic"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            proxy = BudgetProxy(
                ledger,
                SimpleNamespace(get=lambda digest: policy),
                SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="synthetic")),
                "https://test.services.ai.azure.com",
                {
                    "gpt-test": {
                        "provider": "openai",
                        "version": "1",
                        "max_input_tokens": 1000,
                        "max_output_tokens": 32000,
                    }
                },
                client,
            )
            response = await proxy.infer(
                "openai",
                principal,
                str(uuid4()),
                {
                    "model": "gpt-test",
                    "input": [{"type": "compaction_trigger"}],
                },
            )
            assert response.status_code == 200

    asyncio.run(run())
    for scope in ledger.balances(policy, principal)["scopes"]:
        assert scope["reserved_micro_usd"] == 0 and scope["spent_micro_usd"] == 1050


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


@pytest.fixture
def snapshot_store(monkeypatch):
    def create(*policies, **limits):
        documents = {policy_digest(p) + ".json": json.dumps(p).encode() for p in policies}
        downloads = []

        def download(name):
            downloads.append(name)
            return SimpleNamespace(readall=lambda: documents[name])

        blobs = SimpleNamespace(download_blob=download)
        monkeypatch.setattr(
            "claude_foundry.portal.budget_policy.ContainerClient", lambda *a, **kw: blobs
        )
        return Snapshots("syntheticstorage", None, **limits), documents, downloads

    return create


def test_snapshot_cache_is_verified_and_returns_independent_documents(policy, snapshot_store):
    store, _, downloads = snapshot_store(policy)
    digest = policy_digest(policy)
    first = store.get(digest)
    first["members"][0]["extra_models"].append("injected-grant")
    first["settings"]["spend_limit_usd"] = "999999"
    assert store.get(digest) == policy
    assert len(downloads) == 1
    assert store.cache_info()["hits"] == 1
    assert store.cache_info()["misses"] == 1


def test_snapshot_caches_are_isolated_per_storage_client(policy, snapshot_store):
    a, _, a_downloads = snapshot_store(policy)
    b, documents, _ = snapshot_store(policy)
    digest = policy_digest(policy)
    a.get(digest)
    documents[digest + ".json"] = b"{}"
    with pytest.raises(ValueError, match="integrity"):
        b.get(digest)
    assert len(a_downloads) == 1 and b.cache_info()["entries"] == 0


def test_snapshot_lru_bounds_entries_and_bytes(policy, snapshot_store):
    policies = []
    for limit in ["1", "2", "3"]:
        p = copy.deepcopy(policy)
        p["settings"]["spend_limit_usd"] = limit
        policies.append(p)
    size = len(json.dumps(policies[0]).encode())
    for limits in [{"max_entries": 2}, {"max_bytes": size * 2}]:
        store, _, downloads = snapshot_store(*policies, **limits)
        a, b, c = [policy_digest(p) for p in policies]
        for digest in [a, b, a, c, a]:
            store.get(digest)
        assert len(downloads) == 3
        assert store.cache_info()["entries"] == 2
        assert store.cache_info()["bytes"] <= size * 2
        store.get(b)
        assert len(downloads) == 4 and store.cache_info()["evictions"] == 2


@pytest.mark.parametrize("limits", [{"max_entries": 0}, {"max_bytes": 1}])
def test_snapshot_oversize_and_disabled_cache_do_not_change_authorization(
    policy, snapshot_store, limits
):
    store, _, downloads = snapshot_store(policy, **limits)
    for _ in range(2):
        assert store.get(policy_digest(policy)) == policy
    assert len(downloads) == 2
    assert store.cache_info()["entries"] == store.cache_info()["bytes"] == 0


@pytest.mark.parametrize("failure", ["corrupt", "invalid_json", "missing", "outage"])
def test_snapshot_failures_never_cache_or_fall_back(policy, snapshot_store, failure):
    store, documents, downloads = snapshot_store(policy)
    digest = policy_digest(policy)
    name = digest + ".json"
    good = documents[name]
    if failure == "corrupt":
        documents[name] = b"{}"
    elif failure == "invalid_json":
        documents[name] = b"not-json"
    elif failure == "missing":
        del documents[name]
    else:

        def unavailable(name):
            raise TimeoutError("Synthetic storage outage")

        store.blobs.download_blob = unavailable
    with pytest.raises((ValueError, KeyError, TimeoutError)):
        store.get(digest)
    assert store.cache_info()["entries"] == 0
    documents[name] = good
    store.blobs.download_blob = lambda name: SimpleNamespace(readall=lambda: documents[name])
    assert store.get(digest) == policy
    assert store.cache_info()["misses"] == 2
    assert len(downloads) <= 1


def test_snapshot_publication_checks_storage_even_when_cache_is_warm(policy, snapshot_store):
    from azure.core.exceptions import ResourceExistsError

    store, documents, _ = snapshot_store(policy)
    digest = policy_digest(policy)
    store.get(digest)
    documents[digest + ".json"] = b"{}"

    def exists(*args, **kwargs):
        raise ResourceExistsError("Already exists")

    store.blobs.upload_blob = exists
    with pytest.raises(ValueError, match="integrity"):
        store.put(policy)


@pytest.mark.parametrize("outage", [False, True])
def test_concurrent_snapshot_misses_share_download_and_failure(policy, snapshot_store, outage):
    store, _, downloads = snapshot_store(policy)
    start, release = threading.Event(), threading.Event()
    download = store.blobs.download_blob

    def blocked(name):
        start.set()
        assert release.wait(5), "Test download was not released"
        if outage:
            raise TimeoutError("Synthetic shared outage")
        return download(name)

    store.blobs.download_blob = blocked
    with ThreadPoolExecutor(max_workers=16) as pool:
        calls = [pool.submit(store.get, policy_digest(policy)) for _ in range(16)]
        try:
            assert start.wait(5)
            deadline = time.monotonic() + 5
            while store.cache_info()["waits"] < 15 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert store.cache_info()["waits"] == 15
        finally:
            release.set()
        for call in calls:
            if outage:
                with pytest.raises(TimeoutError):
                    call.result(timeout=5)
            else:
                assert call.result(timeout=5) == policy
    assert store.cache_info()["misses"] == 1
    assert len(downloads) == (0 if outage else 1)
    store.blobs.download_blob = download
    assert store.get(policy_digest(policy)) == policy
    assert len(downloads) == 1


def test_snapshot_download_does_not_lock_other_versions(policy, snapshot_store):
    changed = copy.deepcopy(policy)
    changed["settings"]["spend_limit_usd"] = "2"
    store, _, _ = snapshot_store(policy, changed)
    old, new = policy_digest(policy), policy_digest(changed)
    start, release = threading.Event(), threading.Event()
    download = store.blobs.download_blob

    def blocked(name):
        if name == old + ".json":
            start.set()
            assert release.wait(5)
        return download(name)

    store.blobs.download_blob = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(store.get, old)
        try:
            assert start.wait(5)
            assert pool.submit(store.get, new).result(timeout=2) == changed
        finally:
            release.set()
        assert pending.result(timeout=5) == policy


@pytest.mark.parametrize(
    "change",
    [
        "suspend",
        "model",
        "user_limit",
        "group_limit",
        "org_limit",
        "pause",
        "outage",
        "race",
        "expired",
        "new_blob_missing",
        "new_blob_corrupt",
    ],
)
@pytest.mark.parametrize("shared", [False, True])
def test_warm_policy_cache_cannot_bypass_live_admission(
    policy, ledger, snapshot_store, monkeypatch, change, shared
):
    store, documents, _ = snapshot_store(
        policy,
        shared_cache=SimpleNamespace(get=lambda digest: (json.dumps(policy).encode(), "hit"))
        if shared
        else None,
    )
    store.get(policy_digest(policy))
    changed = copy.deepcopy(policy)
    if change == "suspend":
        changed["members"][0]["status"] = "suspended"
    elif change == "model":
        changed["groups"][0]["model_ids"] = []
    elif change == "user_limit":
        changed["members"][0]["spend_limit_usd"] = "0"
    elif change == "group_limit":
        changed["groups"][0]["spend_limit_usd"] = "0"
    elif change == "org_limit":
        changed["settings"]["spend_limit_usd"] = "0"
    if change in {
        "suspend",
        "model",
        "user_limit",
        "group_limit",
        "org_limit",
        "race",
        "new_blob_missing",
        "new_blob_corrupt",
    }:
        if change == "race":
            changed["members"][0]["spend_limit_usd"] = "0"
            commit = ledger.rows.commit

            def concurrent_publish(writes):
                if any(key.startswith("request-") for key, _, _ in writes):
                    ledger.activate(changed)
                return commit(writes)

            ledger.rows.commit = concurrent_publish
        else:
            if change.startswith("new_blob"):
                changed["budget_prices"][0]["note"] += " New reviewed policy."
            ledger.activate(changed)
        if change != "new_blob_missing":
            documents[policy_digest(changed) + ".json"] = (
                b"{}" if change == "new_blob_corrupt" else json.dumps(changed).encode()
            )
    elif change == "pause":
        value, etag = ledger.rows.read("policy")
        ledger.rows.commit([("policy", {**value, "paused": True}, etag)])
    elif change == "outage":

        def unavailable(*args):
            raise TimeoutError("Authoritative Table unavailable")

        ledger.rows.read = unavailable
    else:
        future = datetime.now(UTC) + timedelta(days=10)
        monkeypatch.setattr(
            "claude_foundry.portal.budget_pricing.datetime", SimpleNamespace(now=lambda tz: future)
        )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            proxy = BudgetProxy(
                ledger,
                store,
                SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="synthetic")),
                "https://test.services.ai.azure.com",
                {
                    "claude-sonnet-4-6": {
                        "provider": "anthropic",
                        "version": "1",
                        "max_input_tokens": 1000,
                        "max_output_tokens": 1000,
                    }
                },
                client,
            )
            with pytest.raises((BudgetDenied, TimeoutError, ValueError, KeyError)):
                await proxy.infer(
                    "anthropic",
                    policy["members"][0]["id"],
                    str(uuid4()),
                    {"model": "claude-sonnet-4-6", "max_tokens": 100},
                )

    asyncio.run(run())
    assert calls == []


def test_warm_policy_cache_preserves_concurrent_monetary_limits(policy, ledger, snapshot_store):
    store, _, downloads = snapshot_store(policy)
    store.get(policy_digest(policy))

    def attempt(_):
        current = store.get(ledger.active())
        try:
            return admit(ledger, current)
        except BudgetDenied:
            return None

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(attempt, range(100)))
    assert sum(r is not None for r in results) == 10
    assert len(downloads) == 1
    assert all(
        s["reserved_micro_usd"] == 1_000_000
        for s in ledger.balances(policy, policy["members"][0]["id"])["scopes"]
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_apim_never_caches_admission_or_semantic_answers(org, provider):
    import xml.etree.ElementTree as ET

    from claude_foundry.budget_gateway import policy_xml

    root = ET.fromstring(
        policy_xml(
            org["tenant_id"],
            "https://budget-test.azurewebsites.net",
            "api://" + str(uuid4()),
            provider,
        )
    )
    assert root.find("inbound/validate-azure-ad-token") is not None
    assert root.find("inbound/authentication-managed-identity") is not None
    assert len(root.findall("backend/forward-request")) == 1
    assert not any("cache" in element.tag for element in root.iter())
    assert root.find(".//return-response") is None
    assert root.find("outbound/set-header[@name='Cache-Control']/value").text == "no-store"


@pytest.mark.parametrize("existing", [False, True])
def test_budget_gateway_uses_full_put_for_create_and_rerun(org, existing):
    from claude_foundry.azure import AzureError
    from claude_foundry.budget_gateway import deploy

    calls = []
    marker = "Managed by claude-codex-foundry budget gateway preview."

    class FakeAzure:
        def request(self, method, path, body=None, version="2024-05-01"):
            calls.append((method, path, body))
            if method == "GET":
                if existing:
                    return {"properties": {"description": marker}}
                raise AzureError(404, "missing")
            return {}

    deploy(
        org,
        FakeAzure(),
        "https://budget-test.azurewebsites.net",
        "api://" + str(uuid4()),
        "openai",
        "codex-governed",
        "codex-governed",
    )
    api_writes = [call for call in calls if call[1].endswith("/apis/codex-governed")]
    assert api_writes[0][0] == "GET"
    assert api_writes[1][0] == ("PATCH" if existing else "PUT")
    if existing:
        assert "apiType" not in api_writes[1][2]["properties"]
    else:
        assert api_writes[1][2]["properties"]["apiType"] == "http"
    assert api_writes[-1][0] == "PATCH"
    assert api_writes[-1][2] == {"properties": {"subscriptionRequired": False}}


@pytest.mark.parametrize("result", ["hit", "miss"])
def test_shared_snapshot_cache_is_verified_and_reused_locally(policy, snapshot_store, result):
    shared = SimpleNamespace(get=lambda digest: (json.dumps(policy).encode(), result))
    store, _, downloads = snapshot_store(policy, shared_cache=shared)
    assert store.get(policy_digest(policy)) == policy
    assert store.get(policy_digest(policy)) == policy
    info = store.cache_info()
    assert downloads == [] and info["shared_reads"] == 1 and info["hits"] == 1
    assert info["shared_hits"] == (result == "hit")


@pytest.mark.parametrize("result", ["hit", "miss", "unknown", "corrupt", "outage", "disabled"])
def test_shared_probe_checks_apim_even_with_warm_l1(policy, snapshot_store, result):
    calls = []

    def get(digest):
        calls.append(digest)
        if result == "outage":
            raise TimeoutError("Synthetic private error")
        return (b"{}" if result == "corrupt" else json.dumps(policy).encode()), result

    store, _, _ = snapshot_store(policy)
    digest = policy_digest(policy)
    store.get(digest)
    store._shared = None if result == "disabled" else SimpleNamespace(get=get)
    before = store.cache_info()
    assert store.probe_shared(digest) == ("failed" if result in {"corrupt", "outage"} else result)
    assert store.cache_info() == before
    assert calls == ([] if result == "disabled" else [digest])


@pytest.mark.parametrize("failure", ["outage", "corrupt", "wrong_version"])
def test_shared_cache_failure_falls_back_only_to_verified_durable_version(
    policy, snapshot_store, failure
):
    def get(digest):
        if failure == "outage":
            raise TimeoutError("Shared cache unavailable")
        return (b"not-json" if failure == "corrupt" else b"{}"), "hit"

    store, documents, downloads = snapshot_store(policy, shared_cache=SimpleNamespace(get=get))
    digest = policy_digest(policy)
    assert store.get(digest) == policy
    assert len(downloads) == 1 and store.cache_info()["shared_failures"] == 1
    fresh, documents, _ = snapshot_store(policy, shared_cache=SimpleNamespace(get=get))
    documents[digest + ".json"] = b"{}"
    with pytest.raises(ValueError, match="integrity"):
        fresh.get(digest)
    assert fresh.cache_info()["entries"] == 0


@pytest.mark.parametrize("status", [200, 302, 401, 403, 429, 500])
def test_apim_snapshot_client_uses_machine_token_and_does_not_follow_redirects(policy, status):
    from claude_foundry.portal.policy_cache import ApimPolicyCache

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, content=json.dumps(policy).encode(), headers={"x-budget-policy-cache": "hit"}
        )

    token_scopes = []

    def token(scope):
        token_scopes.append(scope)
        return SimpleNamespace(token="synthetic-machine-token")

    audience = "api://" + str(uuid4())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        shared = ApimPolicyCache(
            "https://test.azure-api.net/claude-budget-preview/v1/policy-snapshots",
            audience,
            SimpleNamespace(get_token=token),
            client,
        )
        if status == 200:
            assert json.loads(shared.get(policy_digest(policy))[0]) == policy
        else:
            with pytest.raises(httpx.HTTPStatusError):
                shared.get(policy_digest(policy))
    assert len(calls) == 1 and calls[0].headers["authorization"] == "Bearer synthetic-machine-token"
    assert token_scopes == [audience + "/.default"]


def test_shared_snapshot_size_bound(policy):
    from claude_foundry.portal.policy_cache import MAX_SHARED_BYTES, ApimPolicyCache

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"x" * (MAX_SHARED_BYTES + 1))
        )
    ) as client:
        shared = ApimPolicyCache(
            "https://test.azure-api.net/claude-budget-preview/v1/policy-snapshots",
            "api://" + str(uuid4()),
            SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test")),
            client,
        )
        with pytest.raises(ValueError, match="too large"):
            shared.get(policy_digest(policy))


@pytest.mark.parametrize(
    "role", ["owner", "admin", "group_admin", "developer", "billing", "auditor"]
)
def test_cache_diagnostics_require_current_organization_admin(policy, ledger, snapshot_store, role):
    from fastapi import HTTPException

    policy["members"][0]["role"] = role
    ledger.activate(policy)
    store, _, _ = snapshot_store(policy)
    probes = []
    store.probe_shared = lambda digest: probes.append(digest) or "hit"
    proxy = BudgetProxy(
        ledger, store, None, "https://test.services.ai.azure.com", {}, client=object()
    )
    if role in {"owner", "admin"}:
        result = asyncio.run(proxy.cache_status(policy["members"][0]["id"]))
        assert result["scope"] == "current_worker" and result["shared_probe"] == "hit"
        assert probes == [policy_digest(policy)]
    else:
        with pytest.raises(HTTPException) as denied:
            asyncio.run(proxy.cache_status(policy["members"][0]["id"]))
        assert denied.value.status_code == 403
        assert probes == []


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_terminal_event_is_settled_before_client_disconnect(policy, ledger, provider):
    if provider == "openai":
        policy["budget_prices"][0]["provider"] = "openai"
        ledger.activate(policy)
    terminal = (
        [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 2, "output_tokens": 0}},
            },
            {"type": "message_delta", "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
        if provider == "anthropic"
        else [
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 2, "output_tokens": 3}},
            }
        ]
    )

    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in terminal)
            raise AssertionError("Client disconnects without requesting another chunk")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=ProviderStream())
            )
        ) as client:
            proxy = BudgetProxy(
                ledger,
                SimpleNamespace(get=lambda digest: policy),
                SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test")),
                "https://test.services.ai.azure.com",
                {
                    "claude-sonnet-4-6": {
                        "version": "1",
                        "provider": provider,
                        "max_input_tokens": 1000,
                        "max_output_tokens": 1000,
                    }
                },
                client,
            )
            response = await proxy.infer(
                provider,
                policy["members"][0]["id"],
                str(uuid4()),
                {"model": "claude-sonnet-4-6", "stream": True},
            )
            await anext(response.body_iterator)
            # The terminal event has reached the client, but upstream EOF has not.
            assert all(
                scope["spent_micro_usd"] == 51 and scope["reserved_micro_usd"] == 0
                for scope in ledger.balances(policy, policy["members"][0]["id"])["scopes"]
            )
            await response.body_iterator.aclose()

    asyncio.run(run())


def test_openai_stream_eof_emits_terminal_error_and_keeps_reservation(policy, ledger):
    policy["budget_prices"][0]["provider"] = "openai"
    ledger.activate(policy)
    partial = b'data: {"type":"response.created","sequence_number":0}\n\n'

    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield partial

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=ProviderStream())
            )
        ) as client:
            proxy = BudgetProxy(
                ledger,
                SimpleNamespace(get=lambda digest: policy),
                SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test")),
                "https://test.services.ai.azure.com",
                {
                    "claude-sonnet-4-6": {
                        "version": "1",
                        "provider": "openai",
                        "max_input_tokens": 1000,
                        "max_output_tokens": 1000,
                    }
                },
                client,
            )
            response = await proxy.infer(
                "openai",
                policy["members"][0]["id"],
                str(uuid4()),
                {"model": "claude-sonnet-4-6", "stream": True},
            )
            wire = b"".join([chunk async for chunk in response.body_iterator])
            assert wire.startswith(partial)
            events = [
                json.loads(line[6:]) for line in wire.splitlines() if line.startswith(b"data: ")
            ]
            assert events[-1] == {
                "type": "error",
                "error": {
                    "code": "upstream_stream_incomplete",
                    "message": (
                        "The upstream model stream ended before a terminal event; "
                        "the budget reservation remains held for review."
                    ),
                    "param": None,
                },
            }

    asyncio.run(run())
    assert all(
        scope["reserved_micro_usd"] == 4500 and scope["spent_micro_usd"] == 0
        for scope in ledger.balances(policy, policy["members"][0]["id"])["scopes"]
    )


def test_openai_failed_stream_is_normalized_for_native_clients(policy, ledger):
    policy["budget_prices"][0]["provider"] = "openai"
    ledger.activate(policy)
    failed = b'data: {"type":"error","message":"provider failure"}\n\n'

    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield failed

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=ProviderStream())
            )
        ) as client:
            proxy = BudgetProxy(
                ledger,
                SimpleNamespace(get=lambda digest: policy),
                SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test")),
                "https://test.services.ai.azure.com",
                {
                    "claude-sonnet-4-6": {
                        "version": "1",
                        "provider": "openai",
                        "max_input_tokens": 1000,
                        "max_output_tokens": 1000,
                    }
                },
                client,
            )
            response = await proxy.infer(
                "openai",
                policy["members"][0]["id"],
                str(uuid4()),
                {"model": "claude-sonnet-4-6", "stream": True},
            )
            wire = b"".join([chunk async for chunk in response.body_iterator])
            events = [
                json.loads(line[6:]) for line in wire.splitlines() if line.startswith(b"data: ")
            ]
            assert events[0] == {"type": "error", "message": "provider failure"}
            assert events[-1]["error"]["code"] == "upstream_stream_incomplete"

    asyncio.run(run())


def test_explicit_unlimited_scopes_keep_accounting_and_enforce_finite_team(policy):
    policy["settings"].update(spend_limit_usd=None, spend_limit_unlimited=True)
    policy["groups"][0].update(spend_ceiling_usd=None, spend_ceiling_unlimited=True)
    policy = snapshot(policy)
    group = {
        **policy["groups"][0],
        "id": str(uuid4()),
        "spend_ceiling_unlimited": False,
        "spend_ceiling_usd": "400",
        "spend_limit_usd": "400",
    }
    member = {
        **policy["members"][0],
        "id": str(uuid4()),
        "role": "developer",
        "group_id": group["id"],
        "spend_limit_usd": "400",
    }
    policy["groups"].append(group)
    policy["members"].append(member)
    ledger = Ledger(MemoryRows())
    ledger.activate(policy)
    request = admit(ledger, policy, amount=900_000_000)
    ledger.settle(request["request_id"], 500_000_000)
    admit(ledger, policy, amount=900_000_000)
    scopes = ledger.balances(policy, policy["members"][0]["id"])["scopes"]
    assert all(
        s["limit_micro_usd"] is None
        and s["remaining_micro_usd"] is None
        and s["spent_micro_usd"] == 500_000_000
        and s["reserved_micro_usd"] == 900_000_000
        for s in scopes
    )

    def attempt(_):
        try:
            return ledger.reserve(
                str(uuid4()), member["id"], "claude-sonnet-4-6", 100_000_000, policy, "hash"
            )
        except BudgetDenied:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = [r for r in pool.map(attempt, range(12)) if r]
    assert len(accepted) == 4
    scopes = ledger.balances(policy, member["id"])["scopes"]
    assert scopes[0]["limit_micro_usd"] is None
    assert all(
        s["limit_micro_usd"] == 400_000_000 and s["remaining_micro_usd"] == 0 for s in scopes[1:]
    )
    # A full finite team never prevents the unlimited team's admission.
    admit(ledger, policy, amount=900_000_000)
    finite = copy.deepcopy(policy)
    finite["settings"].update(spend_limit_unlimited=False, spend_limit_usd="400")
    with pytest.raises(BudgetDenied, match="below"):
        ledger.activate(finite)
    assert ledger.active() == policy_digest(policy)


@pytest.mark.parametrize("scope", ["organization", "group"])
def test_unlimited_requires_explicit_choice_and_no_conflicting_amount(policy, scope):
    target, value, flag = (
        (policy["settings"], "spend_limit_usd", "spend_limit_unlimited")
        if scope == "organization"
        else (policy["groups"][0], "spend_ceiling_usd", "spend_ceiling_unlimited")
    )
    target[value] = None
    with pytest.raises(ValueError):
        snapshot(policy)
    with pytest.raises(BudgetDenied, match="configured"):
        Ledger(MemoryRows()).activate(policy)
    target[flag] = True
    snapshot(policy)
    target[value] = "1"
    with pytest.raises(ValueError, match="unlimited"):
        snapshot(policy)
    with pytest.raises(BudgetDenied, match="configured"):
        Ledger(MemoryRows()).activate(policy)


def test_group_publication_preserves_owner_unlimited_choice(policy):
    from claude_foundry.portal.budget_policy import group_publication

    actor = {**policy["members"][0], "id": str(uuid4()), "role": "group_admin"}
    draft = copy.deepcopy(policy)
    draft["settings"].update(spend_limit_usd=None, spend_limit_unlimited=True)
    draft["groups"][0].update(spend_ceiling_usd=None, spend_ceiling_unlimited=True)
    published = group_publication(policy, draft, actor)
    assert not published["settings"]["spend_limit_unlimited"]
    assert not published["groups"][0]["spend_ceiling_unlimited"]
    assert published["groups"][0]["spend_ceiling_usd"] == "1"
    # A delegated publication must also preserve an already unlimited owner ceiling.
    policy["groups"][0].update(spend_ceiling_usd=None, spend_ceiling_unlimited=True)
    published = group_publication(policy, draft, actor)
    assert published["groups"][0]["spend_ceiling_unlimited"]
