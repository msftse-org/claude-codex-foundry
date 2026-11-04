"""Synthetic held-path diagnostics; neither telemetry nor metadata authorizes a refund."""

import asyncio
import copy
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import anyio
import httpx
import pytest
from azure.core.exceptions import HttpResponseError
from fastapi import HTTPException
from test_budget import admit
from test_budget import ledger as ledger
from test_budget import policy as policy

from claude_foundry import unified_deployment
from claude_foundry.portal.budget_ledger import Contended
from claude_foundry.portal.budget_pricing import BudgetPrice
from claude_foundry.portal.budget_proxy import BudgetProxy

PRIVATE = (
    "synthetic-private-prompt synthetic-private-response "
    'usage={"input_tokens":987654} Authorization=Bearer-synthetic-private-token '
    "https://synthetic-private.example.invalid"
)
REASONS = (
    "dispatch_unknown",
    "client_disconnected",
    "stream_incomplete",
    "rejected_possibly_billed",
    "usage_unreadable",
    "settlement_failed",
)


class ProviderStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), error=None):
        self.chunks, self.error = chunks, error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


@pytest.fixture(params=["anthropic", "openai"])
def runtime(request, policy, ledger):
    provider = request.param
    policy["budget_prices"][0]["provider"] = provider
    ledger.activate(policy)
    return provider, policy, ledger


def proxy_for(provider, policy, ledger, client, **kwargs):
    return BudgetProxy(
        ledger,
        SimpleNamespace(get=lambda digest: policy),
        SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="synthetic-token")),
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
        **kwargs,
    )


def terminal(provider, usage=None):
    usage = {"input_tokens": 2, "output_tokens": 3} if usage is None else usage
    events = (
        [
            {"type": "message_start", "message": {"usage": usage}},
            {"type": "message_delta", "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
        if provider == "anthropic"
        else [{"type": "response.completed", "response": {"usage": usage}}]
    )
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


def assert_held(ledger, request_id, reason, caplog, error=None):
    reservation = ledger.rows.read("request-" + request_id)[0]
    assert reservation["state"] == "reserved"
    assert reservation["hold_reason"] == reason
    assert datetime.fromisoformat(reservation["held_at"]).utcoffset().total_seconds() == 0
    for key in reservation["counters"]:
        assert ledger.rows.read(key)[0] == {"spent": 0, "reserved": reservation["reserved"]}
    warnings = [
        record for record in caplog.records if record.name == "claude_foundry.portal.budget_proxy"
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].exc_info is None
    message = warnings[0].getMessage()
    assert message.startswith(
        f"Reservation held: request={request_id} reason={reason} error={error or 'None'}"
    )
    assert all(value not in message for value in PRIVATE.split())
    return message


@pytest.mark.parametrize("reason", REASONS)
def test_note_hold_changes_only_metadata(ledger, policy, reason):
    reservation = admit(ledger, policy)
    key = "request-" + reservation["request_id"]
    before = copy.deepcopy(ledger.rows.rows)
    started = datetime.now(UTC)
    ledger.note_hold(reservation["request_id"], reason)
    updated, etag = ledger.rows.read(key)
    assert updated.pop("hold_reason") == reason
    assert started <= datetime.fromisoformat(updated.pop("held_at")) <= datetime.now(UTC)
    assert updated == reservation
    assert etag != before[key][1]
    assert {k: v for k, v in ledger.rows.rows.items() if k != key} == {
        k: v for k, v in before.items() if k != key
    }


@pytest.mark.parametrize("case", ["settled", "missing", "invalid_id", "invalid_reason"])
def test_note_hold_does_nothing_for_non_reserved_or_invalid_input(ledger, policy, case):
    request_id = admit(ledger, policy)["request_id"]
    reason = "dispatch_unknown"
    if case == "settled":
        ledger.settle(request_id, 51)
    elif case == "missing":
        request_id = str(uuid4())
    elif case == "invalid_id":
        request_id = PRIVATE
    else:
        reason = PRIVATE
    before = copy.deepcopy(ledger.rows.rows)
    ledger.note_hold(request_id, reason)
    assert ledger.rows.rows == before


@pytest.mark.parametrize("operation", ["read", "commit"])
def test_note_hold_storage_failure_is_quiet_and_does_not_change_counters(
    ledger, policy, monkeypatch, operation
):
    request_id = admit(ledger, policy)["request_id"]
    before = copy.deepcopy(ledger.rows.rows)

    def unavailable(*args):
        raise TimeoutError(PRIVATE)

    monkeypatch.setattr(ledger.rows, operation, unavailable)
    ledger.note_hold(request_id, "dispatch_unknown")
    assert ledger.rows.rows == before


def test_note_hold_retries_with_a_fresh_etag(ledger, policy, monkeypatch):
    request_id = admit(ledger, policy)["request_id"]
    key = "request-" + request_id
    commit = ledger.rows.commit
    attempts = []

    def contended(writes):
        attempts.append(writes)
        assert len(writes) == 1 and writes[0][0] == key and writes[0][2] is not None
        if len(attempts) < 3:
            current, etag = ledger.rows.read(key)
            commit([(key, {**current, "synthetic_revision": len(attempts)}, etag)])
        commit(writes)

    monkeypatch.setattr(ledger.rows, "commit", contended)
    ledger.note_hold(request_id, "dispatch_unknown")
    assert len(attempts) == 3
    assert len({writes[0][2] for writes in attempts}) == 3
    row = ledger.rows.read(key)[0]
    assert row["hold_reason"] == "dispatch_unknown" and row["synthetic_revision"] == 2


def test_note_hold_contention_is_bounded_without_sleep(ledger, policy, monkeypatch):
    request_id = admit(ledger, policy)["request_id"]
    before = copy.deepcopy(ledger.rows.rows)
    attempts = []

    def contended(writes):
        attempts.append(writes)
        raise Contended()

    monkeypatch.setattr(ledger.rows, "commit", contended)
    ledger.note_hold(request_id, "dispatch_unknown")
    assert len(attempts) == 3
    assert ledger.rows.rows == before


def test_note_hold_requires_an_existing_etag(ledger, policy, monkeypatch):
    reservation = admit(ledger, policy)
    before = copy.deepcopy(ledger.rows.rows)
    monkeypatch.setattr(ledger.rows, "read", lambda key: (reservation, None))
    ledger.note_hold(reservation["request_id"], "dispatch_unknown")
    assert ledger.rows.rows == before


def test_note_hold_ambiguous_write_cannot_change_accounting(ledger, policy, monkeypatch):
    reservation = admit(ledger, policy)
    key = "request-" + reservation["request_id"]
    before = copy.deepcopy(ledger.rows.rows)
    commit = ledger.rows.commit

    def ambiguous(writes):
        commit(writes)
        raise TimeoutError(PRIVATE)

    monkeypatch.setattr(ledger.rows, "commit", ambiguous)
    ledger.note_hold(reservation["request_id"], "dispatch_unknown")
    updated = ledger.rows.read(key)[0]
    assert updated.pop("hold_reason") == "dispatch_unknown"
    updated.pop("held_at")
    assert updated == reservation
    assert {k: v for k, v in ledger.rows.rows.items() if k != key} == {
        k: v for k, v in before.items() if k != key
    }


def test_note_hold_does_not_overwrite_concurrent_settlement(ledger, policy, monkeypatch):
    request_id = admit(ledger, policy)["request_id"]
    commit = ledger.rows.commit
    raced = False

    def settle_first(writes):
        nonlocal raced
        if not raced:
            raced = True
            ledger.settle(request_id, 51)
        commit(writes)

    monkeypatch.setattr(ledger.rows, "commit", settle_first)
    ledger.note_hold(request_id, "dispatch_unknown")
    row = ledger.rows.read("request-" + request_id)[0]
    assert raced and row["state"] == "settled" and row["charged"] == 51
    assert "hold_reason" not in row and "held_at" not in row
    assert all(ledger.rows.read(key)[0] == {"spent": 51, "reserved": 0} for key in row["counters"])


def test_settle_retries_after_concurrent_note_hold(ledger, policy, monkeypatch):
    request_id = admit(ledger, policy)["request_id"]
    commit = ledger.rows.commit
    attempts = []

    def annotate_first(writes):
        if len(writes) > 1:
            attempts.append(writes)
            if len(attempts) == 1:
                ledger.note_hold(request_id, "client_disconnected")
        commit(writes)

    monkeypatch.setattr(ledger.rows, "commit", annotate_first)
    row = ledger.settle(request_id, 51)
    assert len(attempts) == 2
    assert row["state"] == "settled" and row["charged"] == 51
    assert row["hold_reason"] == "client_disconnected"
    assert all(ledger.rows.read(key)[0] == {"spent": 51, "reserved": 0} for key in row["counters"])


@pytest.mark.parametrize("cancelled", [False, True])
def test_dispatch_failure_records_reason(runtime, caplog, cancelled):
    provider, policy, ledger = runtime
    request_id = str(uuid4())

    def handler(request):
        raise asyncio.CancelledError() if cancelled else httpx.ReadTimeout(PRIVATE)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            with pytest.raises(asyncio.CancelledError if cancelled else HTTPException) as error:
                await proxy.infer(
                    provider, policy["members"][0]["id"], request_id, {"model": "claude-sonnet-4-6"}
                )
            if not cancelled:
                assert error.value.status_code == 502

    asyncio.run(run())
    assert_held(
        ledger,
        request_id,
        "client_disconnected" if cancelled else "dispatch_unknown",
        caplog,
        "CancelledError" if cancelled else "ReadTimeout",
    )


def test_pre_dispatch_capture_failure_keeps_original_error(runtime, caplog):
    provider, policy, ledger = runtime
    request_id = str(uuid4())

    def begin(*args):
        raise ValueError(PRIVATE)

    def unexpected_dispatch(request):
        pytest.fail("A failed pre-dispatch capture must not dispatch inference")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_dispatch)) as client:
            proxy = proxy_for(
                provider, policy, ledger, client, content_capture=SimpleNamespace(begin=begin)
            )
            with pytest.raises(ValueError, match="synthetic-private"):
                await proxy.infer(
                    provider, policy["members"][0]["id"], request_id, {"model": "claude-sonnet-4-6"}
                )

    asyncio.run(run())
    assert_held(ledger, request_id, "dispatch_unknown", caplog, "ValueError")


@pytest.mark.parametrize(
    "mode,reason,error",
    [
        ("missing_usage", "usage_unreadable", "ValueError"),
        ("invalid_usage", "usage_unreadable", "ValueError"),
        ("not_object", "usage_unreadable", "AttributeError"),
        ("malformed_json", "usage_unreadable", "JSONDecodeError"),
        ("read_error", "usage_unreadable", "ReadError"),
        ("oversized", "usage_unreadable", "ValueError"),
        ("pricing_error", "usage_unreadable", "ValueError"),
        ("settlement_error", "settlement_failed", "Contended"),
        ("private_storage_error", "settlement_failed", "RuntimeError"),
    ],
)
def test_success_response_held_paths(runtime, monkeypatch, caplog, mode, reason, error):
    provider, policy, ledger = runtime
    request_id = str(uuid4())
    value = {"usage": {"input_tokens": 2, "output_tokens": 3}}
    if mode == "missing_usage":
        value = {"output": PRIVATE}
    elif mode == "invalid_usage":
        value = {"usage": {"input_tokens": PRIVATE, "output_tokens": 3}}
    elif mode == "not_object":
        value = [PRIVATE]
    raw = json.dumps(value).encode()
    if mode == "malformed_json":
        raw = PRIVATE.encode()
    elif mode == "oversized":
        raw = b"x" * (32 * 1024 * 1024 + 1)
    stream = ProviderStream([raw], httpx.ReadError(PRIVATE) if mode == "read_error" else None)

    def fail(*args):
        if mode == "settlement_error":
            raise Contended("Settlement remains reserved; retry reconciliation")
        if mode == "private_storage_error":
            raise RuntimeError(PRIVATE)
        raise ValueError(PRIVATE)

    if mode == "pricing_error":
        monkeypatch.setattr(BudgetPrice, "usage_cost", fail)
    elif mode in {"settlement_error", "private_storage_error"}:
        monkeypatch.setattr(ledger, "settle", fail)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            call = proxy.infer(
                provider, policy["members"][0]["id"], request_id, {"model": "claude-sonnet-4-6"}
            )
            if mode in {"malformed_json", "read_error", "oversized"}:
                with pytest.raises((ValueError, httpx.ReadError)):
                    await call
            else:
                response = await call
                assert response.status_code == 200 and json.loads(response.body) == value

    asyncio.run(run())
    message = assert_held(ledger, request_id, reason, caplog, error)
    assert stream.closed
    if mode == "settlement_error":
        assert "message=Settlement remains reserved; retry reconciliation" in message
    elif mode == "private_storage_error":
        assert "message=" in message and "redacted" in message


@pytest.mark.parametrize("streaming", [False, True])
def test_overrun_still_pauses_enforcement_without_releasing(runtime, caplog, streaming):
    provider, policy, ledger = runtime
    request_id = str(uuid4())
    usage = {"input_tokens": 1001, "output_tokens": 3}
    stream = ProviderStream(
        [terminal(provider, usage) if streaming else json.dumps({"usage": usage}).encode()]
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            response = await proxy.infer(
                provider,
                policy["members"][0]["id"],
                request_id,
                {"model": "claude-sonnet-4-6", "stream": streaming},
            )
            if streaming:
                assert b"budget reservation remains held" in b"".join(
                    [chunk async for chunk in response.body_iterator]
                )
            else:
                assert json.loads(response.body) == {"usage": usage}

    asyncio.run(run())
    message = assert_held(ledger, request_id, "settlement_failed", caplog, "BudgetDenied")
    assert "message=Usage exceeded its verified reservation; enforcement is paused" in message
    assert ledger.rows.read("policy")[0]["paused"] is True


@pytest.mark.parametrize("status", [200, 401, 503])
@pytest.mark.parametrize("cancelled", [False, True])
def test_response_read_failure_keeps_hold(runtime, caplog, status, cancelled):
    provider, policy, ledger = runtime
    request_id = str(uuid4())
    stream = ProviderStream(
        [b'{"synthetic":'],
        asyncio.CancelledError() if cancelled else httpx.ReadError(PRIVATE),
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, stream=stream))
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            with pytest.raises(asyncio.CancelledError if cancelled else httpx.ReadError):
                await proxy.infer(
                    provider, policy["members"][0]["id"], request_id, {"model": "claude-sonnet-4-6"}
                )

    asyncio.run(run())
    reason = (
        "client_disconnected"
        if cancelled
        else "usage_unreadable"
        if status == 200
        else "rejected_possibly_billed"
    )
    assert_held(ledger, request_id, reason, caplog, "CancelledError" if cancelled else "ReadError")
    assert stream.closed


@pytest.mark.parametrize("kind", ["storage", "untrusted_local_message"])
def test_settlement_diagnostic_redacts_unknown_messages(runtime, monkeypatch, caplog, kind):
    provider, policy, ledger = runtime
    request_id = str(uuid4())
    error = (
        HttpResponseError(PRIVATE)
        if kind == "storage"
        else Contended("Settlement remains reserved; retry reconciliation " + PRIVATE)
    )
    if kind == "storage":
        error.status_code = 503

    def fail(*args):
        raise error

    monkeypatch.setattr(ledger, "settle", fail)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"usage": {"input_tokens": 2, "output_tokens": 3}}
                )
            )
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            response = await proxy.infer(
                provider, policy["members"][0]["id"], request_id, {"model": "claude-sonnet-4-6"}
            )
            assert response.status_code == 200

    asyncio.run(run())
    message = assert_held(ledger, request_id, "settlement_failed", caplog, type(error).__name__)
    assert "message=" in message and "redacted" in message
    if kind == "storage":
        assert "(HTTP 503)" in message


@pytest.mark.parametrize("status", [400, 413, 415, 422, 500, 503])
def test_possibly_billed_rejection_is_held(runtime, caplog, status):
    provider, policy, ledger = runtime
    request_id = str(uuid4())
    value = {"error": {"code": "content_filter", "message": PRIVATE}}

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=value))
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            response = await proxy.infer(
                provider, policy["members"][0]["id"], request_id, {"model": "claude-sonnet-4-6"}
            )
            assert response.status_code == status and json.loads(response.body) == value

    asyncio.run(run())
    assert_held(ledger, request_id, "rejected_possibly_billed", caplog)


@pytest.mark.parametrize("status", [401, 403, 404, 429])
@pytest.mark.parametrize("failed", [False, True])
def test_existing_safe_release_is_unchanged(runtime, monkeypatch, caplog, status, failed):
    provider, policy, ledger = runtime
    request_id = str(uuid4())
    value = {"error": {"message": PRIVATE}}
    if failed:

        def unavailable(*args):
            raise TimeoutError(PRIVATE)

        monkeypatch.setattr(ledger, "settle", unavailable)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=value))
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            response = await proxy.infer(
                provider, policy["members"][0]["id"], request_id, {"model": "claude-sonnet-4-6"}
            )
            assert response.status_code == status and json.loads(response.body) == value

    asyncio.run(run())
    if failed:
        assert_held(ledger, request_id, "settlement_failed", caplog, "TimeoutError")
    else:
        row = ledger.rows.read("request-" + request_id)[0]
        assert row["state"] == "settled" and row["charged"] == 0
        assert "hold_reason" not in row and not caplog.records


@pytest.mark.parametrize(
    "mode,reason,error",
    [
        ("eof", "stream_incomplete", "ValueError"),
        ("missing_terminal_usage", "stream_incomplete", "ValueError"),
        ("response_error", "stream_incomplete", "ValueError"),
        ("read_error", "stream_incomplete", "ReadError"),
        ("pricing_error", "usage_unreadable", "ValueError"),
        ("settlement_error", "settlement_failed", "Contended"),
    ],
)
def test_stream_held_paths(runtime, monkeypatch, caplog, mode, reason, error):
    provider, policy, ledger = runtime
    request_id = str(uuid4())
    chunk = b'data: {"type":"synthetic.partial"}\n\n'
    if mode == "missing_terminal_usage":
        chunk = terminal(provider, {"output_tokens": 3})
    elif mode == "response_error":
        chunk = b"data: " + json.dumps({"type": "error", "message": PRIVATE}).encode() + b"\n\n"
    elif mode in {"pricing_error", "settlement_error"}:
        chunk = terminal(provider)

    def fail(*args):
        if mode == "settlement_error":
            raise Contended("Settlement remains reserved; retry reconciliation")
        raise ValueError(PRIVATE)

    if mode == "pricing_error":
        monkeypatch.setattr(BudgetPrice, "usage_cost", fail)
    elif mode == "settlement_error":
        monkeypatch.setattr(ledger, "settle", fail)
    stream = ProviderStream([chunk], httpx.ReadError(PRIVATE) if mode == "read_error" else None)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            response = await proxy.infer(
                provider,
                policy["members"][0]["id"],
                request_id,
                {"model": "claude-sonnet-4-6", "stream": True},
            )
            wire = b"".join([part async for part in response.body_iterator])
            assert b"budget reservation remains held" in wire

    asyncio.run(run())
    assert_held(ledger, request_id, reason, caplog, error)
    assert stream.closed


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio", "close"])
def test_client_disconnect_before_terminal_records_once(runtime, caplog, cancellation):
    provider, policy, ledger = runtime
    request_id = str(uuid4())

    async def run():
        reading = asyncio.Event()

        class WaitingStream(ProviderStream):
            async def __aiter__(self):
                yield b'data: {"type":"synthetic.partial"}\n\n'
                reading.set()
                await asyncio.Event().wait()

        stream = WaitingStream()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            response = await proxy.infer(
                provider,
                policy["members"][0]["id"],
                request_id,
                {"model": "claude-sonnet-4-6", "stream": True},
            )
            await anext(response.body_iterator)
            if cancellation == "close":
                await response.body_iterator.aclose()
            elif cancellation == "asyncio":
                task = asyncio.create_task(anext(response.body_iterator))
                await reading.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(anext, response.body_iterator)
                    await reading.wait()
                    tasks.cancel_scope.cancel()
        assert stream.closed

    asyncio.run(run())
    assert_held(
        ledger,
        request_id,
        "client_disconnected",
        caplog,
        "GeneratorExit" if cancellation == "close" else "CancelledError",
    )


@pytest.mark.parametrize("complete", [False, True])
def test_close_after_final_event_does_not_add_a_second_warning(runtime, caplog, complete):
    provider, policy, ledger = runtime
    request_id = str(uuid4())
    stream = ProviderStream([terminal(provider)] if complete else [])

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            response = await proxy.infer(
                provider,
                policy["members"][0]["id"],
                request_id,
                {"model": "claude-sonnet-4-6", "stream": True},
            )
            await anext(response.body_iterator)
            if not complete:
                assert_held(ledger, request_id, "stream_incomplete", caplog, "ValueError")
            await response.body_iterator.aclose()

    asyncio.run(run())
    if complete:
        row = ledger.rows.read("request-" + request_id)[0]
        assert row["state"] == "settled" and row["charged"] == 51
        assert "hold_reason" not in row and not caplog.records
    else:
        assert_held(ledger, request_id, "stream_incomplete", caplog, "ValueError")
    assert stream.closed


@pytest.mark.parametrize("streaming", [False, True])
def test_note_hold_failure_cannot_break_the_response(runtime, monkeypatch, caplog, streaming):
    provider, policy, ledger = runtime
    request_id = str(uuid4())

    def unavailable(*args):
        raise TimeoutError(PRIVATE)

    monkeypatch.setattr(ledger, "note_hold", unavailable)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (
                    httpx.Response(200, stream=ProviderStream([]))
                    if streaming
                    else httpx.Response(200, json={})
                )
            )
        ) as client:
            proxy = proxy_for(provider, policy, ledger, client)
            response = await proxy.infer(
                provider,
                policy["members"][0]["id"],
                request_id,
                {"model": "claude-sonnet-4-6", "stream": streaming},
            )
            assert response.status_code == 200
            if streaming:
                assert b"budget reservation remains held" in b"".join(
                    [chunk async for chunk in response.body_iterator]
                )
            else:
                assert json.loads(response.body) == {}

    asyncio.run(run())
    row = ledger.rows.read("request-" + request_id)[0]
    assert row["state"] == "reserved" and "hold_reason" not in row
    assert all(
        ledger.rows.read(key)[0] == {"spent": 0, "reserved": 4500} for key in row["counters"]
    )
    reason = "stream_incomplete" if streaming else "usage_unreadable"
    assert len(caplog.records) == 1 and f"reason={reason}" in caplog.text
    assert PRIVATE not in caplog.text


def test_runtime_warning_and_exception_telemetry_is_not_sampled():
    host = json.loads(
        (Path(__file__).resolve().parents[1] / "budget/functions/host.json").read_text()
    )
    assert host["logging"]["logLevel"]["default"] == "Warning"
    sampling = host["logging"]["applicationInsights"]["samplingSettings"]
    assert {"Trace", "Exception"} <= set(sampling["excludedTypes"].split(";"))


@pytest.mark.parametrize("existing", [False, True])
def test_runtime_parameters_use_the_portals_insights_scope(tmp_path, monkeypatch, existing):
    local = tmp_path / "synthetic-state"
    (local / "unified").mkdir(parents=True)
    audience = "api://00000000-0000-0000-0000-000000000001"
    (local / "unified/entra.json").write_text(json.dumps({"audience": audience}))
    calls = []
    job = SimpleNamespace(
        local=local,
        dist=tmp_path / "packages",
        s={
            "suffix": "synthetic",
            "subscription_id": "00000000-0000-0000-0000-000000000002",
            "tenant_id": "00000000-0000-0000-0000-000000000003",
            "location": "eastus2",
        },
        n={
            "portal": "synthetic-portal",
            "portal_resource_group": "synthetic-portal-rg",
            "insights": "synthetic-insights",
            "resource_group": "synthetic-foundation-rg",
        },
        unified_runtime_settings={"BUDGET_AUDIENCE": audience} if existing else {},
        azure=lambda subscription: SimpleNamespace(
            request=lambda *args: {
                "identity": {"principalId": "00000000-0000-0000-0000-000000000004"}
            }
        ),
        az=lambda *args: calls.append(args),
        step=lambda *args: None,
        script=lambda *args: None,
        run=lambda *args: None,
        save=lambda **kwargs: None,
    )
    monkeypatch.setattr(unified_deployment, "publish", lambda *args: None)
    unified_deployment.deploy_runtime(
        job,
        {
            "subscription_id": job.s["subscription_id"],
            "apim_resource_group": "synthetic-gateway-rg",
            "apim_name": "synthetic-gateway",
            "api_id": "synthetic-api",
            "foundry_name": "synthetic-foundry",
            "foundry_resource_group": "synthetic-foundry-rg",
        },
        {
            "budgetOrganizationId": job.s["subscription_id"],
            "budgetStorageAccount": "syntheticstorage",
            "providerConfigs": {},
        },
        {},
        "00000000-0000-0000-0000-000000000005",
    )
    parameters = json.loads((local / "unified/runtime-parameters.json").read_text())["parameters"]
    assert parameters["insightsName"]["value"] == job.n["insights"]
    assert parameters["insightsResourceGroup"]["value"] == job.n["resource_group"]
    assert len(calls) == 1 and calls[0][:3] == ("deployment", "group", "create")
