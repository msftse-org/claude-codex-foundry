"""Small billable Responses probes; reports exclude prompts and bearer tokens."""

import io
import json
import time
from urllib.error import HTTPError
from urllib.request import Request

from .auth import credential
from .verify import open_url


def _apim_not_found(body, headers):
    headers = {name.lower(): value for name, value in (headers or {}).items()}
    if headers.keys() & {
        "x-codex-gateway",
        "x-claude-gateway",
        "x-budget-request-id",
        "x-ms-request-id",
        "x-request-id",
    } or any(name.startswith(("x-backend-", "openai-")) for name in headers):
        return False
    if "server" in headers and headers["server"].lower() != "microsoft-azure-api-management":
        return False

    def unique_object(pairs):
        result = dict(pairs)
        return result if len(result) == len(pairs) else None

    try:
        detail = json.loads(body, object_pairs_hook=unique_object)
    except ValueError:
        return False
    return (
        detail == {"statusCode": 404, "message": "Resource not found"}
        and type(detail["statusCode"]) is int
    )


def _propagation_opener(open_request, route, *, route_wait, access_wait, clock, sleep):
    first_request = True

    def open_first(request, timeout):
        nonlocal first_request
        eligible = first_request
        first_request = False
        if (
            not eligible
            or not (route_wait or access_wait)
            or request.full_url != route
            or request.get_method() != "POST"
        ):
            return open_request(request, timeout)

        started = clock()
        deadline = None
        delay = 2
        announced = False
        initial_budget = min(wait for wait in (route_wait, access_wait) if wait)
        attempt_timeout = min(timeout, initial_budget)
        while True:
            try:
                return open_request(request, attempt_timeout)
            except HTTPError as error:
                if error.code == 404 and route_wait:
                    with error:
                        body = error.read()
                    if not _apim_not_found(body, error.headers):
                        raise HTTPError(
                            error.url, error.code, error.reason, error.headers, io.BytesIO(body)
                        ) from None
                    wait, kind = route_wait, "API Management route"
                    recovery = "check the route and rerun the command"
                elif error.code in {401, 403} and access_wait:
                    error.close()
                    wait, kind = access_wait, "access"
                    recovery = (
                        "check group membership and Foundry permissions, then rerun the command"
                    )
                else:
                    raise
                status = error.code
            # Include HTTP time, and never extend the window if the rejection changes.
            deadline = min(deadline, started + wait) if deadline is not None else started + wait
            budget = deadline - started
            expired = (
                f"Codex {kind} still returns HTTP {status} after {budget:g} seconds; {recovery}."
            )
            remaining = deadline - clock()
            if remaining <= 0:
                raise RuntimeError(expired)
            if not announced:
                print(
                    f"Waiting for Codex {kind} propagation (up to {budget:g} seconds; first request only).",
                    flush=True,
                )
                announced = True
            sleep(min(delay, remaining))
            delay = min(delay * 2, 15)
            remaining = deadline - clock()
            if remaining <= 0:
                raise RuntimeError(expired)
            attempt_timeout = min(timeout, remaining)

    return open_first


def response(c, payload, token=None, timeout=180, *, opener=None):
    req = Request(
        c["gateway_url"] + "/responses",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + (token or credential(c)),
            "Content-Type": "application/json",
        },
    )
    try:
        with (opener or open_url)(req, timeout) as r:
            if payload.get("stream"):
                events = []
                for line in r:
                    if line.startswith(b"data: ") and line.strip() != b"data: [DONE]":
                        events.append(json.loads(line[6:]))
                completed = [e["response"] for e in events if e.get("type") == "response.completed"]
                if len(completed) != 1:
                    failures = [
                        e.get("type")
                        for e in events
                        if e.get("type") in {"error", "response.failed", "response.incomplete"}
                    ]
                    raise RuntimeError("Stream did not complete: " + str(failures))
                return completed[0], {
                    "events": len(events),
                    "gateway": r.headers.get("x-codex-gateway"),
                    "request_id": r.headers.get("apim-request-id"),
                }
            return json.load(r), {
                "gateway": r.headers.get("x-codex-gateway"),
                "request_id": r.headers.get("apim-request-id"),
            }
    except HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Responses HTTP {e.code}: {detail[:1500]}") from None


def output_text(result):
    return "".join(
        p.get("text", "")
        for x in result.get("output", [])
        for p in x.get("content", [])
        if p.get("type") == "output_text"
    )


def qualify(
    c,
    model,
    token=None,
    *,
    route_propagation_wait=0,
    access_propagation_wait=0,
    opener=None,
    clock=None,
    sleep=None,
):
    for name, wait in (
        ("route_propagation_wait", route_propagation_wait),
        ("access_propagation_wait", access_propagation_wait),
    ):
        if type(wait) is not int or wait < 0:
            raise ValueError(f"{name} must be a non-negative whole number of seconds")
    clock = clock or time.monotonic
    started = clock()
    result = {"name": model["name"], "model": model["model"], "passed": False}
    common = {
        "model": model["name"],
        "store": False,
        "max_output_tokens": 2048,
        "include": ["reasoning.encrypted_content"],
    }
    # Pro models may require high reasoning and cannot accept low effort.
    if (
        model["model"].startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))
        and "-pro" not in model["model"]
    ):
        common["reasoning"] = {"effort": "low"}
    try:
        open_request = _propagation_opener(
            opener or open_url,
            c["gateway_url"] + "/responses",
            route_wait=route_propagation_wait,
            access_wait=access_propagation_wait,
            clock=clock,
            sleep=sleep or time.sleep,
        )
        first, trace = response(
            c,
            {**common, "input": "Reply exactly CODEX_AZURE_OK.", "stream": True},
            token,
            opener=open_request,
        )
        if "CODEX_AZURE_OK" not in output_text(first):
            raise RuntimeError("Unexpected streaming probe response")
        tools = [
            {
                "type": "function",
                "name": "read_marker",
                "description": "Read a harmless test marker.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                "strict": True,
            }
        ]
        messages = [
            {
                "role": "user",
                "content": "Call read_marker, then return its value exactly. Do not invent the marker.",
            }
        ]
        called, _ = response(
            c,
            {
                **common,
                "input": messages,
                "tools": tools,
                "tool_choice": {"type": "function", "name": "read_marker"},
            },
            token,
            opener=open_request,
        )
        calls = [x for x in called.get("output", []) if x.get("type") == "function_call"]
        if len(calls) != 1 or calls[0]["name"] != "read_marker":
            raise RuntimeError("Expected one read_marker tool call")
        followup = [
            *messages,
            *called["output"],
            {
                "type": "function_call_output",
                "call_id": calls[0]["call_id"],
                "output": "RESUMED_AZURE_OK",
            },
        ]
        final, _ = response(
            c,
            {**common, "input": followup, "tools": tools, "tool_choice": "none", "stream": True},
            token,
            opener=open_request,
        )
        if "RESUMED_AZURE_OK" not in output_text(final):
            raise RuntimeError("Tool result was not preserved on follow-up")
        result.update(
            passed=True,
            streaming=True,
            tool_loop=True,
            replayed_followup=True,
            gateway=trace["gateway"],
            usage=final.get("usage"),
        )
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        result["error"] = str(e)
    result["seconds"] = round(clock() - started, 2)
    return result
