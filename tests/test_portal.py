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
    assert len(writes) == 4
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


def test_shared_target_drives_profiles_reporting_and_publication_without_resetting_state(
    app, org, key, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "claude_foundry.portal.app.publish", lambda config, *args: calls.append(config)
    )
    with TestClient(app) as client:
        auth = headers(org, key)
        before = client.get("/api/state", headers=auth).json()
        assert before["gateway_url"] == org["gateway_url"]
        profile = client.get("/api/client-profile", headers=auth).json()
        assert profile["gateway_url"] == org["gateway_url"]
        result = client.post(
            "/api/publish",
            headers=auth,
            json={
                "revision": before["revision"],
                "organization": before["settings"]["name"],
            },
        )
        assert result.status_code == 200
        after = client.get("/api/state", headers=auth).json()
        for field in ["members", "groups", "prices", "settings"]:
            assert after[field] == before[field]
        assert calls[0]["api_id"] == org["api_id"]
        assert calls[0]["gateway_url"] == profile["gateway_url"]
        from claude_foundry.portal.governance import impact

        other = {
            **org,
            "api_id": "claude-foundry-preview",
            "api_path": "claude-preview",
            "gateway_url": org["gateway_url"] + "-preview",
        }
        assert impact(other, after)["policy_hash"] != after["published_hash"]


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


@pytest.mark.parametrize(
    "claims", [{"scp": None}, {"scp": ["access_as_user"]}, {"oid": 123}, {"exp": None}]
)
def test_malformed_claim_types_fail_closed(app, org, key, claims):
    with TestClient(app) as client:
        response = client.get(
            "/api/state", headers={"Authorization": "Bearer " + token(org, key, **claims)}
        )
        assert response.status_code == 401


@pytest.mark.parametrize(
    "length,body,status",
    [
        ("1", b"x" * 1_000_001, 413),
        ("1", b"{}", 400),
        ("invalid", b"{}", 400),
        ("1000001", b"{}", 413),
    ],
)
def test_body_limit_checks_received_bytes_and_secures_rejections(app, length, body, status):
    with TestClient(app) as client:
        response = client.post("/api/changes", content=body, headers={"Content-Length": length})
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("role", ["owner", "admin", "billing", "auditor", "developer"])
def test_complete_role_read_and_write_journey(app, org, key, monkeypatch, role):
    data, etag = app.state.store.read()
    actor = add_member(data, role)
    app.state.store.save(data, etag)
    monkeypatch.setattr("claude_foundry.portal.app.publish", lambda *args: None)
    with TestClient(app) as client:
        auth = headers(org, key, actor["id"])
        read = client.get("/api/state", headers=auth)
        assert read.status_code == 200
        for route in ["/api/impact", "/api/telemetry"]:
            assert client.get(route, headers=auth).status_code == (
                403 if role == "developer" else 200
            )
        assert client.get("/api/client-profile", headers=auth).status_code == 200
        assert client.get(
            "/api/client-profile", params={"group_id": actor["group_id"]}, headers=auth
        ).status_code == (200 if role in {"owner", "admin"} else 403)
        csv = f"object_id,name,email\n{uuid4()},Test developer,test@example.com"
        values = {
            "member": {**actor, "id": str(uuid4()), "role": "developer"},
            "member_import": {"csv": csv, "group_id": actor["group_id"]},
            "group": {**data["groups"][0], "id": str(uuid4()), "name": "Test group"},
            "settings": {**data["settings"], "monthly_budget_usd": 123},
            "pricing": {
                "model": "claude-sonnet-4-6",
                "input_per_million": 3,
                "output_per_million": 15,
            },
            "request": {"model": "claude-sonnet-4-6", "reason": "Needed for validation"},
        }
        preview = client.post("/api/import-preview", headers=auth, json=values["member_import"])
        assert preview.status_code == (200 if role in {"owner", "admin"} else 403)
        for kind, value in values.items():
            before = app.state.store.read()[0]
            allowed = (
                role in {"owner", "admin"}
                or kind == "request"
                or (role == "billing" and kind in {"settings", "pricing"})
            )
            response = client.post(
                "/api/changes",
                headers=auth,
                json={"revision": before["revision"], "kind": kind, "value": value},
            )
            assert response.status_code == (200 if allowed else 403), (role, kind, response.text)
            after = app.state.store.read()[0]
            if not allowed:
                assert after == before
            else:
                assert after["revision"] == before["revision"] + 1
                assert after["audit"][-1]["actor_id"] == actor["id"]
        before = app.state.store.read()[0]
        decision = {"id": before["requests"][-1]["id"], "approve": True, "note": "Validated"}
        response = client.post(
            "/api/changes",
            headers=auth,
            json={"revision": before["revision"], "kind": "decision", "value": decision},
        )
        assert response.status_code == (200 if role in {"owner", "admin"} else 403)
        before = app.state.store.read()[0]
        result = client.post(
            "/api/publish",
            headers=auth,
            json={"revision": before["revision"], "organization": before["settings"]["name"]},
        )
        assert result.status_code == (200 if role in {"owner", "admin"} else 403)


def test_concurrent_role_revocation_cannot_authorize_against_newer_state(
    app, org, key, monkeypatch
):
    store = app.state.store
    data, etag = store.read()
    actor = add_member(data, "admin")
    store.save(data, etag)
    read = store.read
    changed = False

    def revoke_after_authorization_read():
        nonlocal changed
        snapshot, version = read()
        if not changed:
            changed = True
            latest = copy.deepcopy(snapshot)
            next(m for m in latest["members"] if m["id"] == actor["id"])["status"] = "suspended"
            latest["revision"] += 1
            store.save(latest, version)
        return snapshot, version

    monkeypatch.setattr(store, "read", revoke_after_authorization_read)
    with TestClient(app) as client:
        result = client.post(
            "/api/changes",
            headers=headers(org, key, actor["id"]),
            json={
                "revision": 1,
                "kind": "settings",
                "value": {**data["settings"], "name": "Unauthorized overwrite"},
            },
        )
        assert result.status_code == 409
        assert read()[0]["settings"]["name"] == data["settings"]["name"]


def test_pending_requests_are_deduplicated_and_decline_does_not_grant(state):
    value = {"model": "claude-sonnet-4-6", "reason": "Needed for evaluation"}
    pending = change(state, "request", value)
    with pytest.raises(ValueError, match="open request"):
        change(pending, "request", value)
    declined = change(
        pending,
        "decision",
        {"id": pending["requests"][0]["id"], "approve": False, "note": "Not needed"},
    )
    assert declined["requests"][0]["status"] == "declined"
    assert declined["members"][0]["extra_models"] == []
    assert len(change(declined, "request", value)["requests"]) == 2


def test_profile_is_scoped_and_has_valid_defaults(app, org, key, monkeypatch):
    monkeypatch.setenv("PORTAL_DESKTOP_CLIENT_ID", CLIENT)
    with TestClient(app) as client:
        response = client.get("/api/client-profile", headers=headers(org, key))
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        profile = response.json()
        assert profile["auth_mode"] == "msal"
        assert profile["client_id"] == CLIENT
        assert [m["name"] for m in profile["models"] if m["isFamilyDefault"]] == [
            "claude-sonnet-4-6"
        ]
        assert "subscription_id" not in profile
        assert "secret" not in response.text.lower()
        assert (
            client.get(
                "/api/client-profile", params={"group_id": str(uuid4())}, headers=headers(org, key)
            ).status_code
            == 404
        )
        data, etag = app.state.store.read()
        data["groups"][0].update(model_ids=[], default_model="")
        app.state.store.save(data, etag)
        assert client.get("/api/client-profile", headers=headers(org, key)).status_code == 409


def test_publication_timeout_restores_every_attempt_even_when_first_restore_fails(org, state):
    class Azure:
        def __init__(self):
            self.writes = []

        def request(self, method, path, body=None):
            if method == "GET":
                return {"properties": {"format": "rawxml", "value": "old"}}
            self.writes.append((path, body["properties"]["value"]))
            if len(self.writes) in {2, 3}:
                raise TimeoutError("Uncertain remote outcome")

    azure = Azure()
    with pytest.raises(RuntimeError, match="rollback is incomplete"):
        publish(org, state, azure)
    assert len(azure.writes) == 4
    assert [value for _, value in azure.writes[-2:]] == ["old", "old"]


def test_active_publication_blocks_edits_and_duplicate_publish(app, org, key):
    from claude_foundry.portal.domain import now

    data, etag = app.state.store.read()
    data.update(publish_status="publishing", publish_started_at=now())
    app.state.store.save(data, etag)
    with TestClient(app) as client:
        auth = headers(org, key)
        assert (
            client.post(
                "/api/changes",
                headers=auth,
                json={"revision": 0, "kind": "settings", "value": data["settings"]},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/publish",
                headers=auth,
                json={"revision": 0, "organization": data["settings"]["name"]},
            ).status_code
            == 409
        )


def test_invalid_group_and_default_cannot_be_saved(state):
    with pytest.raises(ValueError, match="existing group"):
        change(
            state,
            "member",
            {
                **state["members"][0],
                "id": str(uuid4()),
                "group_id": str(uuid4()),
                "role": "developer",
            },
        )
    with pytest.raises(ValueError, match="default model"):
        change(state, "group", {**state["groups"][0], "model_ids": []})
    with pytest.raises(ValueError, match="existing default group"):
        import_members(state, {"csv": "object_id,name,email", "group_id": []})


def test_chunked_oversized_body_is_stopped_before_application():
    import asyncio

    from claude_foundry.portal.boundary import RequestBoundary

    called, sent = [], []
    chunks = iter(
        [
            {"type": "http.request", "body": b"x" * 600_000, "more_body": True},
            {"type": "http.request", "body": b"x" * 600_000, "more_body": False},
        ]
    )

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    async def application(*args):
        called.append(True)

    asyncio.run(
        RequestBoundary(application)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/changes",
                "headers": [(b"content-length", b"1")],
            },
            receive,
            send,
        )
    )
    assert not called
    assert sent[0]["status"] == 413
    assert (b"cache-control", b"no-store") in sent[0]["headers"]


@pytest.fixture
def logs_app(app, org, monkeypatch):
    monkeypatch.setenv(
        "PORTAL_CONTENT_LOGS_JSON",
        json.dumps(
            {
                "workspace_id": CLIENT,
                "resource_id": f"/subscriptions/{CLIENT}/resourceGroups/test/providers/Microsoft.ApiManagement/service/test",
                "api_ids": ["claude-preview", "codex-foundry"],
                "capture_enabled": True,
            }
        ),
    )
    return create_app(org, store=app.state.store, credential=object(), local=False)


@pytest.mark.parametrize("role", ["owner", "admin", "billing", "auditor", "developer"])
def test_content_logs_role_boundary(logs_app, org, key, monkeypatch, role):
    data, etag = logs_app.state.store.read()
    member = add_member(data, role)
    logs_app.state.store.save(data, etag)
    monkeypatch.setattr(
        "claude_foundry.portal.app.list_logs", lambda *a: {"items": [], "has_more": False}
    )
    monkeypatch.setattr(
        "claude_foundry.portal.app.read_log",
        lambda *a: {"chunks": [{"response": "PRIVATE_TEST_CONTENT"}]},
    )
    with TestClient(logs_app) as client:
        auth = headers(org, key, member["id"])
        expected = 200 if role in {"owner", "admin"} else 403
        assert client.get("/api/content-logs/config", headers=auth).status_code == expected
        assert (
            client.post(
                "/api/content-logs/search",
                headers=auth,
                json={"start": "2026-10-14T00:00:00Z", "end": "2026-10-15T00:00:00Z"},
            ).status_code
            == expected
        )
        response = client.post(
            "/api/content-logs/read",
            headers=auth,
            json={"correlation_id": CLIENT, "reason": "Support investigation"},
        )
        assert response.status_code == expected
        assert ("PRIVATE_TEST_CONTENT" in response.text) == (expected == 200)
        assert response.headers["cache-control"] == "no-store"
    stored, _ = logs_app.state.store.read()
    if expected == 200:
        event = stored["audit"][-1]
        assert event["action"] == "content.view_requested"
        assert event["actor_id"] == member["id"]
        assert event["target"] == CLIENT
    assert "PRIVATE_TEST_CONTENT" not in json.dumps(stored)


def test_content_read_requires_durable_audit(logs_app, org, key, monkeypatch):
    calls = []
    monkeypatch.setattr("claude_foundry.portal.app.read_log", lambda *a: calls.append(a))

    def broken_save(*args):
        raise OSError("private storage not available")

    monkeypatch.setattr(logs_app.state.store, "save", broken_save)
    with TestClient(logs_app) as client:
        r = client.post(
            "/api/content-logs/read",
            headers=headers(org, key),
            json={"correlation_id": CLIENT, "reason": "Support investigation"},
        )
        assert r.status_code == 503
        assert "private storage" not in r.text
        assert calls == []


def test_content_api_rejects_bad_inputs_and_unauthenticated(logs_app, org, key):
    with TestClient(logs_app) as client:
        assert client.get("/api/content-logs/config").status_code == 401
        for value in [
            {"correlation_id": "bad-id", "reason": "Support"},
            {"correlation_id": CLIENT, "reason": "      "},
            {"correlation_id": CLIENT, "reason": "Support", "workspace_id": OWNER},
        ]:
            assert (
                client.post(
                    "/api/content-logs/read", headers=headers(org, key), json=value
                ).status_code
                == 422
            )
        r = client.post(
            "/api/content-logs/search",
            headers=headers(org, key),
            json={
                "start": "2026-10-14T00:00:00Z",
                "end": "2026-10-15T00:00:00Z",
                "api_id": "unrelated-customer",
            },
        )
        assert r.status_code == 422


def test_content_revocation_during_query_denies_response(logs_app, org, key, monkeypatch):
    def revoked(*args):
        state, etag = logs_app.state.store.read()
        state["members"][0]["status"] = "suspended"
        state["revision"] += 1
        logs_app.state.store.save(state, etag)
        return {"chunks": [{"response": "PRIVATE_TEST_CONTENT"}]}

    monkeypatch.setattr("claude_foundry.portal.app.read_log", revoked)
    with TestClient(logs_app) as client:
        r = client.post(
            "/api/content-logs/read",
            headers=headers(org, key),
            json={"correlation_id": CLIENT, "reason": "Support investigation"},
        )
        assert r.status_code == 403
        assert "PRIVATE_TEST_CONTENT" not in r.text


def test_content_disabled_without_configuration(app, org, key):
    with TestClient(app) as client:
        r = client.get("/api/content-logs/config", headers=headers(org, key))
        assert r.json()["configured"] is False
        assert (
            client.post(
                "/api/content-logs/read",
                headers=headers(org, key),
                json={"correlation_id": CLIENT, "reason": "Support investigation"},
            ).status_code
            == 409
        )


def test_group_admin_cannot_cross_scope_raise_ceiling_or_change_roles(state):
    group = state["groups"][0]
    group.update(
        spend_ceiling_usd="100", spend_limit_usd="80", delegated_models=["claude-sonnet-4-6"]
    )
    admin = add_member(state, "group_admin")
    developer = add_member(state)
    other_group = {**group, "id": str(uuid4()), "name": "Other team"}
    state["groups"].append(other_group)
    other = add_member(state, group_id=other_group["id"])
    updated = change(state, "member", {**developer, "spend_limit_usd": "20"}, admin)
    assert updated["members"][-1]["spend_limit_usd"] == "20"
    assert updated["audit"][-1]["group_id"] == group["id"]
    for kind, value in [
        ("group", {**group, "spend_ceiling_usd": "101"}),
        ("group", {**group, "delegated_models": []}),
        ("group", other_group),
        ("member", other),
        ("member", {**developer, "role": "group_admin"}),
        ("member", {**developer, "group_id": other_group["id"]}),
        ("settings", state["settings"]),
    ]:
        with pytest.raises(PermissionError):
            change(state, kind, value, admin)
    with pytest.raises(ValueError, match="allowance"):
        change(state, "member", {**developer, "spend_limit_usd": "81"}, admin)
    with pytest.raises(ValueError, match="ceiling"):
        change(state, "group", {**group, "spend_limit_usd": "101"}, admin)


def test_group_admin_endpoint_scope(app, org, key, monkeypatch):
    data, etag = app.state.store.read()
    group = data["groups"][0]
    group.update(delegated_models=["claude-sonnet-4-6"])
    admin = add_member(data, "group_admin")
    second = {**group, "id": str(uuid4()), "name": "Secret team"}
    data["groups"].append(second)
    outsider = add_member(data, group_id=second["id"])
    data["audit"] = [
        {"group_id": second["id"], "action": "secret"},
        {"group_id": group["id"], "action": "local"},
    ]
    app.state.store.save(data, etag)
    queried = []
    monkeypatch.setattr(
        "claude_foundry.portal.app.cost_analysis",
        lambda a, b, c, filters, p: queried.append(filters.group_ids) or {"ok": True},
    )
    with TestClient(app) as client:
        auth = headers(org, key, admin["id"])
        result = client.get("/api/state", headers=auth).json()
        assert [g["id"] for g in result["groups"]] == [group["id"]]
        assert outsider["id"] not in [m["id"] for m in result["members"]]
        assert result["audit"] == [{"group_id": group["id"], "action": "local"}]
        assert result["settings"] == {"name": data["settings"]["name"]}
        for route in ["/api/telemetry", "/api/impact"]:
            assert client.get(route, headers=auth).status_code == 403
        assert (
            client.get(
                "/api/client-profile", params={"group_id": second["id"]}, headers=auth
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/budgets/balance", params={"member_id": outsider["id"]}, headers=auth
            ).status_code
            == 403
        )
        assert client.post("/api/publish", json={}, headers=auth).status_code == 403
        assert (
            client.post(
                "/api/import-preview",
                json={
                    "csv": f"object_id,name,email\n{uuid4()},X,x@example.com",
                    "group_id": second["id"],
                },
                headers=auth,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/cost-analysis",
                json={"start": "2026-01-01", "end": "2026-01-02", "group_ids": [second["id"]]},
                headers=auth,
            ).status_code
            == 200
        )
        assert queried == [[group["id"]]]


def test_budget_service_accepts_only_dedicated_gateway_identity(org, key, monkeypatch):
    from claude_foundry.portal.budget_proxy import GatewayIdentity

    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, value: SimpleNamespace(key=key.public_key()),
    )
    identity = GatewayIdentity(org["tenant_id"], "api://" + CLIENT, OWNER)
    common = {"aud": "api://" + CLIENT, "scp": None}
    identity.validate("Bearer " + token(org, key, **common))
    identity.validate("Bearer " + token(org, key, **{**common, "aud": CLIENT, "ver": "2.0"}))
    for bad in [
        {"aud": str(uuid4())},
        {"oid": str(uuid4())},
        {"scp": "access_as_user"},
        {"tid": str(uuid4())},
        {"exp": int(time.time()) - 100},
        {"iss": "https://malicious.example.com"},
    ]:
        with pytest.raises(HTTPException):
            identity.validate("Bearer " + token(org, key, **{**common, **bad}))


def test_snapshot_origin_is_machine_only_and_does_not_use_portal_role_claims(
    org, state, key, monkeypatch, tmp_path
):
    gateway, audience = str(uuid4()), "api://" + str(uuid4())
    monkeypatch.setenv("PORTAL_OWNER_ID", OWNER)
    monkeypatch.setenv("PORTAL_CLIENT_ID", CLIENT)
    monkeypatch.setenv("BUDGET_POLICY_CACHE_AUDIENCE", audience)
    monkeypatch.setenv("BUDGET_POLICY_CACHE_APIM_PRINCIPAL_ID", gateway)
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, value: SimpleNamespace(key=key.public_key()),
    )
    reads = []

    def get(digest):
        reads.append(digest)
        return {"schema": 1, "members": []}

    app = create_app(
        org,
        LocalStore(tmp_path / "state.json", state),
        credential=object(),
        local=False,
        budget=(None, SimpleNamespace(get=get)),
    )
    path = "/api/internal/budget-policies/" + "a" * 64
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers(org, key)).status_code == 401
        for invalid in [
            {"oid": OWNER, "scp": None},
            {"oid": gateway, "scp": "access_as_user"},
            {"oid": gateway, "scp": None, "tid": str(uuid4())},
        ]:
            assert (
                client.get(
                    path,
                    headers={"Authorization": "Bearer " + token(org, key, aud=audience, **invalid)},
                ).status_code
                == 401
            )
        assert reads == []
        auth = {"Authorization": "Bearer " + token(org, key, oid=gateway, aud=audience, scp=None)}
        result = client.get(path, headers=auth)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert client.get("/api/internal/budget-policies/invalid", headers=auth).status_code == 400
        assert reads == ["a" * 64]


def test_snapshot_origin_disabled_by_default(app):
    with TestClient(app) as client:
        assert client.get("/api/internal/budget-policies/" + "a" * 64).status_code == 404


def test_local_navigation_is_allowed_but_cross_site_api_stays_blocked(
    org, state, monkeypatch, tmp_path
):
    monkeypatch.setenv("PORTAL_OWNER_ID", OWNER)
    application = create_app(
        org, LocalStore(tmp_path / "local.json", state), credential=object(), local=True
    )
    with TestClient(application) as client:
        navigation = {
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        }
        # 404 without a built frontend is acceptable; the boundary must not reject HTML navigation.
        assert client.get("/", headers=navigation).status_code in {200, 404}
        assert client.get("/api/state", headers=navigation).status_code == 403
        assert client.post("/api/changes", headers=navigation, json={}).status_code == 403


def test_group_content_scope_and_revoke_during_query(app, org, key, monkeypatch):
    data, etag = app.state.store.read()
    actor = add_member(data, "group_admin")
    app.state.store.save(data, etag)
    monkeypatch.setenv(
        "PORTAL_CONTENT_LOGS_JSON",
        json.dumps(
            {
                "workspace_id": str(uuid4()),
                "resource_id": f"/subscriptions/{uuid4()}/resourceGroups/test/providers/Microsoft.ApiManagement/service/test",
                "api_ids": ["claude-budget-preview"],
            }
        ),
    )
    application = create_app(org, app.state.store, credential=object(), local=False)
    scopes = []
    monkeypatch.setattr(
        "claude_foundry.portal.app.read_log",
        lambda *args, **kwargs: scopes.append(kwargs["group_id"]) or None,
    )
    with TestClient(application) as client:
        auth = headers(org, key, actor["id"])
        result = client.post(
            "/api/content-logs/read",
            json={"correlation_id": str(uuid4()), "reason": "Investigating team failure"},
            headers=auth,
        )
        assert result.status_code == 404
        assert scopes == [actor["group_id"]]
        assert app.state.store.read()[0]["audit"][-1]["group_id"] == actor["group_id"]

        def changed_scope(*args, **kwargs):
            data, etag = app.state.store.read()
            next(m for m in data["members"] if m["id"] == actor["id"])["role"] = "developer"
            app.state.store.save(data, etag)
            return {"items": [{"content": "must not escape"}]}

        monkeypatch.setattr("claude_foundry.portal.app.list_logs", changed_scope)
        result = client.post(
            "/api/content-logs/search",
            json={"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"},
            headers=auth,
        )
        assert result.status_code == 403 and "must not escape" not in result.text


@pytest.fixture
def budget_app(app, org):
    from datetime import UTC, datetime, timedelta

    from claude_foundry.portal.budget_ledger import Ledger, MemoryRows, policy_digest
    from claude_foundry.portal.budget_policy import snapshot

    data, etag = app.state.store.read()
    data["settings"].update(budget_enforcement=True, spend_limit_usd="10")
    data["groups"][0].update(spend_ceiling_usd="5", delegated_models=["claude-sonnet-4-6"])
    data["budget_prices"] = [
        dict(
            model="claude-sonnet-4-6",
            provider="anthropic",
            deployment_version="test",
            max_input_tokens=1000,
            max_output_tokens=1000,
            default_output_tokens=100,
            input_ceiling_per_million="3",
            output_ceiling_per_million="15",
            source_url="https://example.com",
            verified_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            valid_until=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            note="Synthetic test pricing only",
        )
    ]
    admin = add_member(data, "group_admin")
    developer = add_member(data)
    second = {**data["groups"][0], "id": str(uuid4()), "name": "Another team"}
    data["groups"].append(second)
    outsider = add_member(data, group_id=second["id"])
    app.state.store.save(data, etag)
    policy = snapshot(data)
    ledger = Ledger(MemoryRows())
    ledger.activate(policy)

    class Snapshots:
        def __init__(self):
            self.values = {policy_digest(policy): policy}

        def get(self, key):
            return copy.deepcopy(self.values[key])

        def put(self, value):
            key = policy_digest(value)
            self.values[key] = copy.deepcopy(value)
            return key

    snapshots = Snapshots()
    application = create_app(
        org, app.state.store, credential=object(), local=False, budget=(ledger, snapshots)
    )
    return application, ledger, snapshots, admin, developer, outsider


def test_budget_portal_scopes_and_owner_only_full_charge(budget_app, org, key):
    application, ledger, snapshots, admin, developer, outsider = budget_app
    policy = snapshots.get(ledger.active())
    requests = {}
    for member in [developer, outsider]:
        request = str(uuid4())
        requests[member["id"]] = request
        ledger.reserve(request, member["id"], "claude-sonnet-4-6", 10000, policy, "synthetic")
    with TestClient(application) as client:
        auth = headers(org, key, admin["id"])
        scopes = client.get("/api/budgets/balance", headers=auth).json()["scopes"]
        assert len(scopes) == 2 and all(s["scope"] != "org" for s in scopes)
        assert (
            client.get(
                "/api/budgets/balance", params={"member_id": outsider["id"]}, headers=auth
            ).status_code
            == 403
        )
        held = client.get("/api/budgets/reservations", headers=auth).json()["items"]
        assert [r["request_id"] for r in held] == [requests[developer["id"]]]
        assert "fingerprint" not in held[0]
        for member in [admin, developer, outsider]:
            assert (
                client.post(
                    "/api/budgets/close-reservation",
                    headers=headers(org, key, member["id"]),
                    json={
                        "request_id": requests[developer["id"]],
                        "reason": "Investigating incomplete request",
                    },
                ).status_code
                == 403
            )
        result = client.post(
            "/api/budgets/close-reservation",
            headers=headers(org, key),
            json={
                "request_id": requests[developer["id"]],
                "reason": "Usage unknown; retain maximum charge",
            },
        )
        assert result.status_code == 200 and result.json()["charged_micro_usd"] == 10000
        row, _ = ledger.rows.read("request-" + requests[developer["id"]])
        assert row["charged"] == row["reserved"] and row["state"] == "settled"
        audit = application.state.store.read()[0]["audit"][-1]
        assert audit["action"] == "budget.close_at_maximum_requested"
        assert audit["group_id"] == developer["group_id"]


def test_group_publication_excludes_owner_drafts_and_foreign_team(budget_app, org, key):
    application, ledger, snapshots, admin, developer, outsider = budget_app
    before = snapshots.get(ledger.active())
    data, etag = application.state.store.read()
    data["settings"]["spend_limit_usd"] = "99"
    data["groups"][1]["spend_ceiling_usd"] = "99"
    next(m for m in data["members"] if m["id"] == developer["id"])["spend_limit_usd"] = "1"
    application.state.store.save(data, etag)
    with TestClient(application) as client:
        result = client.post(
            "/api/budgets/publish",
            headers=headers(org, key, admin["id"]),
            json={"revision": data["revision"], "organization": data["settings"]["name"]},
        )
        assert result.status_code == 200, result.text
        active = snapshots.get(ledger.active())
        assert (
            active["settings"] == before["settings"] and active["groups"][1] == before["groups"][1]
        )
        assert (
            next(m for m in active["members"] if m["id"] == developer["id"])["spend_limit_usd"]
            == "1"
        )
        assert (
            client.post(
                "/api/budgets/publish", headers=headers(org, key, developer["id"]), json={}
            ).status_code
            == 403
        )


def test_budget_publication_and_closure_require_durable_audit(budget_app, org, key, monkeypatch):
    application, ledger, snapshots, admin, developer, outsider = budget_app
    active = ledger.active()
    policy = snapshots.get(active)
    request = str(uuid4())
    ledger.reserve(request, developer["id"], "claude-sonnet-4-6", 10000, policy, "test")
    data, _ = application.state.store.read()

    def unavailable(*args):
        raise OSError("Storage unavailable")

    monkeypatch.setattr(application.state.store, "save", unavailable)
    with TestClient(application) as client:
        assert (
            client.post(
                "/api/budgets/publish",
                headers=headers(org, key),
                json={
                    "revision": data["revision"],
                    "organization": data["settings"]["name"],
                    "published_hash": active,
                },
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/api/budgets/close-reservation",
                headers=headers(org, key),
                json={"request_id": request, "reason": "Investigating unknown usage"},
            ).status_code
            == 503
        )
    assert ledger.active() == active
    assert ledger.rows.read("request-" + request)[0]["state"] == "reserved"


def test_budget_portal_cannot_overwrite_gateway_with_legacy_policy(budget_app, org, key):
    application, *_ = budget_app
    data, _ = application.state.store.read()
    with TestClient(application) as client:
        response = client.post(
            "/api/publish",
            headers=headers(org, key),
            json={"revision": data["revision"], "organization": data["settings"]["name"]},
        )
    assert response.status_code == 409 and "Legacy gateway publication is disabled" in response.text


def test_blob_bootstrap_is_lazy_and_never_overwrites_existing_state(monkeypatch, state):
    from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

    from claude_foundry.portal.store import BlobStore

    class Blob:
        exists = True
        uploaded = None

        def get_blob_properties(self):
            if not self.exists:
                raise ResourceNotFoundError("Missing")

        def upload_blob(self, value, **options):
            assert options["overwrite"] is False
            if self.exists:
                raise ResourceExistsError("A different instance initialized first")
            self.uploaded = json.loads(value)

    blob = Blob()
    monkeypatch.setattr("claude_foundry.portal.store.BlobClient", lambda *a, **k: blob)
    calls = []

    def initial():
        calls.append(True)
        return state

    BlobStore("example", object(), initial)
    assert calls == [] and blob.uploaded is None
    blob.exists = False
    BlobStore("example", object(), initial)
    assert calls == [True] and blob.uploaded == state

    def racing_initial():
        blob.exists = True
        return {**state, "revision": 99}

    BlobStore("example", object(), racing_initial)
    assert blob.uploaded == state  # Another instance won; its state is preserved.


@pytest.mark.parametrize("role", ["admin", "group_admin"])
def test_only_owner_can_choose_unlimited(state, role):
    actor = add_member(state, role)
    group = state["groups"][0]
    before = copy.deepcopy(state)
    with pytest.raises(PermissionError):
        change(
            state,
            "group",
            {**group, "spend_ceiling_usd": None, "spend_ceiling_unlimited": True},
            actor,
        )
    with pytest.raises(PermissionError):
        change(
            state,
            "settings",
            {**state["settings"], "spend_limit_usd": None, "spend_limit_unlimited": True},
            actor,
        )
    assert state == before


@pytest.fixture
def unified_app(budget_app, org, monkeypatch):
    application, ledger, snapshots, admin, developer, outsider = budget_app
    state, etag = application.state.store.read()
    gpt = {"name": "gpt-test", "model": "gpt-test", "qualified": True}
    claude = {
        **org,
        "api_id": "claude-governed",
        "gateway_url": "https://test-apim.azure-api.net/claude-governed",
    }
    codex = {
        **org,
        "api_id": "codex-governed",
        "client_type": "codex",
        "gateway_url": "https://test-apim.azure-api.net/codex-governed/v1",
        "models": [gpt],
        "default_model": "gpt-test",
    }
    state["groups"][0]["model_ids"].append("gpt-test")
    state["groups"][0]["delegated_models"].append("gpt-test")
    state["budget_prices"].append(
        {**state["budget_prices"][0], "model": "gpt-test", "provider": "openai"}
    )
    application.state.store.save(state, etag)
    from claude_foundry.portal.budget_policy import snapshot

    policy = snapshot(state)
    snapshots.put(policy)
    ledger.activate(policy)
    monkeypatch.setenv(
        "PORTAL_PROVIDER_CONFIGS_JSON", json.dumps({"claude": claude, "codex": codex})
    )
    application = create_app(
        org, application.state.store, credential=object(), local=False, budget=(ledger, snapshots)
    )
    return application, ledger, snapshots, admin, developer, outsider


def test_shared_portal_lists_both_providers_and_exports_correct_profiles(unified_app, org, key):
    app, ledger, snapshots, *_ = unified_app
    with TestClient(app) as client:
        auth = headers(org, key)
        data = client.get("/api/state", headers=auth).json()
        assert data["client_types"] == ["claude", "codex"]
        assert {m["provider"] for m in data["models"]} == {"claude", "codex"}
        codex = client.get("/api/client-profile?client=codex", headers=auth)
        assert codex.status_code == 200, codex.text
        profile = codex.json()
        assert profile["client_type"] == "codex" and profile["default_model"] == "gpt-test"
        assert profile["gateway_url"].endswith("/codex-governed/v1")
        assert [m["name"] for m in profile["models"]] == ["gpt-test"]
        claude = client.get("/api/client-profile?client=claude", headers=auth).json()
        assert [m["name"] for m in claude["models"]] == ["claude-sonnet-4-6"]
        assert claude["gateway_url"].endswith("/claude-governed")
        assert client.get("/api/client-profile?client=invalid", headers=auth).status_code == 422
        assert client.get("/api/client-profile?client=codex").status_code == 401


def test_shared_profile_exports_enforce_group_scope_and_published_grants(unified_app, org, key):
    app, ledger, snapshots, admin, developer, outsider = unified_app
    data, etag = app.state.store.read()
    data["groups"][1]["model_ids"].append("gpt-test")  # Unpublished change must not grant access.
    data["groups"][1]["delegated_models"].append("gpt-test")
    app.state.store.save(data, etag)
    with TestClient(app) as client:
        assert (
            client.get(
                "/api/client-profile?client=codex", headers=headers(org, key, outsider["id"])
            ).status_code
            == 409
        )
        assert (
            client.get(
                "/api/client-profile",
                params={"client": "codex", "group_id": outsider["group_id"]},
                headers=headers(org, key, admin["id"]),
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/client-profile?client=codex", headers=headers(org, key, developer["id"])
            ).status_code
            == 200
        )
        assert (
            client.get("/api/model-policy", headers=headers(org, key, developer["id"])).status_code
            == 403
        )
        assert client.get("/api/model-policy", headers=headers(org, key)).status_code == 200


def test_shared_reporting_uses_both_fixed_api_scopes(unified_app, org, key, monkeypatch):
    app, *_ = unified_app
    scopes = []
    monkeypatch.setattr(
        "claude_foundry.portal.app.telemetry",
        lambda app_id, credential, config, days: scopes.append(config) or {"status": "connected"},
    )
    monkeypatch.setattr(
        "claude_foundry.portal.app.cost_analysis",
        lambda app_id, credential, config, body, prices: (
            scopes.append(config) or {"status": "connected"}
        ),
    )
    with TestClient(app) as client:
        auth = headers(org, key)
        assert client.get("/api/telemetry", headers=auth).status_code == 200
        assert (
            client.post(
                "/api/cost-analysis",
                json={"start": "2026-01-01", "end": "2026-01-02"},
                headers=auth,
            ).status_code
            == 200
        )
    assert len(scopes) == 2
    for config in scopes:
        assert {p["api_id"] for p in config["reporting_apis"]} == {
            "claude-governed",
            "codex-governed",
        }
