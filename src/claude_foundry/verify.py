"""Small live checks against the customer's configured gateway."""

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .auth import credential


def open_url(req, timeout, attempts=3):
    """Open a request, retrying only failures that occur before it is delivered."""
    for attempt in range(attempts):
        try:
            return urlopen(req, timeout=timeout)
        except HTTPError:
            raise
        except URLError as error:
            # A refused connection or failed DNS lookup sent nothing; timeouts are ambiguous.
            if (
                not isinstance(error.reason, (ConnectionRefusedError, socket.gaierror))
                or attempt == attempts - 1
            ):
                raise
            time.sleep(2**attempt)


def request(c, path, token=None, data=None):
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(
        c["gateway_url"] + path, data=json.dumps(data).encode() if data else None, headers=headers
    )
    try:
        with open_url(req, 120) as response:
            return response.status, dict(response.headers), response.read().decode()
    except HTTPError as error:
        return error.code, dict(error.headers), error.read().decode()


def verify(c, all_models=False, *, governed=False):
    for label, token in [("missing token", None), ("invalid token", "not-a-token")]:
        status, _, _ = request(c, "/v1/models", token)
        if status != 401:
            raise RuntimeError(f"Expected 401 for {label}, got {status}")
        print("PASS rejected", label, flush=True)
    token = credential(c)
    status, _, body = request(c, "/v1/models", token)
    if status != 200:
        raise RuntimeError(f"Catalog failed: {status} {body}")
    actual = {m["id"] for m in json.loads(body)["data"]}
    if actual != {m["name"] for m in c["models"]}:
        raise RuntimeError("Gateway catalog and client profile differ; export a fresh profile")
    print("PASS authenticated model catalog", flush=True)
    models = c["models"] if all_models else c["models"][:1]
    for model in models:
        payload = {
            "model": model["name"],
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "Reply exactly APIM_OK. No explanation."}],
        }
        status, headers, body = request(c, "/v1/messages", token, payload)
        if status != 200 or "APIM_OK" not in body or not headers.get("x-claude-gateway"):
            raise RuntimeError(f"{model['name']} failed: {status} {body}")
        print("PASS inference", model["name"], flush=True)
    payload = {
        "model": c["models"][0]["name"],
        "max_tokens": 128,
        "stream": True,
        "messages": [{"role": "user", "content": "Reply exactly STREAM_OK."}],
    }
    status, _, body = request(c, "/v1/messages", token, payload)
    if (
        status != 200
        or "event: message_stop" not in body
        or "event: content_block_delta" not in body
    ):
        raise RuntimeError(f"Streaming failed: {status} {body}")
    print("PASS streaming", flush=True)
    payload.pop("stream")
    payload.pop("max_tokens")
    status, _, body = request(c, "/v1/messages/count_tokens", token, payload)
    if status != 200 or json.loads(body).get("input_tokens", 0) <= 0:
        raise RuntimeError(f"Token counting failed: {status} {body}")
    print("PASS token counting", flush=True)
    payload["model"] = "not-an-allowed-deployment"
    status, _, _ = request(c, "/v1/messages", token, payload)
    expected = 403 if governed else 400
    if status != expected:
        hint = {
            403: " A 403 usually means portal governance is published; rerun with --governed.",
            400: " A 400 means the baseline policy is active; omit --governed or wait for"
            " publication to propagate.",
        }.get(status, "")
        raise RuntimeError(
            f"Unknown model should return {expected} at the gateway, got {status}.{hint}"
        )
    print("PASS model allowlist", flush=True)
