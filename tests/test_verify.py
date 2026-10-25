import json
from unittest.mock import Mock

import pytest

from claude_foundry.cli import main
from claude_foundry.verify import verify


@pytest.mark.parametrize("governed,status", [(False, 400), (True, 403)])
def test_verifier_checks_exact_authorization_mode(org, monkeypatch, governed, status):
    monkeypatch.setattr("claude_foundry.verify.credential", lambda c: "token")
    request = Mock(
        side_effect=[
            (401, {}, ""),
            (401, {}, ""),
            (200, {}, json.dumps({"data": [{"id": org["models"][0]["name"]}]})),
            (200, {"x-claude-gateway": org["api_id"]}, "APIM_OK"),
            (200, {}, "event: content_block_delta\nevent: message_stop"),
            (200, {}, '{"input_tokens": 1}'),
            (status, {}, ""),
        ]
    )
    monkeypatch.setattr("claude_foundry.verify.request", request)
    verify(org, governed=governed)
    request.reset_mock(side_effect=False)
    request.side_effect = [
        (401, {}, ""),
        (401, {}, ""),
        (200, {}, json.dumps({"data": [{"id": org["models"][0]["name"]}]})),
        (200, {"x-claude-gateway": org["api_id"]}, "APIM_OK"),
        (200, {}, "event: content_block_delta\nevent: message_stop"),
        (200, {}, '{"input_tokens": 1}'),
        (400 if governed else 403, {}, ""),
    ]
    with pytest.raises(RuntimeError, match=f"return {status}.*--governed"):
        verify(org, governed=governed)


def test_cli_passes_governance_mode_explicitly(org, monkeypatch):
    monkeypatch.setattr("claude_foundry.cli.auth.installed_config", lambda: org)
    check = Mock()
    monkeypatch.setattr("claude_foundry.cli.verify", check)
    monkeypatch.setattr("sys.argv", ["claude-foundry", "verify", "--governed"])
    assert main() == 0
    check.assert_called_once_with(org, False, governed=True)


def test_failures_before_delivery_are_retried(monkeypatch):
    import socket
    from urllib.error import URLError

    from claude_foundry import verify as module

    attempts = []

    def opener(req, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise URLError(ConnectionRefusedError(61, "Connection refused"))
        if len(attempts) == 2:
            raise URLError(socket.gaierror(8, "nodename nor servname provided"))
        return "response"

    monkeypatch.setattr(module, "urlopen", opener)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    assert module.open_url("request", 120) == "response"
    assert attempts == [120, 120, 120]


@pytest.mark.parametrize("kind", ["timeout", "http", "refused-every-time"])
def test_ambiguous_or_persistent_failures_are_not_hidden(monkeypatch, kind):
    from urllib.error import HTTPError, URLError

    from claude_foundry import verify as module

    errors = {
        "timeout": URLError(TimeoutError("timed out")),
        "http": HTTPError("https://gateway", 502, "Bad Gateway", {}, None),
        "refused-every-time": URLError(ConnectionRefusedError(61, "Connection refused")),
    }
    attempts = []

    def opener(req, timeout):
        attempts.append(1)
        raise errors[kind]

    monkeypatch.setattr(module, "urlopen", opener)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    with pytest.raises(URLError):
        module.open_url("request", 120)
    assert len(attempts) == (3 if kind == "refused-every-time" else 1)
