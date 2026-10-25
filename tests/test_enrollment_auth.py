import base64
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from claude_foundry import enrollment_auth as auth
from claude_foundry.config import TOKEN_RESOURCE
from claude_foundry.configuration_client import EnrollmentDescriptor, SignInRequired

TENANT = "00000000-0000-0000-0000-000000000001"
SUBJECT = "00000000-0000-0000-0000-000000000002"
RESOURCE = "00000000-0000-0000-0000-000000000003"
OTHER = "00000000-0000-0000-0000-000000000004"


def descriptor():
    return EnrollmentDescriptor.parse(
        {
            "schema_version": 1,
            "organization_id": OTHER,
            "tenant_id": TENANT,
            "configuration_resource_id": RESOURCE,
            "configuration_url": "https://portal.example.com/api/v1/me/client-configuration",
            "routes": {
                "claude": {
                    "route_id": "claude-governed",
                    "gateway_url": "https://gateway.example.com/claude",
                }
            },
        }
    )


def claims(**overrides):
    return {
        "tid": TENANT,
        "oid": SUBJECT,
        "aud": RESOURCE,
        "azp": auth.AZURE_CLI_CLIENT_ID,
        "scp": "configuration.read",
        "exp": 2000000000,
        **overrides,
    }


def token(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    # Not a signature-valid token: claims inspection is only a consistency guard.
    return f"header.{encoded}.signature"


def cli(monkeypatch, *, payload=None, response=None, error=None):
    calls = []
    monkeypatch.setattr(auth, "find_az", lambda: "/trusted/bin/az")
    monkeypatch.setattr(auth.time, "time", lambda: 1900000000)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if error:
            raise error
        return response or SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"accessToken": token(payload or claims())}),
            stderr="",
        )

    monkeypatch.setattr(auth.subprocess, "run", run)
    return calls


def test_scopes_are_separate_and_normal_az_account_is_never_used(monkeypatch, tmp_path):
    monkeypatch.setenv("AZURE_CONFIG_DIR", "/unrelated/normal-azure-cache")
    directory = tmp_path / "enrollment/azure"
    provider = auth.EnrolledAzureCli(descriptor(), directory, subject_id=SUBJECT)
    calls = cli(monkeypatch)
    assert provider.configuration_token() == token(claims())
    command, options = calls[0]
    assert command == [
        "/trusted/bin/az",
        "account",
        "get-access-token",
        "--tenant",
        TENANT,
        "--scope",
        f"api://{RESOURCE}/configuration.read",
        "--only-show-errors",
        "--output",
        "json",
    ]
    assert options["capture_output"] is True and options["timeout"] == 30
    assert options["env"]["AZURE_CONFIG_DIR"] == str(directory)
    assert os.environ["AZURE_CONFIG_DIR"] == "/unrelated/normal-azure-cache"
    assert directory.stat().st_mode & 0o777 == 0o700
    payload = claims(aud=TOKEN_RESOURCE, scp="user_impersonation")
    calls = cli(monkeypatch, payload=payload)
    assert provider.inference_token() == token(payload)
    assert calls[0][0][6] == TOKEN_RESOURCE + "/.default"
    assert not list(directory.iterdir())  # The wrapper never writes a token itself.


def test_login_is_explicit_and_uses_the_same_isolated_cache(monkeypatch, tmp_path):
    provider = auth.EnrolledAzureCli(descriptor(), tmp_path / "azure")
    calls = cli(monkeypatch)
    provider.login()
    login, read = calls
    assert login[0][1] == "login"
    assert "--scope" in login[0] and descriptor().scope in login[0]
    assert login[0][-1] == "none" and login[1]["timeout"] == 360
    assert login[1]["env"]["AZURE_CONFIG_DIR"] == read[1]["env"]["AZURE_CONFIG_DIR"]
    assert read[0][1:3] == ["account", "get-access-token"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tid", OTHER),
        ("oid", OTHER),
        ("oid", None),
        ("oid", "email@example.com"),
        ("aud", "https://management.azure.com"),
        ("aud", [RESOURCE]),
        ("azp", OTHER),
        ("idtyp", "app"),
        ("scp", ""),
        ("scp", ["configuration.read"]),
        ("scp", "configuration.reader"),
        ("exp", 1),
        ("exp", True),
        ("exp", "2000000000"),
        ("exp", float("nan")),
        ("exp", float("inf")),
    ],
)
def test_account_audience_client_scope_and_expiry_guard(monkeypatch, tmp_path, key, value):
    provider = auth.EnrolledAzureCli(descriptor(), tmp_path / "azure", subject_id=SUBJECT)
    calls = cli(monkeypatch, payload=claims(**{key: value}))
    with pytest.raises(SignInRequired):
        provider.configuration_token()
    assert len(calls) == 1 and calls[0][0][1] != "login"


def test_initial_identity_is_not_a_local_authorization_grant(monkeypatch, tmp_path):
    provider = auth.EnrolledAzureCli(descriptor(), tmp_path / "azure")
    calls = cli(monkeypatch, payload=claims(oid=OTHER))
    assert provider.configuration_token()  # API must verify and return the subject.
    with pytest.raises(SignInRequired, match="Complete organization enrollment"):
        provider.inference_token()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(returncode=1, stdout="secret-token", stderr="secret-account"),
        SimpleNamespace(returncode=0, stdout="not json secret", stderr=""),
        SimpleNamespace(returncode=0, stdout='{"accessToken":"secret"}', stderr=""),
        SimpleNamespace(returncode=0, stdout='{"accessToken":null}', stderr=""),
        SimpleNamespace(returncode=0, stdout="[]", stderr=""),
        SimpleNamespace(returncode=0, stdout=" " * 32769, stderr=""),
    ],
)
def test_cli_failure_never_echoes_credentials_or_diagnostics(monkeypatch, tmp_path, response):
    provider = auth.EnrolledAzureCli(descriptor(), tmp_path / "azure", subject_id=SUBJECT)
    calls = cli(monkeypatch, response=response)
    with pytest.raises(SignInRequired) as error:
        provider.configuration_token()
    assert "secret" not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize("error", [OSError("secret"), subprocess.TimeoutExpired("secret", 30)])
def test_process_failures_are_noninteractive_and_safe(monkeypatch, tmp_path, error):
    provider = auth.EnrolledAzureCli(descriptor(), tmp_path / "azure", subject_id=SUBJECT)
    calls = cli(monkeypatch, error=error)
    with pytest.raises(SignInRequired) as caught:
        provider.configuration_token()
    assert "secret" not in str(caught.value) and len(calls) == 1


def test_credential_cache_permissions_are_not_silently_repaired(tmp_path):
    directory = tmp_path / "azure"
    directory.mkdir(mode=0o755)
    with pytest.raises(SignInRequired, match="0700"):
        auth.EnrolledAzureCli(descriptor(), directory, subject_id=SUBJECT)
    assert directory.stat().st_mode & 0o777 == 0o755


def test_symlinked_cache_or_parent_is_not_followed(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    for directory in [link, link / "azure"]:
        with pytest.raises(SignInRequired, match="symlinks"):
            auth.EnrolledAzureCli(descriptor(), directory, subject_id=SUBJECT)
    assert not list(target.iterdir())


@pytest.mark.parametrize("directory", ["relative/azure", "/absolute/../azure"])
def test_ambiguous_cache_path_rejected(directory):
    with pytest.raises(SignInRequired, match="absolute"):
        auth.EnrolledAzureCli(descriptor(), directory, subject_id=SUBJECT)


def test_inference_token_from_another_account_is_not_emitted(monkeypatch, tmp_path, capsys):
    provider = auth.EnrolledAzureCli(descriptor(), tmp_path / "azure", subject_id=SUBJECT)
    cli(
        monkeypatch,
        payload=claims(oid=OTHER, aud=TOKEN_RESOURCE, scp="user_impersonation"),
    )
    with pytest.raises(SignInRequired):
        provider.inference_token()
    assert capsys.readouterr().out == ""
