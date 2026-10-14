"""Small live checks against the customer's configured gateway."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .auth import credential


def request(c, path, token=None, data=None):
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(
        c["gateway_url"] + path, data=json.dumps(data).encode() if data else None, headers=headers
    )
    try:
        with urlopen(req, timeout=120) as response:
            return response.status, dict(response.headers), response.read().decode()
    except HTTPError as error:
        return error.code, dict(error.headers), error.read().decode()


def verify(c, all_models=False):
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
    if status != 400:
        raise RuntimeError(f"Unknown model should be rejected at the gateway, got {status}")
    print("PASS model allowlist", flush=True)
