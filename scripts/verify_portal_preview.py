#!/usr/bin/env python3
"""Exercise governance on a preview API and restore its exact original policies."""

import argparse
import copy
import json
import socket
import time
from pathlib import Path
from urllib.error import URLError

from claude_foundry.azure import Azure, api_path, az_json
from claude_foundry.config import load_org, write_json
from claude_foundry.portal.service import publish
from claude_foundry.verify import request as gateway_request


def connection_retry(call, *args, **kwargs):
    """Retry DNS/refusal failures before delivery, never ambiguous HTTP timeouts."""
    for attempt in range(3):
        try:
            return call(*args, **kwargs)
        except URLError as error:
            if (
                not isinstance(error.reason, (ConnectionRefusedError, socket.gaierror))
                or attempt == 2
            ):
                raise
            print("Connection failed before request delivery; retrying.", flush=True)
            time.sleep(2**attempt)


def request(*args, **kwargs):
    return connection_retry(gateway_request, *args, **kwargs)


class PreviewAzure(Azure):
    def request(self, *args, **kwargs):
        return connection_retry(super().request, *args, **kwargs)


def verify(config, state, output, all_models=False):
    if not config["api_id"].endswith("-preview") or not config["api_path"].endswith("-preview"):
        raise ValueError(
            "These tests temporarily change entitlements. A separate preview API is required."
        )
    azure = PreviewAzure(config["subscription_id"])
    token = az_json(
        "account",
        "get-access-token",
        "--subscription",
        config["subscription_id"],
        "--resource",
        "https://cognitiveservices.azure.com",
    )["accessToken"]
    paths = [
        api_path(config) + "/policies/policy",
        api_path(config) + "/operations/models/policies/policy",
    ]
    previous = [azure.request("GET", path)["properties"] for path in paths]
    write_json(output.with_suffix(".policies.json"), previous)
    results = []

    def check(name, status, expected, **extra):
        result = {"check": name, "status": status, "expected": expected, **extra}
        results.append(result)
        write_json(output, results)
        print(f"{'PASS' if status == expected else 'FAIL'} {name}: {status}", flush=True)
        if status != expected:
            raise RuntimeError(f"{name}: expected {expected}, received {status}")

    def wait_status(path, data, expected):
        for _ in range(30):
            result = request(config, path, token, data)
            if result[0] == expected:
                return result
            time.sleep(1)
        return result

    def reset_limits(label, payload):
        # First observe a successful request with all limits disabled. Otherwise an old
        # propagated 403/429 could falsely satisfy a different scope's next assertion.
        unlimited = copy.deepcopy(state)
        unlimited["settings"].update(
            monthly_tokens=0, member_tokens_per_minute=0, member_requests_per_minute=0
        )
        for item in unlimited["groups"] + unlimited["members"]:
            item["monthly_tokens"] = 0
        publish(config, unlimited, azure)
        check(
            f"unlimited before {label}",
            wait_status("/v1/messages", {**payload, "max_tokens": 16}, 200)[0],
            200,
        )

    try:
        publish(config, state, azure)
        for name, auth in [("missing token", None), ("invalid token", "invalid")]:
            check(name, request(config, "/v1/models", auth)[0], 401)
        check("catalog", wait_status("/v1/models", None, 200)[0], 200)
        for model in config["models"] if all_models else config["models"][:1]:
            payload = {
                "model": model["name"],
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply OK."}],
            }
            check(model["name"], request(config, "/v1/messages", token, payload)[0], 200)
        payload = {
            "model": config["models"][0]["name"],
            "messages": [{"role": "user", "content": "Reply OK."}],
        }
        check(
            "token counting", request(config, "/v1/messages/count_tokens", token, payload)[0], 200
        )
        status, _, body = request(
            config, "/v1/messages", token, {**payload, "max_tokens": 16, "stream": True}
        )
        check("streaming", status, 200, completed="event: message_stop" in body)
        if "event: message_stop" not in body:
            raise RuntimeError("Stream did not complete")

        restricted = copy.deepcopy(state)
        for group in restricted["groups"]:
            group.update(model_ids=[], default_model="")
        for member in restricted["members"]:
            member["extra_models"] = []
        publish(config, restricted, azure)
        check("denied model", wait_status("/v1/messages/count_tokens", payload, 403)[0], 403)
        _, _, body = request(config, "/v1/models", token)
        if json.loads(body)["data"]:
            raise RuntimeError("Denied models leaked through the catalog")

        suspended = copy.deepcopy(state)
        for member in suspended["members"]:
            member["status"] = "suspended"
        publish(config, suspended, azure)
        check("suspended caller", wait_status("/v1/models", None, 403)[0], 403)

        limited = copy.deepcopy(state)
        limited["settings"]["monthly_tokens"] = 1
        publish(config, limited, azure)
        check("quota test caller authorized", wait_status("/v1/models", None, 200)[0], 200)
        status, _, body = wait_status("/v1/messages", {**payload, "max_tokens": 16}, 403)
        check(
            "organization token quota",
            status,
            403,
        )
        if "token" not in body.lower() or "quota" not in body.lower():
            raise RuntimeError("Expected a token-quota error, not another kind of denial")
        for scope in ["group", "member"]:
            reset_limits(scope + " quota", payload)
            limited = copy.deepcopy(state)
            limited["settings"].update(
                monthly_tokens=0, member_tokens_per_minute=0, member_requests_per_minute=0
            )
            for group in limited["groups"]:
                group["monthly_tokens"] = 1 if scope == "group" else 0
            for member in limited["members"]:
                member["monthly_tokens"] = 1 if scope == "member" else 0
            publish(config, limited, azure)
            status, _, body = wait_status("/v1/messages", {**payload, "max_tokens": 16}, 403)
            check(f"{scope} token quota", status, 403)
            if "quota" not in body.lower():
                raise RuntimeError("Expected a token-quota error")
        for setting, label, route, data in [
            (
                "member_tokens_per_minute",
                "member token rate",
                "/v1/messages",
                {**payload, "max_tokens": 16},
            ),
            (
                "member_requests_per_minute",
                "member request rate",
                "/v1/messages/count_tokens",
                payload,
            ),
        ]:
            reset_limits(label, payload)
            limited = copy.deepcopy(state)
            limited["settings"].update(
                monthly_tokens=0, member_tokens_per_minute=0, member_requests_per_minute=0
            )
            for group in limited["groups"]:
                group["monthly_tokens"] = 0
            for member in limited["members"]:
                member["monthly_tokens"] = 0
            limited["settings"][setting] = 1
            publish(config, limited, azure)
            check(label, wait_status(route, data, 429)[0], 429)
    finally:
        failures = []
        for path, policy in zip(paths, previous, strict=True):
            try:
                azure.request(
                    "PUT",
                    path,
                    {
                        "properties": {
                            "format": policy.get("format", "rawxml"),
                            "value": policy["value"],
                        }
                    },
                )
            except Exception as error:
                failures.append(error)
        if failures:
            raise RuntimeError(
                "Preview policy restore incomplete; operator recovery required"
            ) from failures[0]
        print("Original preview policies restored.", flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".local/portal/governance-checks.json"))
    parser.add_argument("--all-models", action="store_true")
    args = parser.parse_args()
    verify(
        load_org(args.organization),
        json.loads(args.state.read_text()),
        args.output,
        args.all_models,
    )


if __name__ == "__main__":
    main()
