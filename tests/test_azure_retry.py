import io
import json
import socket
from datetime import UTC, datetime
from email.message import Message
from email.utils import format_datetime
from http.client import RemoteDisconnected
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from claude_foundry import azure as arm
from claude_foundry.azure import Azure, AzureError

PATH = "/resources/example"


class LocalClock:
    def __init__(self):
        self.elapsed = 0
        self.waits = []
        self.epoch = datetime(2026, 1, 1, tzinfo=UTC).timestamp()

    def monotonic(self):
        return self.elapsed

    def time(self):
        return self.epoch + self.elapsed

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.elapsed += seconds


def http_error(status=503, body=None, headers=None):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    if body is None:
        body = {"error": {"code": "ServiceUnavailable", "message": "Try again later"}}
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    return HTTPError(
        "https://management.azure.com" + PATH,
        status,
        "Synthetic ARM failure",
        message,
        io.BytesIO(body),
    )


@pytest.fixture
def local_arm(monkeypatch):
    clock = LocalClock()
    client = Azure(
        "subscription-placeholder",
        sleep=clock.sleep,
        clock=clock.monotonic,
        wall_clock=clock.time,
        jitter=lambda low, high: low,
    )
    monkeypatch.setattr(client, "token", lambda: "not-a-live-token")
    outcomes = []
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout, clock.elapsed))
        outcome = outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(timeout)
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(outcome)

    monkeypatch.setattr(arm, "urlopen", open_request)
    return SimpleNamespace(client=client, clock=clock, outcomes=outcomes, calls=calls)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "HEAD", "POST"])
def test_transient_http_then_200_returns_result(local_arm, capsys, method, status):
    local_arm.outcomes.extend([http_error(status), b'{"ok": true}'])

    result = local_arm.client.request(method, PATH, idempotent=method == "POST")

    assert result == {"ok": True}
    assert len(local_arm.calls) == 2
    assert local_arm.clock.waits == [2]
    assert capsys.readouterr().err == (f"Azure {method} {PATH}: HTTP {status}; retrying in 2s\n")


@pytest.mark.parametrize(
    ("headers", "wait"),
    [
        ({"Retry-After": "7"}, 7),
        ({"retry-after": "7"}, 7),
        ({"x-ms-retry-after-ms": "7500"}, 7.5),
        ({"Retry-After": "7", "x-ms-retry-after-ms": "9000"}, 9),
        ({"Retry-After": "-1"}, 2),
        ({"Retry-After": "not a date", "x-ms-retry-after-ms": "bad"}, 2),
        ({"Retry-After": "NaN", "x-ms-retry-after-ms": "inf"}, 2),
        ({"Retry-After": "0", "x-ms-retry-after-ms": "500"}, 2),
    ],
)
def test_retry_delay_headers(local_arm, headers, wait):
    local_arm.outcomes.extend([http_error(429, headers=headers), b"{}"])

    assert local_arm.client.request("GET", PATH) == {}
    assert local_arm.clock.waits == [wait]
    assert local_arm.calls[1][2] >= wait


def test_http_date_retry_after_accounts_for_request_time(local_arm):
    date = format_datetime(datetime.fromtimestamp(local_arm.clock.time() + 17, UTC), usegmt=True)

    def unavailable(timeout):
        local_arm.clock.elapsed += 7
        return http_error(429, headers={"Retry-After": date})

    local_arm.outcomes.extend([unavailable, b"{}"])

    assert local_arm.client.request("GET", PATH) == {}
    assert local_arm.clock.waits == [10]
    assert local_arm.calls[1][2] == 17


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "error",
    [
        URLError("Temporary connection failure"),
        URLError(TimeoutError("Timed out")),
        ConnectionError("Connection failed"),
        ConnectionResetError("Connection reset"),
        TimeoutError("Timed out"),
        socket.timeout("Socket timed out"),  # noqa: UP041 - exercise the socket API alias
        RemoteDisconnected("Remote end closed connection"),
    ],
)
def test_connection_failures_are_retried_when_safe(local_arm, method, error):
    local_arm.outcomes.extend([error, b'{"ok": true}'])

    assert local_arm.client.request(method, PATH, idempotent=method == "POST") == {"ok": True}
    assert len(local_arm.calls) == 2
    assert local_arm.clock.waits == [2]


@pytest.mark.parametrize(
    "error",
    [
        http_error(),
        URLError("Temporary connection failure"),
        ConnectionResetError("Connection reset"),
        TimeoutError("Timed out"),
        RemoteDisconnected("Remote end closed connection"),
    ],
)
def test_post_is_not_retried_by_default(local_arm, capsys, error):
    local_arm.outcomes.append(error)

    with pytest.raises(AzureError):
        local_arm.client.request("POST", PATH)

    assert len(local_arm.calls) == 1
    assert local_arm.clock.waits == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 501])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_non_transient_statuses_are_not_retried(local_arm, status, method):
    local_arm.outcomes.append(http_error(status))

    with pytest.raises(AzureError) as caught:
        local_arm.client.request(method, PATH, idempotent=True)

    assert caught.value.status == status
    assert len(local_arm.calls) == 1
    assert local_arm.clock.waits == []


def test_exhaustion_preserves_short_arm_error_and_query_free_logs(local_arm, capsys):
    body = {"error": {"code": "ServiceUnavailable", "message": "Try later " + "x" * 1000}}
    errors = [http_error(body=body) for _ in range(6)]
    local_arm.outcomes.extend(errors)

    with pytest.raises(AzureError) as caught:
        local_arm.client.request("GET", PATH + "?private-query=not-for-logs")

    assert caught.value.status == 503
    assert str(caught.value).startswith("Azure HTTP 503: ServiceUnavailable: Try later ")
    assert len(str(caught.value)) <= len("Azure HTTP 503: ") + 200
    assert len(local_arm.calls) == 6
    assert local_arm.clock.waits == [2, 4, 8, 16, 32]
    assert all(error.fp.closed for error in errors)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 5
    assert all(f"GET {PATH}: HTTP 503;" in line for line in lines)
    assert all("private-query" not in line and "not-for-logs" not in line for line in lines)


def test_html_error_uses_only_title(local_arm):
    body = (
        b"<html><head><TITLE>\nService unavailable &amp; retry later\n</TITLE></head>"
        b"<body>" + b"private-page-content " * 1000 + b"</body></html>"
    )
    local_arm.outcomes.append(http_error(body=body))

    with pytest.raises(AzureError) as caught:
        local_arm.client.request("POST", PATH)

    assert caught.value.status == 503
    assert str(caught.value) == "Azure HTTP 503: Service unavailable & retry later"


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"\n\nFirst readable line\nDo not include this", "First readable line"),
        (b"<html>" + b"x" * 1000, "<html>" + "x" * 194),
        (b"<title>" + b"x" * 1000 + b"</title>", "x" * 200),
        (b"\xff non-UTF8 error", "\ufffd non-UTF8 error"),
        (b"", "Empty response"),
        (
            {"error": {"code": "BadRequest", "message": "Check\n  the setting"}},
            "BadRequest: Check the setting",
        ),
        ({"code": "BadRequest", "message": "Check the setting"}, "BadRequest: Check the setting"),
    ],
)
def test_error_details_stay_short(local_arm, body, detail):
    local_arm.outcomes.append(http_error(400, body=body))

    with pytest.raises(AzureError) as caught:
        local_arm.client.request("GET", PATH)

    assert str(caught.value) == f"Azure HTTP 400: {detail}"
    assert caught.value.status == 400


@pytest.mark.parametrize(
    ("duration", "timeouts", "waits"),
    [
        (90, [90, 90, 90, 16], [2, 4, 8]),
        (42, [90, 90, 90, 90, 90, 28], [2, 4, 8, 16, 32]),
    ],
)
def test_request_time_and_final_timeout_respect_deadline(local_arm, duration, timeouts, waits):
    def timed_out(timeout):
        local_arm.clock.elapsed += min(duration, timeout)
        return URLError(TimeoutError("Timed out"))

    local_arm.outcomes.extend([timed_out] * 6)

    with pytest.raises(AzureError):
        local_arm.client.request("GET", PATH)

    assert [timeout for _, timeout, _ in local_arm.calls] == timeouts
    assert local_arm.clock.waits == waits
    assert local_arm.clock.elapsed == 300
    assert all(0 < timeout <= min(90, 300 - started) for _, timeout, started in local_arm.calls)


def test_token_acquisition_also_consumes_request_deadline(local_arm):
    def token():
        local_arm.clock.elapsed += 180
        return "not-a-live-token"

    def timed_out(timeout):
        local_arm.clock.elapsed += timeout
        return TimeoutError("Timed out")

    local_arm.client.token = token
    local_arm.outcomes.extend([timed_out] * 6)

    with pytest.raises(AzureError):
        local_arm.client.request("GET", PATH)

    assert [timeout for _, timeout, _ in local_arm.calls] == [90, 28]
    assert local_arm.clock.waits == [2]
    assert local_arm.clock.elapsed == 300
    assert all(timeout <= 300 - started for _, timeout, started in local_arm.calls)


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": "61"},
        {"x-ms-retry-after-ms": "61000"},
        {"Retry-After": "4", "x-ms-retry-after-ms": "301000"},
        {"Retry-After": "Thu, 01 Jan 2026 00:05:01 GMT"},
    ],
)
def test_server_delay_above_backoff_bound_fails_instead_of_retrying_early(local_arm, headers):
    local_arm.outcomes.append(http_error(429, headers=headers))

    with pytest.raises(AzureError) as caught:
        local_arm.client.request("GET", PATH)

    assert caught.value.status == 429
    assert len(local_arm.calls) == 1
    assert local_arm.clock.waits == []


@pytest.mark.parametrize(
    ("duration", "hint", "attempts", "elapsed", "waits"),
    [(42, 40, 4, 288, [40, 40, 40]), (90, 60, 2, 240, [60])],
)
@pytest.mark.parametrize("header_kind", ["seconds", "milliseconds", "date"])
def test_server_delay_must_fit_remaining_deadline(
    local_arm, duration, hint, attempts, elapsed, waits, header_kind
):
    def unavailable(timeout):
        local_arm.clock.elapsed += duration
        if header_kind == "milliseconds":
            headers = {"x-ms-retry-after-ms": str(hint * 1000)}
        elif header_kind == "date":
            headers = {
                "Retry-After": format_datetime(
                    datetime.fromtimestamp(local_arm.clock.time() + hint, UTC), usegmt=True
                )
            }
        else:
            headers = {"Retry-After": str(hint)}
        return http_error(503, headers=headers)

    local_arm.outcomes.extend([unavailable] * 6)

    with pytest.raises(AzureError):
        local_arm.client.request("GET", PATH)

    assert len(local_arm.calls) == attempts
    assert local_arm.clock.elapsed == elapsed
    assert local_arm.clock.waits == waits
    assert all(
        started - previous[2] - duration >= hint
        for previous, (_, _, started) in zip(local_arm.calls, local_arm.calls[1:], strict=False)
    )


def test_jittered_backoff_is_exponential_and_capped(local_arm):
    bounds = []

    def maximum_jitter(low, high):
        bounds.append((low, high))
        return high

    local_arm.client._jitter = maximum_jitter
    local_arm.outcomes.extend(http_error() for _ in range(6))

    with pytest.raises(AzureError):
        local_arm.client.request("GET", PATH)

    assert bounds == [(2, 4), (4, 8), (8, 16), (16, 32), (32, 60)]
    assert local_arm.clock.waits == [4, 8, 16, 32, 60]
    assert all(2 <= wait <= 60 for wait in local_arm.clock.waits)
    assert local_arm.clock.elapsed == 120


def test_no_further_attempt_if_sleep_exhausts_deadline(local_arm):
    def oversleep(seconds):
        local_arm.clock.elapsed += 300

    local_arm.client._sleep = oversleep
    local_arm.outcomes.append(http_error())

    with pytest.raises(AzureError) as caught:
        local_arm.client.request("GET", PATH)

    assert caught.value.status == 503
    assert len(local_arm.calls) == 1


@pytest.mark.parametrize(
    ("body", "result"), [(b"", None), (b"\xef\xbb\xbf{}", {}), (b"plain text", "plain text")]
)
def test_success_response_decoding_is_unchanged(local_arm, body, result):
    local_arm.outcomes.append(body)

    assert local_arm.client.request("GET", PATH) == result
    assert len(local_arm.calls) == 1
    assert local_arm.clock.waits == []


def test_existing_positional_body_and_version_are_preserved_on_retry(local_arm):
    local_arm.outcomes.extend([http_error(), b"{}"])
    body = {"properties": {"value": "example"}}

    assert local_arm.client.request("PUT", PATH, body, "2024-04-01") == {}

    requests = [request for request, _, _ in local_arm.calls]
    assert requests[0] is requests[1]
    assert requests[0].method == "PUT"
    assert requests[0].data == json.dumps(body).encode()
    assert requests[0].full_url.endswith(PATH + "?api-version=2024-04-01")


@pytest.mark.parametrize("method", ["OPTIONS", "TRACE"])
def test_other_methods_require_explicit_retry_opt_in(local_arm, method):
    local_arm.outcomes.append(http_error())

    with pytest.raises(AzureError):
        local_arm.client.request(method, PATH)

    assert len(local_arm.calls) == 1
    assert local_arm.clock.waits == []
