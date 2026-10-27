"""Authorization, policy compilation, and publication failure boundaries."""

import copy
import io
import json
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from claude_foundry.client_bundle import build_template
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


@pytest.mark.parametrize(
    "role", ["owner", "admin", "group_admin", "developer", "auditor", "billing"]
)
def test_setup_bundle_has_same_profile_and_scope_as_export(
    app, org, key, monkeypatch, tmp_path, role
):
    template = tmp_path / "client-bundle.zip"
    template.write_bytes(build_template(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("PORTAL_CLIENT_BUNDLE_TEMPLATE", str(template))
    data, etag = app.state.store.read()
    member = add_member(data, role)
    app.state.store.save(data, etag)
    auth = headers(org, key, member["id"])
    with TestClient(app) as client:
        assert client.get("/api/client-bundle").status_code == 401
        response = client.get("/api/client-bundle", headers=auth)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["cache-control"] == "no-store"
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            profile = json.loads(archive.read("claude-foundry/client.json"))
            assert profile == client.get("/api/client-profile", headers=auth).json()
            assert profile["gateway_url"] in archive.read("claude-foundry/README.md").decode()
            assert " --governed" in archive.read("claude-foundry/README.md").decode()
            assert archive.getinfo("claude-foundry/Install.command").external_attr >> 16 & 0o111
        assert client.get(
            "/api/client-bundle", params={"group_id": member["group_id"]}, headers=auth
        ).status_code == (200 if role in {"owner", "admin", "group_admin"} else 403)
        data, etag = app.state.store.read()
        member = next(m for m in data["members"] if m["id"] == member["id"])
        member["status"] = "suspended"
        app.state.store.save(data, etag)
        assert client.get("/api/client-bundle", headers=auth).status_code == 403


def test_setup_bundle_reports_missing_template_after_authorization(
    app, org, key, monkeypatch, tmp_path
):
    monkeypatch.setenv("PORTAL_CLIENT_BUNDLE_TEMPLATE", str(tmp_path / "missing.zip"))
    with TestClient(app) as client:
        assert client.get("/api/client-bundle").status_code == 401
        assert client.get("/api/client-bundle", headers=headers(org, key)).status_code == 503


def test_deployed_bundle_is_found_outside_the_function_worker_directory(
    app, org, key, monkeypatch, tmp_path
):
    site = tmp_path / "wwwroot"
    site.mkdir()
    (site / "client-bundle.zip").write_bytes(build_template(Path(__file__).resolve().parents[1]))
    monkeypatch.delenv("PORTAL_CLIENT_BUNDLE_TEMPLATE", raising=False)
    monkeypatch.setattr(
        "claude_foundry.portal.app.__file__", str(site / "claude_foundry/portal/app.py")
    )
    worker = tmp_path / "azure-functions-host"
    worker.mkdir()
    monkeypatch.chdir(worker)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["bundle_ready"] is True
        response = client.get("/api/client-bundle", headers=headers(org, key))
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert (
                json.loads(archive.read("claude-foundry/client.json"))["gateway_url"]
                == org["gateway_url"]
            )
        (site / "client-bundle.zip").rename(site / "previous-client-bundle.zip")
        assert client.get("/api/health").json()["bundle_ready"] is False


def test_bundle_readiness_detects_an_unreadable_template(app, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("different runtime user")

    monkeypatch.setattr(
        "claude_foundry.portal.app.bundle_template_path", lambda: SimpleNamespace(open=denied)
    )
    with TestClient(app) as client:
        assert client.get("/api/health").json()["bundle_ready"] is False


@pytest.mark.parametrize("route", ["/api/client-profile", "/api/client-bundle"])
def test_unconfigured_client_selector_never_downloads_the_wrong_application(app, org, key, route):
    with TestClient(app) as client:
        assert (
            client.get(route, params={"client": "codex"}, headers=headers(org, key)).status_code
            == 409
        )
        assert (
            client.get(route, params={"client": "unknown"}, headers=headers(org, key)).status_code
            == 422
        )


def test_budget_setup_bundle_uses_published_gateway_and_access(
    org, state, key, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    template = tmp_path / "client-bundle.zip"
    template.write_bytes(build_template(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("PORTAL_CLIENT_BUNDLE_TEMPLATE", str(template))
    monkeypatch.setenv("PORTAL_OWNER_ID", OWNER)
    monkeypatch.setenv("PORTAL_CLIENT_ID", CLIENT)
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda *args: SimpleNamespace(key=key.public_key()),
    )
    published = copy.deepcopy(state)
    store = LocalStore(tmp_path / "state.json", state)
    budget = (
        SimpleNamespace(active=lambda: "published"),
        SimpleNamespace(get=lambda _: copy.deepcopy(published)),
    )
    app = create_app(org, store=store, credential=object(), local=False, budget=budget)
    gateway = "https://test-apim.azure-api.net/claude-budget-preview"
    with TestClient(app) as client:
        auth = headers(org, key)
        refused = client.get("/api/client-bundle", headers=auth)
        assert refused.status_code == 409
        assert "Ask your administrator" in refused.json()["detail"]
        assert "gateway URL" not in refused.json()["detail"]
        assert client.get("/api/health").json()["setup_ready"] is False
        monkeypatch.setenv("BUDGET_CLAUDE_GATEWAY_URL", gateway)
        assert client.get("/api/health").json()["setup_ready"] is True
        response = client.get("/api/client-bundle", headers=auth)
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert json.loads(archive.read("claude-foundry/client.json"))["gateway_url"] == gateway
            assert gateway in archive.read("claude-foundry/README.md").decode()
        published["groups"][0].update(model_ids=[], default_model="")
        assert client.get("/api/client-bundle", headers=auth).status_code == 409


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
        assert client.get("/api/health").json() == {
            "status": "ok",
            "setup_ready": True,
            "client_types": ["claude", "codex"],
            "bundle_ready": False,
            "client_enrollment_enabled": False,
        }
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


def test_shared_setup_zips_match_published_provider_profiles(
    unified_app, org, key, monkeypatch, tmp_path
):
    import io
    import zipfile
    from pathlib import Path

    from claude_foundry.client_bundle import build_template

    app, ledger, snapshots, admin, developer, outsider = unified_app
    template = tmp_path / "template.zip"
    template.write_bytes(build_template(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("PORTAL_CLIENT_BUNDLE_TEMPLATE", str(template))
    with TestClient(app) as client:
        auth = headers(org, key, developer["id"])
        for provider in ["claude", "codex"]:
            profile = client.get(
                "/api/client-profile", params={"client": provider}, headers=auth
            ).json()
            response = client.get("/api/client-bundle", params={"client": provider}, headers=auth)
            assert response.status_code == 200, (
                response.text[:100] if response.status_code != 200 else ""
            )
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert json.loads(archive.read("claude-foundry/client.json")) == profile
                readme = archive.read("claude-foundry/README.md").decode()
                assert profile["gateway_url"] in readme
                assert "claude-foundry/AGENTS.md" in archive.namelist()
                assert "claude-foundry/Install.command" in archive.namelist()
                if provider == "codex":
                    assert profile["client_type"] == "codex"
                    assert profile["default_model"] in {m["name"] for m in profile["models"]}
                    assert profile["gateway_url"].endswith("/codex-governed/v1")
                    assert "Codex Desktop / CLI" in readme
                else:
                    assert "--budgeted" in readme and "--governed" not in readme
            denied = client.get(
                "/api/client-bundle",
                params={"client": provider, "group_id": outsider["group_id"]},
                headers=auth,
            )
            assert (
                denied.status_code == 403
                and denied.headers.get("content-type") == "application/json"
            )
        assert client.get("/api/client-bundle?client=codex").status_code == 401
        assert (
            client.get(
                "/api/client-bundle?client=codex", headers=headers(org, key, outsider["id"])
            ).status_code
            == 409
        )


CONFIG_RESOURCE = "00000000-0000-0000-0000-000000000005"
CONFIG_CLIENT = "00000000-0000-0000-0000-000000000006"
CONFIG_ORGANIZATION = "00000000-0000-0000-0000-000000000007"
CONFIG_PATH = "/api/v1/me/client-configuration"


def test_cli_enrollment_descriptor_and_configuration_keep_separate_audiences(
    enrollment_app, org, key, monkeypatch
):
    from claude_foundry.configuration_client import AZURE_CLI_CLIENT_ID

    old, ledger, snapshots, *members = enrollment_app
    monkeypatch.setenv("PORTAL_ENROLLMENT_CLIENT_ID", AZURE_CLI_CLIENT_ID)
    monkeypatch.setenv("PORTAL_CONFIGURATION_CLIENT_IDS_JSON", json.dumps([AZURE_CLI_CLIENT_ID]))
    app = create_app(
        org, old.state.store, credential=object(), local=False, budget=(ledger, snapshots)
    )
    with TestClient(app) as client:
        response = client.get("/api/client-enrollment", headers=headers(org, key))
        assert response.status_code == 200
        descriptor = response.json()["descriptor"]
        assert descriptor["schema_version"] == 3
        assert descriptor["public_client_id"] == AZURE_CLI_CLIENT_ID
        config_auth = configuration_headers(org, key, azp=AZURE_CLI_CLIENT_ID)
        assert client.get(CONFIG_PATH, headers=config_auth).status_code == 200
        assert client.get("/api/state", headers=config_auth).status_code == 401
        assert client.get(CONFIG_PATH, headers=headers(org, key)).status_code == 401
        inference_auth = configuration_headers(
            org,
            key,
            azp=AZURE_CLI_CLIENT_ID,
            aud="https://cognitiveservices.azure.com",
            scp="user_impersonation",
        )
        assert client.get(CONFIG_PATH, headers=inference_auth).status_code == 401


@pytest.fixture
def configuration_app(unified_app, org, monkeypatch):
    application, ledger, snapshots, *members = unified_app
    policy = snapshots.get(ledger.active())
    bounds = {
        price["model"]: {
            "provider": price["provider"],
            "version": price["deployment_version"],
            "max_input_tokens": price["max_input_tokens"],
            "max_output_tokens": price["max_output_tokens"],
        }
        for price in policy["budget_prices"]
    }
    for name, value in {
        "PORTAL_CLIENT_CONFIGURATION_ENABLED": "1",
        "PORTAL_CONFIGURATION_RESOURCE_ID": CONFIG_RESOURCE,
        "PORTAL_CONFIGURATION_CLIENT_IDS_JSON": json.dumps([CONFIG_CLIENT]),
        "BUDGET_ORGANIZATION_ID": CONFIG_ORGANIZATION,
        "PORTAL_CONFIGURATION_DEPLOYMENTS_JSON": json.dumps(bounds),
    }.items():
        monkeypatch.setenv(name, value)
    application = create_app(
        org, application.state.store, credential=object(), local=False, budget=(ledger, snapshots)
    )
    return application, ledger, snapshots, *members


def configuration_headers(org, key, oid=OWNER, **overrides):
    return {
        "Authorization": "Bearer "
        + token(
            org,
            key,
            oid,
            **{
                "aud": CONFIG_RESOURCE,
                "azp": CONFIG_CLIENT,
                "scp": "configuration.read",
                **overrides,
            },
        )
    }


@pytest.fixture
def enrollment_app(configuration_app, org, monkeypatch, tmp_path):
    application, ledger, snapshots, *members = configuration_app
    for name, value in {
        "PORTAL_ENROLLMENT_CLIENT_ID": CONFIG_CLIENT,
        "PORTAL_CLIENT_ENROLLMENT_ENABLED": "1",
        "PORTAL_ENROLLMENT_ORIGIN": "https://portal.example.com",
    }.items():
        monkeypatch.setenv(name, value)
    template = tmp_path / "template.zip"
    template.write_bytes(build_template(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("PORTAL_CLIENT_BUNDLE_TEMPLATE", str(template))
    application = create_app(
        org, application.state.store, credential=object(), local=False, budget=(ledger, snapshots)
    )
    return application, ledger, snapshots, *members


def test_enrollment_is_opt_in_and_requires_browser_identity(configuration_app, org, key):
    app, *_ = configuration_app
    with TestClient(app) as client:
        assert not client.get("/api/state", headers=headers(org, key)).json()[
            "client_enrollment_enabled"
        ]
        assert client.get("/api/client-enrollment", headers=headers(org, key)).status_code == 404
        assert client.get("/api/client-enrollment").status_code == 401
        assert (
            client.get(
                "/api/client-enrollment", headers=configuration_headers(org, key)
            ).status_code
            == 401
        )


def test_enrollment_uses_own_published_grants_and_fixed_origin(enrollment_app, org, key):
    app, ledger, snapshots, admin, developer, outsider = enrollment_app
    before, draft = copy.deepcopy(ledger.rows.rows), app.state.store.read()
    with TestClient(app) as client:
        auth = {**headers(org, key, developer["id"]), "Host": "hostile.example.com"}
        response = client.get("/api/client-enrollment", headers=auth)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["clients"] == ["claude", "codex"]
        assert value["onboarding_url"] == "https://portal.example.com/?page=onboarding"
        assert (
            value["descriptor"]["configuration_url"] == "https://portal.example.com" + CONFIG_PATH
        )
        assert value["descriptor"]["organization_id"] == CONFIG_ORGANIZATION
        assert value["descriptor"]["schema_version"] == 2
        assert value["descriptor"]["public_client_id"] == CONFIG_CLIENT
        assert set(value["descriptor"]) == {
            "schema_version",
            "organization_id",
            "tenant_id",
            "configuration_url",
            "configuration_resource_id",
            "public_client_id",
            "routes",
        }
        assert "hostile" not in response.text and developer["id"] not in response.text
        assert "models" not in response.text and "budget" not in response.text
        assert response.headers["cache-control"] == "no-store"
        denied = client.get("/api/client-enrollment?group_id=" + outsider["group_id"], headers=auth)
        assert denied.status_code == 400
        # Outsider has published Claude access only; no selector can supply Codex grants.
        own = client.get("/api/client-enrollment", headers=headers(org, key, outsider["id"]))
        assert own.status_code == 200 and own.json()["clients"] == ["claude"]
        for selection in ["codex", "both"]:
            assert (
                client.get(
                    "/api/client-enrollment-bundle?client=" + selection,
                    headers=headers(org, key, outsider["id"]),
                ).status_code
                == 409
            )
        assert (
            client.get("/api/client-enrollment-bundle?client=both", headers=auth).status_code == 200
        )
    assert ledger.rows.rows == before and app.state.store.read() == draft


def test_enrollment_bundle_is_not_a_user_specific_authorization_artifact(enrollment_app, org, key):
    app, _, _, _, developer, _ = enrollment_app
    with TestClient(app) as client:
        descriptors = []
        for oid in [OWNER, developer["id"]]:
            response = client.get(
                "/api/client-enrollment-bundle?client=claude", headers=headers(org, key, oid)
            )
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
                descriptors.append(json.loads(bundle.read("claude-foundry/enrollment.json")))
                assert json.loads(bundle.read("claude-foundry/installation.json")) == {
                    "schema_version": 1,
                    "clients": ["claude"],
                }
                assert "claude-foundry/client.json" not in bundle.namelist()
                assert oid.encode() not in bundle.read("claude-foundry/enrollment.json")
        assert descriptors[0] == descriptors[1]
        for query in ["client=claude&group_id=platform", "client=claude&client=codex"]:
            assert (
                client.get(
                    "/api/client-enrollment-bundle?" + query, headers=headers(org, key)
                ).status_code
                == 400
            )
        assert client.get("/api/client-enrollment-bundle?client=claude").status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        "http://portal.example.com",
        "https://portal.example.com/",
        "https://portal.example.com/path",
        "https://169.254.169.254",
        "https://portal.example.com?url=",
    ],
)
def test_enrollment_rejects_unsafe_or_noncanonical_origins(
    enrollment_app, org, monkeypatch, origin
):
    app, ledger, snapshots, *_ = enrollment_app
    monkeypatch.setenv("PORTAL_ENROLLMENT_ORIGIN", origin)
    with pytest.raises(ValueError):
        create_app(
            org, app.state.store, credential=object(), local=False, budget=(ledger, snapshots)
        )


def test_enrollment_requires_its_credential_client_to_be_allowed(
    configuration_app, org, monkeypatch
):
    app, ledger, snapshots, *_ = configuration_app
    monkeypatch.setenv("PORTAL_CLIENT_ENROLLMENT_ENABLED", "1")
    with pytest.raises(ValueError, match="organization public client"):
        create_app(
            org, app.state.store, credential=object(), local=False, budget=(ledger, snapshots)
        )


def test_enrollment_fails_closed_during_pause_or_revocation(enrollment_app, org, key):
    app, ledger, snapshots, _, developer, _ = enrollment_app
    policy = copy.deepcopy(snapshots.get(ledger.active()))
    member = next(m for m in policy["members"] if m["id"] == developer["id"])
    member["status"] = "suspended"
    snapshots.put(policy)
    ledger.activate(policy)
    with TestClient(app) as client:
        for path in ["/api/client-enrollment", "/api/client-enrollment-bundle?client=claude"]:
            assert client.get(path, headers=headers(org, key, developer["id"])).status_code == 403


def test_configuration_feed_own_user_only_data_and_no_mutation(configuration_app, org, key):
    app, ledger, snapshots, admin, developer, outsider = configuration_app
    before = copy.deepcopy(ledger.rows.rows)
    draft_before = app.state.store.read()
    with TestClient(app) as client:
        response = client.get(CONFIG_PATH, headers=configuration_headers(org, key, developer["id"]))
        assert response.status_code == 200, response.text
        assert response.json() == {
            "schema_version": 1,
            "organization_id": CONFIG_ORGANIZATION,
            "tenant_id": org["tenant_id"],
            "subject_id": developer["id"],
            "state": "active",
            "clients": {
                "claude": {
                    "route_id": "claude-governed",
                    "default_model": "claude-sonnet-4-6",
                    "models": [
                        {
                            "name": "claude-sonnet-4-6",
                            "family_tier": "sonnet",
                            "family_default": True,
                        }
                    ],
                },
                "codex": {
                    "route_id": "codex-governed",
                    "default_model": "gpt-test",
                    "models": [{"name": "gpt-test", "model": "gpt-test"}],
                },
            },
        }
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["vary"] == "Authorization"
        etag = response.headers["etag"]
        conditional = client.get(
            CONFIG_PATH,
            headers={**configuration_headers(org, key, developer["id"]), "If-None-Match": etag},
        )
        assert conditional.status_code == 304 and conditional.content == b""
        assert conditional.headers["etag"] == etag
    assert ledger.rows.rows == before
    assert app.state.store.read() == draft_before


def test_configuration_feed_omits_unqualified_codex_models(configuration_app, org):
    from claude_foundry.portal.configuration_feed import client_configuration, deployment_bounds

    _, ledger, snapshots, *_ = configuration_app
    providers = json.loads(os.environ["PORTAL_PROVIDER_CONFIGS_JSON"])
    providers["codex"]["models"][0]["qualified"] = False
    # Startup also rejects unqualified provider metadata. Keep the projection
    # defensive if it is called independently of app construction.
    result = client_configuration(
        providers,
        deployment_bounds(json.loads(os.environ["PORTAL_CONFIGURATION_DEPLOYMENTS_JSON"])),
        snapshots.get(ledger.active()),
        CONFIG_ORGANIZATION,
        org["tenant_id"],
        OWNER,
    )
    assert result["clients"]["codex"] == {
        "route_id": "codex-governed",
        "default_model": "",
        "models": [],
    }


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": CLIENT},
        {"aud": "https://cognitiveservices.azure.com"},
        {"azp": CLIENT},
        {"azp": str(uuid4())},
        {"azp": []},
        {"scp": "access_as_user"},
        {"scp": "configuration.read.more"},
        {"scp": ["configuration.read"]},
        {"scp": None, "roles": ["configuration.read"]},
        {"idtyp": "app"},
        {"oid": "not-a-uuid"},
        {"tid": str(uuid4())},
        {"iss": "https://login.microsoftonline.com/common/v2.0"},
        {"exp": 1},
        {"nbf": int(time.time()) + 600},
    ],
)
def test_configuration_feed_rejects_wrong_identity_claims(configuration_app, org, key, claims):
    app, *_ = configuration_app
    with TestClient(app) as client:
        response = client.get(CONFIG_PATH, headers=configuration_headers(org, key, **claims))
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"


def test_configuration_and_portal_tokens_are_not_interchangeable(configuration_app, org, key):
    app, *_ = configuration_app
    with TestClient(app) as client:
        assert client.get(CONFIG_PATH, headers=headers(org, key)).status_code == 401
        assert client.get(CONFIG_PATH).status_code == 401
        assert client.get("/api/state", headers=configuration_headers(org, key)).status_code == 401
        assert (
            client.post("/api/publish", headers=configuration_headers(org, key)).status_code == 401
        )
        assert (
            client.get("/api/client-profile", headers=configuration_headers(org, key)).status_code
            == 401
        )
        assert client.get("/api/state", headers=headers(org, key)).status_code == 200


@pytest.mark.parametrize("query", ["member_id", "group_id", "tenant_id", "client", "url"])
def test_configuration_feed_rejects_all_caller_selectors(configuration_app, org, key, query):
    app, *_ = configuration_app
    with TestClient(app) as client:
        response = client.get(
            CONFIG_PATH,
            params={query: str(uuid4())},
            headers=configuration_headers(org, key),
        )
        assert response.status_code == 400


@pytest.mark.parametrize("revocation", ["suspended", "removed"])
def test_configuration_reauthorizes_before_304(configuration_app, org, key, revocation):
    app, ledger, snapshots, admin, developer, outsider = configuration_app
    auth = configuration_headers(org, key, developer["id"])
    with TestClient(app) as client:
        first = client.get(CONFIG_PATH, headers=auth)
        policy = snapshots.get(ledger.active())
        if revocation == "removed":
            policy["members"] = [m for m in policy["members"] if m["id"] != developer["id"]]
        else:
            next(m for m in policy["members"] if m["id"] == developer["id"])["status"] = "suspended"
        snapshots.put(policy)
        ledger.activate(policy)
        response = client.get(CONFIG_PATH, headers={**auth, "If-None-Match": first.headers["etag"]})
        assert response.status_code == 403
        assert "etag" not in response.headers


def test_configuration_ignores_unpublished_drafts_and_unrelated_publications(
    configuration_app, org, key
):
    app, ledger, snapshots, *_ = configuration_app
    with TestClient(app) as client:
        auth = configuration_headers(org, key)
        first = client.get(CONFIG_PATH, headers=auth)
        draft, tag = app.state.store.read()
        draft["members"][0].update(status="suspended", extra_models=["unpublished-model"])
        app.state.store.save(draft, tag)
        policy = snapshots.get(ledger.active())
        policy["settings"]["spend_limit_usd"] = "9"
        snapshots.put(policy)
        ledger.activate(policy)
        assert (
            client.get(CONFIG_PATH, headers={**auth, "If-None-Match": first.headers["etag"]})
        ).status_code == 304


def test_configuration_feed_revalidates_price_and_deployment_before_304(
    configuration_app, org, key
):
    app, ledger, snapshots, *_ = configuration_app
    with TestClient(app) as client:
        auth = configuration_headers(org, key)
        first = client.get(CONFIG_PATH, headers=auth)
        policy = snapshots.get(ledger.active())
        policy["budget_prices"][0]["deployment_version"] = "changed-but-not-reviewed"
        policy["budget_prices"][1]["valid_until"] = "2000-01-01T00:00:00Z"
        # Keep the price shape valid so this exercises currentness, not validation failure.
        policy["budget_prices"][1]["verified_at"] = "1999-12-31T00:00:00Z"
        snapshots.put(policy)
        ledger.activate(policy)
        response = client.get(CONFIG_PATH, headers={**auth, "If-None-Match": first.headers["etag"]})
        assert response.status_code == 200
        assert response.json()["state"] == "no_models"
        assert response.json()["clients"]["claude"]["models"] == []
        assert response.json()["clients"]["codex"]["models"] == []
        assert response.headers["etag"] != first.headers["etag"]


@pytest.mark.parametrize("failure", ["paused", "snapshot", "concurrent_publish"])
def test_configuration_fails_closed_on_policy_unavailability(
    configuration_app, org, key, monkeypatch, failure
):
    app, ledger, snapshots, *_ = configuration_app
    with TestClient(app) as client:
        auth = configuration_headers(org, key)
        first = client.get(CONFIG_PATH, headers=auth)
        if failure == "paused":
            meta, tag = ledger.rows.read("policy")
            ledger.rows.commit([("policy", {**meta, "paused": True}, tag)])
        elif failure == "snapshot":
            snapshots.values.clear()
        else:
            old = ledger.active()
            results = iter([old, "new-digest"])
            monkeypatch.setattr(ledger, "active", lambda: next(results))
        response = client.get(CONFIG_PATH, headers={**auth, "If-None-Match": first.headers["etag"]})
        assert response.status_code == 503 and "etag" not in response.headers


def test_configuration_disabled_by_default(unified_app, org, key):
    app, *_ = unified_app
    with TestClient(app) as client:
        assert client.get(CONFIG_PATH, headers=configuration_headers(org, key)).status_code == 404


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORTAL_CONFIGURATION_RESOURCE_ID", CLIENT),
        ("PORTAL_CONFIGURATION_CLIENT_IDS_JSON", json.dumps([CLIENT.upper()])),
        ("PORTAL_CONFIGURATION_CLIENT_IDS_JSON", json.dumps([CONFIG_RESOURCE])),
        ("PORTAL_CONFIGURATION_CLIENT_IDS_JSON", json.dumps([CONFIG_CLIENT, CONFIG_CLIENT])),
        ("PORTAL_CONFIGURATION_CLIENT_IDS_JSON", "[]"),
        ("PORTAL_CONFIGURATION_CLIENT_IDS_JSON", '"not-a-list"'),
        ("PORTAL_CONFIGURATION_CLIENT_IDS_JSON", '["not-a-uuid"]'),
        ("PORTAL_CONFIGURATION_DEPLOYMENTS_JSON", "{}"),
        ("PORTAL_CLIENT_CONFIGURATION_ENABLED", "true"),
    ],
)
def test_configuration_startup_rejects_unsafe_settings(
    configuration_app, org, monkeypatch, name, value
):
    app, ledger, snapshots, *_ = configuration_app
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        create_app(
            org, app.state.store, credential=object(), local=False, budget=(ledger, snapshots)
        )


def test_configuration_never_inherits_local_owner_mode(configuration_app, org):
    app, ledger, snapshots, *_ = configuration_app
    with pytest.raises(ValueError, match="authenticated shared"):
        create_app(
            org, app.state.store, credential=object(), local=True, budget=(ledger, snapshots)
        )
