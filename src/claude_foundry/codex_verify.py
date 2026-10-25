"""Small billable Responses probes; reports exclude prompts and bearer tokens."""

import json
import time
from urllib.error import HTTPError
from urllib.request import Request

from .auth import credential
from .verify import open_url


def response(c, payload, token=None, timeout=180):
    req = Request(
        c["gateway_url"] + "/responses",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + (token or credential(c)),
            "Content-Type": "application/json",
        },
    )
    try:
        with open_url(req, timeout) as r:
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


def qualify(c, model, token=None):
    started = time.monotonic()
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
        first, trace = response(
            c, {**common, "input": "Reply exactly CODEX_AZURE_OK.", "stream": True}, token
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
    result["seconds"] = round(time.monotonic() - started, 2)
    return result
