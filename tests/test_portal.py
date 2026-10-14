"""Authorization, policy compilation, and publication failure boundaries."""

import copy
import json
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from claude_foundry.portal.app import create_app
from claude_foundry.portal.auth import Authenticator
from claude_foundry.portal.domain import Change, apply_change, import_members, seed
from claude_foundry.portal.governance import compile_policy
from claude_foundry.portal.service import publish
from claude_foundry.portal.store import Conflict, LocalStore

OWNER = "00000000-0000-0000-0000-000000000003"
CLIENT = "00000000-0000-0000-0000-000000000004"


@pytest.fixture
def state(org):
    return seed(org, OWNER, "Test owner", "owner@example.com")


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(org, key, oid=OWNER, **overrides):
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{org['tenant_id']}/v2.0",
        "aud": CLIENT,
        "tid": org["tenant_id"],
        "azp": CLIENT,
        "oid": oid,
        "scp": "access_as_user",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        **overrides,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


@pytest.fixture
def app(org, state, key, monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_OWNER_ID", OWNER)
    monkeypatch.setenv("PORTAL_CLIENT_ID", CLIENT)
    monkeypatch.delenv("PORTAL_INSIGHTS_APP_ID", raising=False)
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, value: SimpleNamespace(key=key.public_key()),
    )
    store = LocalStore(tmp_path / "state.json", state)
    return create_app(org, store=store, credential=object(), local=False)


def headers(org, key, oid=OWNER):
    return {"Authorization": "Bearer " + token(org, key, oid)}


def add_member(state, role="developer", **kwargs):
    member = {**state["members"][0], "id": str(uuid4()), "role": role, **kwargs}
    state["members"].append(member)
    return member


def change(state, kind, value, actor=None):
    return apply_change(
        copy.deepcopy(state),
        Change(revision=state["revision"], kind=kind, value=value),
        actor or state["members"][0],
        ["claude-sonnet-4-6"],
    )


@pytest.mark.parametrize("role", ["developer", "auditor", "billing"])
def test_read_only_roles_cannot_grant_access(state, role):
    member = add_member(state, role)
    with pytest.raises(PermissionError):
        change(state, "member", {**member, "role": "owner"}, member)


def test_admin_cannot_modify_ownership(state):
    admin = add_member(state, "admin")
    with pytest.raises(PermissionError):
        change(state, "member", state["members"][0], admin)
    with pytest.raises(PermissionError):
        change(state, "member", {**admin, "role": "owner"}, admin)


def test_self_lockout_and_unknown_model_rejected(state):
    with pytest.raises(ValueError, match="yourself"):
        change(state, "member", {**state["members"][0], "status": "suspended"})
    with pytest.raises(ValueError, match="deployed"):
        change(
            state,
            "group",
            {**state["groups"][0], "model_ids": ["nonexistent"], "default_model": ""},
        )


def test_billing_cannot_change_token_quota(state):
    billing = add_member(state, "billing")
    updated = change(state, "settings", {**state["settings"], "monthly_budget_usd": 200}, billing)
    assert updated["settings"]["monthly_budget_usd"] == 200
    with pytest.raises(PermissionError):
        change(state, "settings", {**state["settings"], "monthly_tokens": 200}, billing)


def test_request_approval_audits_and_is_not_automatically_published(state):
    member = add_member(state)
    updated = change(
        state,
        "request",
        {"model": "claude-sonnet-4-6", "reason": "Testing agent workflows"},
        member,
    )
    updated = change(
        updated,
        "decision",
        {"id": updated["requests"][0]["id"], "approve": True, "note": "Approved"},
    )
    assert updated["members"][1]["extra_models"] == ["claude-sonnet-4-6"]
    assert updated["requests"][0]["status"] == "approved"
    assert updated["published_revision"] is None
    assert len(updated["audit"]) == 2
    with pytest.raises(ValueError):
        change(
            updated,
            "decision",
            {"id": updated["requests"][0]["id"], "approve": True, "note": "Again"},
        )


def test_store_rejects_stale_save(state, tmp_path):
    store = LocalStore(tmp_path / "state.json", state)
    a, etag = store.read()
    b, stale = store.read()
    a["revision"] += 1
    store.save(a, etag)
    with pytest.raises(Conflict):
        store.save(b, stale)
    assert store.read()[0]["revision"] == 1
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_csv_import_handles_2000_developers_without_privilege_escalation(state, org):
    rows = [f"{uuid4()},Developer {i},developer{i}@example.com" for i in range(2000)]
    value = {
        "csv": "object_id,name,email\n" + "\n".join(rows),
        "group_id": state["groups"][0]["id"],
    }
    updated = change(state, "member_import", value)
    assert len(updated["members"]) == 2001
    assert all(m["role"] == "developer" for m in updated["members"][1:])
    assert len(updated["audit"]) == 1
    assert len(compile_policy(org, updated)[0].encode()) < 250_000
    assert len(state["members"]) == 1
    with pytest.raises(ValueError, match="already exists"):
        change(updated, "member_import", value)
    elevated = {**value, "csv": "object_id,name,email,role\n" + rows[0] + ",owner"}
    with pytest.raises(ValueError, match="Other columns"):
        import_members(state, elevated)


def test_import_validation_rejects_malformed_rows_and_never_saves_partial_data(app, org, key):
    group = app.state.store.read()[0]["groups"][0]["id"]
    csv = f"object_id,name,email\n{uuid4()},Valid,valid@example.com\nnot-an-oid,Invalid,invalid@example.com"
    with TestClient(app) as client:
        body = {"csv": csv, "group_id": group}
        assert (
            client.post("/api/import-preview", headers=headers(org, key), json=body).status_code
            == 422
        )
        assert (
            client.post(
                "/api/changes",
                headers=headers(org, key),
                json={"revision": 0, "kind": "member_import", "value": body},
            ).status_code
            == 422
        )
        assert len(app.state.store.read()[0]["members"]) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"tid": str(uuid4())},
        {"aud": str(uuid4())},
        {"azp": str(uuid4())},
        {"scp": "other_scope"},
        {"exp": 1},
        {"iss": "https://attacker.example"},
        {"oid": "not-an-oid"},
        {"nbf": int(time.time()) + 86400},
    ],
)
def test_access_tokens_reject_wrong_claims(org, app, key, overrides):
    with TestClient(app) as client:
        result = client.get(
            "/api/state", headers={"Authorization": "Bearer " + token(org, key, **overrides)}
        )
        assert result.status_code == 401


def test_token_signature_is_verified(org, key, monkeypatch):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = Authenticator(org["tenant_id"], CLIENT)
    monkeypatch.setattr(
        auth.keys, "get_signing_key_from_jwt", lambda t: SimpleNamespace(key=key.public_key())
    )
    with pytest.raises(HTTPException) as error:
        auth.identity("Bearer " + token(org, other_key))
    assert error.value.status_code == 401


def test_no_header_identity_bypass(app, org, key):
    with TestClient(app) as client:
        assert (
            client.get("/api/state", headers={"X-MS-CLIENT-PRINCIPAL-ID": OWNER}).status_code == 401
        )
        assert client.get("/api/state", headers=headers(org, key, str(uuid4()))).status_code == 403
        response = client.get("/api/state", headers=headers(org, key))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.json()["viewer"]["id"] == OWNER


def test_developer_sees_only_own_state_and_cannot_publish(app, org, key):
    data, etag = app.state.store.read()
    member = add_member(data)
    other = add_member(data)
    data["audit"] = [{"secret": "administrative change"}]
    data["requests"] = [{"member_id": member["id"]}, {"member_id": other["id"]}]
    app.state.store.save(data, etag)
    with TestClient(app) as client:
        auth = headers(org, key, member["id"])
        response = client.get("/api/state", headers=auth).json()
        assert [m["id"] for m in response["members"]] == [member["id"]]
        assert response["audit"] == []
        assert response["requests"] == [{"member_id": member["id"]}]
        assert set(response["settings"]) == {"name"}
        for route in ["/api/telemetry", "/api/impact"]:
            assert client.get(route, headers=auth).status_code == 403
        assert client.post("/api/publish", headers=auth, json={}).status_code == 403


def test_api_revision_conflicts_and_suspend_revoke_portal_access(app, org, key):
    with TestClient(app) as client:
        value = app.state.store.read()[0]["settings"]
        response = client.post(
            "/api/changes",
            headers=headers(org, key),
            json={"revision": 99, "kind": "settings", "value": value},
        )
        assert response.status_code == 409
        data, etag = app.state.store.read()
        member = add_member(data, status="suspended")
        app.state.store.save(data, etag)
        assert client.get("/api/state", headers=headers(org, key, member["id"])).status_code == 403


def test_local_mode_rejects_cloud_and_dns_rebinding(org, state, monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_OWNER_ID", OWNER)
    monkeypatch.setenv("WEBSITE_SITE_NAME", "cloud")
    with pytest.raises(RuntimeError, match="never run in Azure"):
        create_app(org, local=True)
    monkeypatch.delenv("WEBSITE_SITE_NAME")
    app = create_app(
        org, store=LocalStore(tmp_path / "state.json", state), credential=object(), local=True
    )
    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/state", headers={"host": "attacker.example"}).status_code == 403
        assert (
            client.post(
                "/api/changes", headers={"origin": "https://attacker.example"}, json={}
            ).status_code
            == 403
        )


def test_policy_scales_to_2000_people_and_preserves_streaming(org, state):
    state["members"] = [{**state["members"][0], "id": str(uuid4())} for _ in range(2000)]
    suspended = add_member(state, status="suspended")
    state["settings"]["monthly_tokens"] = 1_000_000
    state["settings"]["member_tokens_per_minute"] = 10000
    policy, catalog = compile_policy(org, state)
    assert len(policy.encode()) < 250_000
    assert suspended["id"] not in policy
    assert "owner@example.com" not in policy
    tree = ET.fromstring(policy)
    assert tree.find("backend/forward-request").get("buffer-response") == "false"
    assert tree.find("inbound/authentication-managed-identity") is not None
    assert tree.find("inbound/validate-jwt") is not None
    assert len(tree.findall(".//llm-token-limit")) == 4
    assert (
        'Variables["allowedModels"]'
        in ET.fromstring(catalog).find("inbound/return-response/set-body").text
    )
    encoded = tree.find("inbound/set-variable[@name='governance']").get("value")
    compact = json.loads(json.loads(encoded[2:-1]))
    assert len(compact["m"]) == 2000
    assert compact["g"]["0"]["id"] == state["groups"][0]["id"]


def test_azure_publication_restores_first_policy_on_second_failure(org, state):
    class Azure:
        def __init__(self):
            self.calls = []

        def request(self, method, path, body=None):
            self.calls.append((method, path, body))
            if method == "GET":
                return {"properties": {"format": "xml", "value": "<old-policy/>"}}
            if "/operations/" in path:
                raise RuntimeError("Azure rejected catalog")

    azure = Azure()
    with pytest.raises(RuntimeError):
        publish(org, state, azure)
    writes = [call for call in azure.calls if call[0] == "PUT"]
    assert len(writes) == 3
    assert writes[-1][2]["properties"]["value"] == "<old-policy/>"
    assert writes[-1][2]["properties"]["format"] == "xml"


def test_failed_publication_retains_draft_and_audits_failure(app, org, key, monkeypatch):
    def fail(*args):
        raise RuntimeError("Sensitive upstream detail")

    monkeypatch.setattr("claude_foundry.portal.app.publish", fail)
    with TestClient(app) as client:
        data = app.state.store.read()[0]
        response = client.post(
            "/api/publish",
            headers=headers(org, key),
            json={"revision": 0, "organization": data["settings"]["name"]},
        )
        assert response.status_code == 502
        assert "Sensitive" not in response.text
        saved = app.state.store.read()[0]
        assert saved["publish_status"] == "failed"
        assert saved["published_revision"] is None
        assert [a["action"] for a in saved["audit"]] == [
            "gateway.publish_started",
            "gateway.publish_failed",
        ]


def test_publication_requires_review_and_success_sets_revision(app, org, key, monkeypatch):
    called = []
    monkeypatch.setattr("claude_foundry.portal.app.publish", lambda *args: called.append(args))
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/publish",
                headers=headers(org, key),
                json={"revision": 0, "organization": "Wrong"},
            ).status_code
            == 409
        )
        assert called == []
        data = app.state.store.read()[0]
        response = client.post(
            "/api/publish",
            headers=headers(org, key),
            json={"revision": 0, "organization": data["settings"]["name"]},
        )
        assert response.status_code == 200
        saved = app.state.store.read()[0]
        assert saved["publish_status"] == "published"
        assert saved["revision"] == saved["published_revision"]


def test_cost_analysis_enforces_roles_and_date_bounds(app, org, key):
    body = {"start": "2024-03-01", "end": "2024-03-02"}
    data, etag = app.state.store.read()
    developer = add_member(data)
    billing = add_member(data, "billing")
    app.state.store.save(data, etag)
    with TestClient(app) as client:
        assert client.post("/api/cost-analysis", json=body).status_code == 401
        assert (
            client.post(
                "/api/cost-analysis", json=body, headers=headers(org, key, developer["id"])
            ).status_code
            == 403
        )
        result = client.post(
            "/api/cost-analysis", json=body, headers=headers(org, key, billing["id"])
        )
        assert result.status_code == 200
        assert result.json()["status"] == "not_configured"
        assert (
            client.post(
                "/api/cost-analysis", json={**body, "end": "2024-09-01"}, headers=headers(org, key)
            ).status_code
            == 422
        )


def test_cost_cache_changes_with_rates_and_does_not_cache_failures(app, org, key, monkeypatch):
    calls = []

    def query(app_id, credential, config, filters, prices):
        calls.append(prices)
        if len(calls) == 1:
            raise ValueError("Transient query failure")
        return {"status": "connected", "totals": []}

    monkeypatch.setattr("claude_foundry.portal.app.cost_analysis", query)
    with TestClient(app) as client:
        body = {"start": "2024-03-01", "end": "2024-03-02"}
        auth = headers(org, key)
        assert client.post("/api/cost-analysis", json=body, headers=auth).status_code == 502
        assert client.post("/api/cost-analysis", json=body, headers=auth).status_code == 200
        client.post("/api/cost-analysis", json=body, headers=auth)
        assert len(calls) == 2
        data, etag = app.state.store.read()
        data["prices"] = [
            {"model": "claude-sonnet-4-6", "input_per_million": 3, "output_per_million": 15}
        ]
        app.state.store.save(data, etag)
        client.post("/api/cost-analysis", json=body, headers=auth)
        assert len(calls) == 3


def test_planning_rates_keep_public_source_and_reject_unsafe_urls(state):
    value = {
        "model": "claude-sonnet-4-6",
        "input_per_million": 3,
        "output_per_million": 15,
        "source_url": "https://platform.claude.com/docs/en/about-claude/pricing",
        "verified_on": "2026-10-14",
        "note": "Global Standard; public base rates.",
    }
    updated = change(state, "pricing", value)
    assert updated["prices"][0]["verified_on"] == "2026-10-14"
    assert updated["audit"][-1]["after"]["source_url"] == value["source_url"]
    with pytest.raises(ValueError):
        change(state, "pricing", {**value, "source_url": "javascript:alert(1)"})
