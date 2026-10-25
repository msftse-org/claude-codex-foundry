import copy
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

TENANT = "00000000-0000-0000-0000-000000000001"
COGNITIVE = "00000000-0000-0000-0000-000000000002"
INFERENCE_SCOPE = "00000000-0000-0000-0000-000000000003"


@pytest.fixture
def registration():
    path = Path(__file__).resolve().parents[1] / "scripts/register_enrollment.py"
    spec = importlib.util.spec_from_file_location("register_enrollment", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Directory:
    def __init__(self):
        self.apps = []
        self.services = [
            {
                "appId": COGNITIVE,
                "servicePrincipalNames": ["https://cognitiveservices.azure.com"],
                "oauth2PermissionScopes": [
                    {"id": INFERENCE_SCOPE, "value": "user_impersonation", "isEnabled": True}
                ],
            }
        ]
        self.calls = []
        self.lose_create_response = False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and "/federatedIdentityCredentials" in path:
            return {"value": []}
        if method == "GET":
            expression = parse_qs(urlsplit(path).query)["$filter"][0]
            if "servicePrincipalNames" in expression:
                return {"value": copy.deepcopy(self.services[:1])}
            field, _, value = expression.split(" ", 2)
            collection = self.apps if path.startswith("applications?") else self.services
            return {
                "value": copy.deepcopy([a for a in collection if a.get(field) == value.strip("'")])
            }
        if method == "POST":
            created = {**copy.deepcopy(body), "id": str(uuid4()), "accountEnabled": True}
            if path == "applications":
                created["appId"] = str(uuid4())
                self.apps.append(created)
                if self.lose_create_response:
                    self.lose_create_response = False
                    raise RuntimeError("Transport response lost")
            else:
                self.services.append(created)
            return copy.deepcopy(created)
        if method == "PATCH":
            app = next(a for a in self.apps if a["id"] == path.split("/")[1])
            app.update(copy.deepcopy(body))
            return None
        raise AssertionError((method, path, body))

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def test_preview_has_no_local_or_remote_writes(registration, tmp_path):
    graph = Directory()
    output = tmp_path / "receipt.json"
    result = registration.prepare(graph, TENANT, "Test", output)
    assert result == {"mode": "preview", "existing": {"api": False, "client": False}}
    assert not output.exists()
    assert not graph.writes


def test_creation_has_only_two_delegated_permissions_no_secrets(registration, tmp_path):
    graph = Directory()
    output = tmp_path / "receipt.json"
    result = registration.prepare(graph, TENANT, "Test", output, apply=True)
    api, client = graph.apps
    assert result["configuration_resource_id"] == api["appId"]
    assert result["public_client_id"] == client["appId"]
    assert api["identifierUris"] == ["api://" + api["appId"]]
    assert api["api"]["oauth2PermissionScopes"][0]["type"] == "Admin"
    assert api["requiredResourceAccess"] == []
    assert client["publicClient"]["redirectUris"] == ["http://localhost"]
    assert client["isFallbackPublicClient"] is False
    assert len(client["requiredResourceAccess"]) == 2
    assert client["requiredResourceAccess"][1] == {
        "resourceAppId": COGNITIVE,
        "resourceAccess": [{"id": INFERENCE_SCOPE, "type": "Scope"}],
    }
    assert all(
        p["type"] == "Scope"
        for resource in client["requiredResourceAccess"]
        for p in resource["resourceAccess"]
    )
    assert output.stat().st_mode & 0o777 == 0o600
    receipt = json.loads(output.read_text())
    assert receipt["tenant_id"] == TENANT
    assert "client_secret" not in output.read_text()
    graph.calls.clear()
    assert registration.prepare(graph, TENANT, "Test", output, apply=True) == result
    assert not graph.writes


def test_lost_create_response_resumes_with_same_receipt(registration, tmp_path):
    graph = Directory()
    output = tmp_path / "receipt.json"
    graph.lose_create_response = True
    with pytest.raises(RuntimeError, match="Transport"):
        registration.prepare(graph, TENANT, "Test", output, apply=True)
    assert len(graph.apps) == 1
    assert output.exists()
    registration.prepare(graph, TENANT, "Test", output, apply=True)
    assert len(graph.apps) == 2


def test_does_not_adopt_named_application_without_receipt(registration, tmp_path):
    graph = Directory()
    registration.prepare(graph, TENANT, "Test", tmp_path / "first.json", apply=True)
    graph.calls.clear()
    with pytest.raises(ValueError, match="already exists"):
        registration.prepare(graph, TENANT, "Test", tmp_path / "other.json", apply=True)
    assert not graph.writes
    assert not (tmp_path / "other.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("signInAudience", "AzureADMultipleOrgs"),
        ("isFallbackPublicClient", True),
        ("publicClient", {"redirectUris": ["https://untrusted.example"]}),
        ("web", {"redirectUris": [], "implicitGrantSettings": {"enableIdTokenIssuance": True}}),
        ("requiredResourceAccess", [{"resourceAppId": COGNITIVE, "resourceAccess": []}]),
        ("passwordCredentials", [{"keyId": str(uuid4())}]),
        ("keyCredentials", [{"keyId": str(uuid4())}]),
        ("appRoles", [{"id": str(uuid4())}]),
        ("identifierUris", ["api://untrusted"]),
        ("api", {"requestedAccessTokenVersion": 1}),
    ],
)
def test_manifest_drift_causes_no_write(registration, tmp_path, field, value):
    graph = Directory()
    output = tmp_path / "receipt.json"
    registration.prepare(graph, TENANT, "Test", output, apply=True)
    graph.apps[1][field] = value
    graph.calls.clear()
    with pytest.raises(ValueError):
        registration.prepare(graph, TENANT, "Test", output, apply=True)
    assert not graph.writes


def test_consent_is_explicit_and_only_for_dedicated_client(registration, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(registration, "az_json", lambda *args: calls.append(args))
    graph = Directory()
    output = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="requires --apply"):
        registration.prepare(graph, TENANT, "Test", output, consent=True)
    assert not graph.calls
    result = registration.prepare(graph, TENANT, "Test", output, apply=True, consent=True)
    assert calls == [
        ("ad", "app", "permission", "admin-consent", "--id", result["public_client_id"])
    ]


def test_other_tenant_or_unsafe_receipt_refused(registration, tmp_path):
    graph = Directory()
    output = tmp_path / "receipt.json"
    registration.prepare(graph, TENANT, "Test", output, apply=True)
    graph.calls.clear()
    with pytest.raises(ValueError, match="Receipt"):
        registration.prepare(graph, str(uuid4()), "Test", output, apply=True)
    output.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        registration.prepare(graph, TENANT, "Test", output, apply=True)
    assert not graph.calls


def test_disabled_enterprise_application_not_reenabled(registration, tmp_path):
    graph = Directory()
    output = tmp_path / "receipt.json"
    registration.prepare(graph, TENANT, "Test", output, apply=True)
    graph.services[1]["accountEnabled"] = False
    graph.calls.clear()
    with pytest.raises(ValueError, match="disabled"):
        registration.prepare(graph, TENANT, "Test", output, apply=True)
    assert not graph.writes
