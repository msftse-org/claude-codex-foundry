#!/usr/bin/env python3
"""Exercise governance on a preview API and restore its exact original policies."""

import argparse
import copy
import json
import time
from pathlib import Path

from claude_foundry.azure import Azure, api_path, az_json
from claude_foundry.config import load_org, write_json
from claude_foundry.portal.service import publish
from claude_foundry.verify import request


def verify(config, state, output, all_models=False):
    if not config["api_id"].endswith("-preview") or not config["api_path"].endswith("-preview"):
        raise ValueError(
            "These tests temporarily change entitlements. A separate preview API is required."
        )
    azure = Azure(config["subscription_id"])
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
    finally:
        for path, policy in zip(paths, previous, strict=True):
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
