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
