"""Synthetic, offline coverage for Claude Code auto mode through governed routes."""

import asyncio
import copy
import hashlib
import importlib.util
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import azure.functions
import azure.identity
import httpx
import pytest
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from claude_foundry.portal import budget_ledger, budget_policy, budget_proxy
from claude_foundry.portal.budget_ledger import BudgetDenied, Ledger, MemoryRows
from claude_foundry.portal.budget_policy import snapshot
from claude_foundry.portal.budget_pricing import BudgetPrice
from claude_foundry.portal.budget_proxy import BudgetProxy
from claude_foundry.portal.content_capture import ContentCapture
from claude_foundry.portal.domain import seed

SAFEGUARDS = [
    {
        "type": "dangerous_tool_use",
        "classifier_context": {
            "permission_mode": "auto",
            "live_cwd": "/Users/example/project",
            "trusted_directories": ["/Users/example/trusted-project"],
        },
    }
]
PRIVATE_MARKERS = (
    "safeguards",
    "classifier_context",
    "/Users/example/project",
    "/Users/example/trusted-project",
)
GENERIC_ERROR = {"error": {"message": "Invalid request or expired model verification"}}


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
        {
            "model": "claude-sonnet-4-6",
            "provider": "anthropic",
            "deployment_version": "1",
            "max_input_tokens": 1000,
            "max_output_tokens": 1000,
            "default_output_tokens": 100,
            "input_ceiling_per_million": "3",
            "output_ceiling_per_million": "15",
            "source_url": "https://example.com/price",
            "verified_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "valid_until": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            "note": "Synthetic test pricing and capacity, never production rates",
        }
    ]
    return snapshot(state)


def message_body(stream=False):
    return {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Read the synthetic café.txt fixture."}],
        "max_tokens": 100,
        "service_tier": "standard_only",
        "stream": stream,
        "tools": [
            {
                "name": "read_file",
                "description": "Read a synthetic fixture",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
    }


def count_body():
    return {
        key: value for key, value in message_body().items() if key in {"model", "messages", "tools"}
    }


def encoded(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def upstream_response(request):
    if request.url.path.endswith("/count_tokens"):
        return httpx.Response(200, json={"input_tokens": 7})
    content = [
        {
            "type": "tool_use",
            "id": "tool_synthetic",
            "name": "read_file",
            "input": {"path": "fixture.txt"},
        }
    ]
    if json.loads(request.content).get("stream"):
        events = [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 2, "output_tokens": 0}},
            },
            {"type": "content_block_start", "index": 0, "content_block": content[0]},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 3},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(
            200,
            content=b"".join(b"data: " + encoded(event) + b"\n\n" for event in events),
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(
        200,
        json={
            "type": "message",
            "role": "assistant",
            "content": content,
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
    )


@asynccontextmanager
async def gateway(policy, upstream=upstream_response):
    ledger = Ledger(MemoryRows())
    ledger.activate(policy)
    ledger.reserve = Mock(wraps=ledger.reserve)
    ledger.settle = Mock(wraps=ledger.settle)
    capture = object.__new__(ContentCapture)
    capture.max_bytes = 262144
    capture.container = SimpleNamespace(upload_blob=Mock())
    requests = []

    def handle(request):
        requests.append(request)
        return upstream(request)

    credential = SimpleNamespace(get_token=Mock(return_value=SimpleNamespace(token="synthetic")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        proxy = BudgetProxy(
            ledger,
            SimpleNamespace(get=lambda digest: policy),
            credential,
            "https://contoso.services.ai.azure.com",
            {
                "claude-sonnet-4-6": {
                    "provider": "anthropic",
                    "version": "1",
                    "max_input_tokens": 1000,
                    "max_output_tokens": 1000,
                }
            },
            client,
            content_capture=capture,
        )
        yield SimpleNamespace(
            proxy=proxy,
            ledger=ledger,
            capture=capture,
            requests=requests,
            principal=policy["members"][0]["id"],
            credential=credential,
        )


async def consume(response):
    if isinstance(response, StreamingResponse):
        return b"".join([chunk async for chunk in response.body_iterator])
    return response.body


def assert_private_context_absent(value):
    for marker in PRIVATE_MARKERS:
        assert marker not in value


@pytest.fixture
def function_host(monkeypatch, policy):
    def load(proxy):
        environment = {
            "AZURE_CLIENT_ID": str(uuid4()),
            "BUDGET_TENANT_ID": str(uuid4()),
            "BUDGET_AUDIENCE": "api://synthetic",
            "BUDGET_APIM_PRINCIPAL_ID": str(uuid4()),
            "BUDGET_STORAGE_ACCOUNT": "contoso",
            "BUDGET_ORGANIZATION_ID": str(uuid4()),
            "BUDGET_FOUNDRY_ENDPOINT": "https://contoso.services.ai.azure.com",
            "BUDGET_DEPLOYMENTS_JSON": "{}",
        }
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        for key in ("BUDGET_BOOTSTRAP_JSON", "BUDGET_CONTENT_LOGS_ENABLED"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(
            azure.functions,
            "FunctionApp",
            lambda **kwargs: SimpleNamespace(route=lambda **kwargs: lambda handler: handler),
        )
        monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", Mock())
        monkeypatch.setattr(
            budget_proxy, "GatewayIdentity", Mock(return_value=SimpleNamespace(validate=Mock()))
        )
        monkeypatch.setattr(budget_proxy, "BudgetProxy", Mock(return_value=proxy))
        monkeypatch.setattr(budget_ledger, "AzureRows", Mock(return_value=MemoryRows()))
        monkeypatch.setattr(
            budget_policy,
            "Snapshots",
            Mock(return_value=SimpleNamespace(get=lambda digest: policy)),
        )
        path = Path(__file__).resolve().parents[1] / "budget/functions/function_app.py"
        spec = importlib.util.spec_from_file_location("claude_safeguards_function_app", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.inference

    return load


def request(body, operation="messages", principal=None):
    async def stream():
        yield encoded(body)

    return SimpleNamespace(
        method="POST",
        path_params={"provider": "anthropic", "operation": operation},
        headers={
            "authorization": "Bearer synthetic",
            "x-budget-principal-id": principal or str(uuid4()),
            "x-budget-request-id": str(uuid4()),
        },
        stream=stream,
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("safeguards", [SAFEGUARDS, [], None, {"opaque": "ignored"}])
def test_message_payload_fingerprint_and_reservation_are_equivalent(
    policy, stream, safeguards, caplog
):
    caplog.set_level(logging.DEBUG)
    request_id = str(uuid4())
    plain = message_body(stream)
    auto = {**plain, "safeguards": copy.deepcopy(safeguards)}
    original = copy.deepcopy(auto)

    async def run(body):
        async with gateway(policy) as test:
            response = await test.proxy.infer("anthropic", test.principal, request_id, body)
            result = await consume(response)
            assert response.status_code == 200
            assert b"tool_use" in result
            assert b"safeguard_results" not in result
            assert test.ledger.reserve.call_count == test.ledger.settle.call_count == 1
            assert test.ledger.reserve.call_args.args[3] == 4500
            assert test.ledger.settle.call_args.args == (request_id, 51)
            assert test.ledger.rows.pending() == []
            assert_private_context_absent(repr(test.ledger.rows.rows))
            uploads = test.capture.container.upload_blob.call_args_list
            assert len(uploads) == 2
            for upload in uploads:
                record = json.loads(upload.args[1])
                assert json.loads(record["request"]["content"]) == plain
                assert_private_context_absent(upload.args[1].decode())
            fingerprint = hashlib.sha256(
                json.dumps(plain, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            assert test.ledger.reserve.call_args.args[-1] == fingerprint
            return test.requests[0].content, test.ledger.reserve.call_args

    baseline = asyncio.run(run(plain))
    with_safeguards = asyncio.run(run(auto))
    assert baseline == with_safeguards
    assert baseline[0] == encoded(plain)
    assert auto == original
    assert_private_context_absent(caplog.text)


@pytest.mark.parametrize("safeguards", [SAFEGUARDS, [], None, {"opaque": "ignored"}])
def test_count_tokens_payload_is_equivalent_without_reserving(policy, safeguards, caplog):
    caplog.set_level(logging.DEBUG)
    plain = count_body()
    auto = {**plain, "safeguards": copy.deepcopy(safeguards)}
    original = copy.deepcopy(auto)

    async def run(body):
        async with gateway(policy) as test:
            before = copy.deepcopy(test.ledger.rows.rows)
            response = await test.proxy.count_tokens(test.principal, body)
            assert response.status_code == 200
            assert json.loads(response.body) == {"input_tokens": 7}
            test.ledger.reserve.assert_not_called()
            test.ledger.settle.assert_not_called()
            test.capture.container.upload_blob.assert_not_called()
            assert test.ledger.rows.rows == before
            return test.requests[0].content

    assert asyncio.run(run(plain)) == asyncio.run(run(auto)) == encoded(plain)
    assert auto == original
    assert_private_context_absent(caplog.text)


@pytest.mark.parametrize("mode", ["default", "acceptEdits"])
@pytest.mark.parametrize("operation", ["messages", "streaming-messages", "count-tokens"])
def test_requests_without_safeguards_are_byte_for_byte_unchanged(policy, mode, operation):
    body = (
        count_body()
        if operation == "count-tokens"
        else message_body(operation.startswith("stream"))
    )
    body["messages"][0]["content"] += f" Synthetic {mode} regression."
    before = encoded(body)

    async def run():
        async with gateway(policy) as test:
            if operation == "count-tokens":
                response = await test.proxy.count_tokens(test.principal, body)
            else:
                response = await test.proxy.infer("anthropic", test.principal, str(uuid4()), body)
            await consume(response)
            assert response.status_code == 200
            assert test.requests[0].content == before

    asyncio.run(run())
    assert encoded(body) == before


@pytest.mark.parametrize("operation", ["messages", "count-tokens"])
def test_unknown_fields_are_named_without_their_values(policy, function_host, operation, caplog):
    caplog.set_level(logging.DEBUG)
    body = count_body() if operation == "count-tokens" else message_body()
    body.update(foo="synthetic-value-never-echo", safeguards=copy.deepcopy(SAFEGUARDS))

    async def run():
        async with gateway(policy) as test:
            handler = function_host(test.proxy)
            response = await handler(request(body, operation, test.principal))
            assert response.status_code == 422
            assert json.loads(response.body) == {
                "error": {"message": "Unsupported request fields: foo"}
            }
            assert not test.requests
            test.ledger.reserve.assert_not_called()
            test.credential.get_token.assert_not_called()
            test.capture.container.upload_blob.assert_not_called()

    asyncio.run(run())
    assert "synthetic-value-never-echo" not in caplog.text
    assert_private_context_absent(caplog.text)


def test_unsupported_field_names_are_sorted_limited_and_truncated(policy, function_host):
    fields = ["zeta", "e" * 90, "beta", "alpha", "delta", "gamma", "epsilon"]
    body = {**message_body(), **dict.fromkeys(fields, "synthetic-value-never-echo")}
    expected = ", ".join(name[:64] for name in sorted(fields)[:5])

    async def run():
        async with gateway(policy) as test:
            handler = function_host(test.proxy)
            response = await handler(request(body, principal=test.principal))
            assert response.status_code == 422
            assert json.loads(response.body) == {
                "error": {"message": "Unsupported request fields: " + expected}
            }
            assert not test.requests
            test.ledger.reserve.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("field", ["stream", "max_tokens", "output_config", "metadata"])
def test_count_tokens_keeps_its_narrow_allowlist(policy, function_host, field):
    values = {"stream": True, "max_tokens": 100, "output_config": {}, "metadata": {}}
    body = {**count_body(), field: values[field], "safeguards": copy.deepcopy(SAFEGUARDS)}

    async def run():
        async with gateway(policy) as test:
            handler = function_host(test.proxy)
            response = await handler(request(body, "count-tokens", test.principal))
            assert response.status_code == 422
            assert json.loads(response.body) == {
                "error": {"message": "Unsupported request fields: " + field}
            }
            assert not test.requests
            test.ledger.reserve.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("error_type", [TypeError, ValueError])
@pytest.mark.parametrize("operation", ["messages", "count-tokens"])
def test_other_validation_errors_keep_the_generic_response(
    policy, function_host, error_type, operation, caplog
):
    caplog.set_level(logging.DEBUG)
    private = "/Users/example/project"
    proxy = SimpleNamespace(
        infer=AsyncMock(side_effect=error_type(private)),
        count_tokens=AsyncMock(side_effect=error_type(private)),
    )
    handler = function_host(proxy)
    body = {**message_body(), "safeguards": copy.deepcopy(SAFEGUARDS)}
    response = asyncio.run(handler(request(body, operation)))
    assert response.status_code == 422
    assert json.loads(response.body) == GENERIC_ERROR
    assert_private_context_absent(caplog.text)


@pytest.mark.parametrize(
    "invalid",
    [
        {"max_tokens": 0},
        {"max_tokens": 1001},
        {"max_tokens": True},
        {"max_tokens": "100"},
        {"stream": "true"},
        {"tools": None},
        {"tools": [{"type": "web_search_20250305"}]},
        {"service_tier": "priority"},
        {"context_management": {"edits": [{"type": "compact_20260112"}]}},
    ],
)
def test_safeguards_do_not_relax_verified_budget_bounds(policy, invalid):
    price = BudgetPrice.model_validate(policy["budget_prices"][0])
    body = {**message_body(), **invalid, "safeguards": copy.deepcopy(SAFEGUARDS)}
    with pytest.raises(ValueError):
        price.prepare(body)


@pytest.mark.parametrize(
    ("maximum", "output_bound", "reserved"), [(None, 100, 4500), (1, 1, 3015), (1000, 1000, 18000)]
)
def test_safeguards_preserve_full_capacity_reservations(policy, maximum, output_bound, reserved):
    price = BudgetPrice.model_validate(policy["budget_prices"][0])
    body = {**message_body(), "max_tokens": maximum}
    expected = price.prepare(body)
    assert price.prepare({**body, "safeguards": copy.deepcopy(SAFEGUARDS)}) == expected
    assert expected[0]["max_tokens"] == output_bound
    assert expected[1:] == (reserved, output_bound)


def test_existing_anthropic_defaults_and_openai_payload_are_preserved(policy):
    price = BudgetPrice.model_validate(policy["budget_prices"][0])
    base = {"model": price.model, "messages": []}
    assert price.prepare(base) == (
        {**base, "max_tokens": 100, "service_tier": "standard_only"},
        4500,
        100,
    )
    openai = BudgetPrice.model_validate(
        {**policy["budget_prices"][0], "model": "gpt-test", "provider": "openai"}
    )
    body = {"model": openai.model, "input": "Synthetic fixture", "max_output_tokens": 100}
    assert openai.prepare(body) == (
        {**body, "service_tier": "default", "store": False},
        4500,
        100,
    )
    with pytest.raises(ValueError, match="Unsupported request fields: safeguards"):
        openai.prepare({**body, "safeguards": copy.deepcopy(SAFEGUARDS)})


@pytest.mark.parametrize("stream", [False, True])
def test_fallback_classifier_requests_use_normal_admission_and_settlement(policy, stream):
    async def run():
        async with gateway(policy) as test:
            main = {**message_body(stream), "safeguards": copy.deepcopy(SAFEGUARDS)}
            classifier = {
                **message_body(stream),
                "messages": [
                    {"role": "user", "content": "Classify the synthetic read_file tool call."}
                ],
            }
            for body in (main, classifier):
                response = await test.proxy.infer("anthropic", test.principal, str(uuid4()), body)
                assert b"safeguard_results" not in await consume(response)
            assert len(test.requests) == 2
            assert test.ledger.reserve.call_count == test.ledger.settle.call_count == 2
            assert all(call.args[3] == 4500 for call in test.ledger.reserve.call_args_list)
            assert all(call.args[1] == 51 for call in test.ledger.settle.call_args_list)
            assert test.ledger.rows.pending() == []
            balances = test.ledger.balances(policy, test.principal)
            assert all(
                scope["spent_micro_usd"] == 102 and scope["reserved_micro_usd"] == 0
                for scope in balances["scopes"]
            )
            test.ledger.reserve(
                str(uuid4()), test.principal, "claude-sonnet-4-6", 995_500, policy, "synthetic"
            )
            with pytest.raises(BudgetDenied, match="insufficient"):
                await test.proxy.infer("anthropic", test.principal, str(uuid4()), classifier)
            assert len(test.requests) == 2

    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_tool_results_follow_the_same_governed_request_path(policy, stream):
    async def run():
        async with gateway(policy) as test:
            body = {**message_body(stream), "safeguards": copy.deepcopy(SAFEGUARDS)}
            response = await test.proxy.infer("anthropic", test.principal, str(uuid4()), body)
            result = await consume(response)
            assert b"tool_synthetic" in result
            followup = copy.deepcopy(body)
            followup["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool_synthetic",
                                "name": "read_file",
                                "input": {"path": "fixture.txt"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool_synthetic",
                                "content": "Synthetic fixture content",
                            }
                        ],
                    },
                ]
            )
            response = await test.proxy.infer("anthropic", test.principal, str(uuid4()), followup)
            assert b"safeguard_results" not in await consume(response)
            assert response.status_code == 200
            forwarded = json.loads(test.requests[-1].content)
            assert forwarded["messages"] == followup["messages"]
            assert "safeguards" not in forwarded
            assert test.ledger.reserve.call_count == test.ledger.settle.call_count == 2
            assert all(call.args[3] == 4500 for call in test.ledger.reserve.call_args_list)
            assert test.ledger.rows.pending() == []

    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_safeguard_context_is_absent_from_failure_logs_and_stored_state(policy, stream, caplog):
    caplog.set_level(logging.DEBUG)

    def fail(request):
        raise httpx.ReadError("Synthetic upstream failure")

    async def run():
        async with gateway(policy, fail) as test:
            body = {**message_body(stream), "safeguards": copy.deepcopy(SAFEGUARDS)}
            with pytest.raises(HTTPException) as caught:
                await test.proxy.infer("anthropic", test.principal, str(uuid4()), body)
            assert caught.value.status_code == 502
            assert len(test.ledger.rows.pending()) == 1
            test.ledger.settle.assert_not_called()
            assert_private_context_absent(repr(test.ledger.rows.rows))
            for upload in test.capture.container.upload_blob.call_args_list:
                assert_private_context_absent(upload.args[1].decode())

    asyncio.run(run())
    assert "Inference outcome unknown; reservation held" in caplog.text
    assert_private_context_absent(caplog.text)
