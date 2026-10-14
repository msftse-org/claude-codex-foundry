"""Authenticated administration API and static portal entry point."""

import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from azure.identity import AzureCliCredential, ManagedIdentityCredential
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..config import export_client, load_org
from .auth import Authenticator
from .boundary import RequestBoundary
from .costs import CostQuery, cost_analysis
from .domain import Change, apply_change, audit, effective_models, import_members, now, seed
from .governance import impact
from .service import ManagedAzure, publish, telemetry
from .store import BlobStore, Conflict, LocalStore


def create_app(config=None, store=None, credential=None, local=None):
    config = config or (
        json.loads(os.environ["PORTAL_ORGANIZATION_JSON"])
        if os.getenv("PORTAL_ORGANIZATION_JSON")
        else load_org(os.environ["PORTAL_ORGANIZATION_PATH"])
    )
    local = os.getenv("PORTAL_LOCAL_MODE") == "1" if local is None else local
    if local and (os.getenv("WEBSITE_SITE_NAME") or os.getenv("CONTAINER_APP_NAME")):
        raise RuntimeError("Local administrator mode must never run in Azure")
    owner_id = str(UUID(os.environ["PORTAL_OWNER_ID"]))
    client_id = os.getenv("PORTAL_CLIENT_ID", "")
    auth = Authenticator(config["tenant_id"], client_id)
    credential = credential or (
        AzureCliCredential(subscription=config["subscription_id"])
        if local
        else ManagedIdentityCredential(client_id=os.getenv("AZURE_CLIENT_ID"))
    )
    azure = ManagedAzure(config["subscription_id"], credential)
    app = FastAPI(title="Claude Foundry Admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.telemetry_cache = None
    app.state.cost_cache = {}

    def storage():
        if app.state.store is None:
            initial = seed(
                config,
                owner_id,
                os.getenv("PORTAL_OWNER_NAME", "Organization owner"),
                os.getenv("PORTAL_OWNER_EMAIL", "owner@example.com"),
            )
            app.state.store = (
                LocalStore(os.getenv("PORTAL_LOCAL_PATH", ".local/portal/state.json"), initial)
                if local
                else BlobStore(os.environ["PORTAL_STORAGE_ACCOUNT"], credential, initial)
            )
        return app.state.store

    app.add_middleware(RequestBoundary, local=local)

    def actor(request: Request):
        oid = owner_id if local else auth.identity(request.headers.get("authorization"))
        state, etag = storage().read()
        member = next(
            (m for m in state["members"] if m["id"] == oid and m["status"] == "active"), None
        )
        if not member:
            raise HTTPException(
                403, "Your organization has not granted portal access, or your access is suspended."
            )
        return state, etag, member

    def administrative(member):
        if member["role"] == "developer":
            raise HTTPException(403, "An administration role is required.")

    actor_dependency = Depends(actor)

    @app.get("/api/bootstrap")
    def bootstrap():
        return {
            "tenantId": config["tenant_id"],
            "clientId": client_id,
            "local": local,
            "configured": bool(client_id) or local,
            "name": "Claude Foundry Admin",
        }

    @app.get("/api/state")
    def state(access=actor_dependency):
        data, _, member = access
        data.update(
            viewer=member,
            models=config["models"],
            gateway_url=config["gateway_url"],
            tenant_id=config["tenant_id"],
            policy_hash=impact(config, data)["policy_hash"],
        )
        if member["role"] == "developer":
            data.update(
                members=[member],
                groups=[g for g in data["groups"] if g["id"] == member["group_id"]],
                audit=[],
                prices=[],
                settings={"name": data["settings"]["name"]},
                requests=[r for r in data["requests"] if r["member_id"] == member["id"]],
            )
        return data

    @app.post("/api/changes")
    def change(body: Change, access=actor_dependency):
        data, etag, member = access
        if data["revision"] != body.revision:
            raise HTTPException(
                409, "Another administrator saved changes. Reload and review again."
            )
        if data["publish_status"] == "publishing":
            raise HTTPException(409, "Gateway publication is in progress. Wait for it to finish.")
        try:
            updated = apply_change(
                copy.deepcopy(data), body, member, [m["name"] for m in config["models"]]
            )
            # Validate the resulting policy size and model references before persistence.
            impact(config, updated)
            storage().save(updated, etag)
            return {
                "revision": updated["revision"],
                "saved": True,
                "gateway_changed": impact(config, updated)["policy_hash"]
                != updated.get("published_hash"),
            }
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/import-preview")
    def import_preview(body: dict, access=actor_dependency):
        data, _, member = access
        if member["role"] not in {"owner", "admin"}:
            raise HTTPException(403, "Only owners and administrators can import members.")
        try:
            members = import_members(data, body)
            data["members"].extend(members)
            effect = impact(config, data)
            return {
                "count": len(members),
                "members": members[:20],
                "policy_bytes": effect["policy_bytes"],
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/impact")
    def preview(access=actor_dependency):
        data, _, member = access
        administrative(member)
        return impact(config, data)

    @app.post("/api/publish")
    def deploy(body: dict, access=actor_dependency):
        data, etag, member = access
        if member["role"] not in {"owner", "admin"}:
            raise HTTPException(403, "Only owners and administrators can publish gateway access.")
        if (
            body.get("revision") != data["revision"]
            or body.get("organization") != data["settings"]["name"]
        ):
            raise HTTPException(
                409, "Review the current revision and confirm the organization name."
            )
        if data["publish_status"] == "publishing":
            started = datetime.fromisoformat(data["publish_started_at"])
            if (datetime.now(UTC) - started).total_seconds() < 600:
                raise HTTPException(409, "A publication is already in progress.")
        reviewed = data["revision"]
        data.update(publish_status="publishing", publish_started_at=now())
        data["revision"] += 1
        audit(
            data,
            member,
            "gateway.publish_started",
            "Gateway",
            None,
            {"reviewed_revision": reviewed},
        )
        try:
            storage().save(data, etag)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        data, etag = storage().read()
        try:
            publish(config, data, azure)
        except Exception:
            data.update(publish_status="failed")
            data["revision"] += 1
            audit(
                data,
                member,
                "gateway.publish_failed",
                "Gateway",
                None,
                {"reviewed_revision": reviewed},
            )
            storage().save(data, etag)
            raise HTTPException(
                502,
                "Azure could not confirm publication. The draft is retained. An operator must inspect both gateway policies before retrying; automatic rollback may be incomplete.",
            ) from None
        data["revision"] += 1
        data.update(
            publish_status="published",
            published_revision=data["revision"],
            published_at=now(),
            published_hash=impact(config, data)["policy_hash"],
        )
        audit(data, member, "gateway.published", "Gateway", None, {"reviewed_revision": reviewed})
        storage().save(data, etag)
        return {"published": True, "revision": data["revision"]}

    @app.get("/api/telemetry")
    def usage(days: int = 30, access=actor_dependency):
        _, _, member = access
        administrative(member)
        if days not in {7, 30, 90}:
            raise HTTPException(422, "Choose 7, 30, or 90 days.")
        cached = app.state.telemetry_cache
        if cached and cached[0] == days and (datetime.now(UTC) - cached[1]).total_seconds() < 60:
            return cached[2]
        try:
            result = telemetry(os.getenv("PORTAL_INSIGHTS_APP_ID"), credential, config, days)
            app.state.telemetry_cache = (days, datetime.now(UTC), result)
            return result
        except Exception:
            raise HTTPException(
                502,
                "Usage could not be loaded from Application Insights. Check the portal identity's monitoring access, then retry.",
            ) from None

    @app.post("/api/cost-analysis")
    def costs(body: CostQuery, access=actor_dependency):
        data, _, member = access
        administrative(member)
        prices = data["prices"]
        key = body.model_dump_json() + json.dumps(prices, sort_keys=True)
        cached = app.state.cost_cache.get(key)
        if cached and (datetime.now(UTC) - cached[0]).total_seconds() < 60:
            return cached[1]
        try:
            result = cost_analysis(
                os.getenv("PORTAL_INSIGHTS_APP_ID"), credential, config, body, prices
            )
        except Exception:
            raise HTTPException(
                502,
                "Cost analysis could not be loaded from Application Insights. Retry with a shorter period or check monitoring access.",
            ) from None
        # Bound in-memory query caching; never cache auth decisions or failed queries.
        if len(app.state.cost_cache) >= 16:
            app.state.cost_cache.pop(next(iter(app.state.cost_cache)))
        app.state.cost_cache[key] = (datetime.now(UTC), result)
        return result

    @app.get("/api/client-profile")
    def client_profile(group_id: str | None = None, access=actor_dependency):
        data, _, member = access
        allowed = effective_models(data, member)
        group = next(g for g in data["groups"] if g["id"] == member["group_id"])
        if group_id:
            if member["role"] not in {"owner", "admin"}:
                raise HTTPException(403, "Only administrators can export another group's profile.")
            group = next((g for g in data["groups"] if g["id"] == group_id), None)
            if not group:
                raise HTTPException(404, "Group not found.")
            allowed = group["model_ids"]
        models = copy.deepcopy([m for m in config["models"] if m["name"] in allowed])
        if not models:
            raise HTTPException(
                409, "This group has no model access. Grant a model before exporting."
            )
        for family in {m["anthropicFamilyTier"] for m in models}:
            options = [m for m in models if m["anthropicFamilyTier"] == family]
            if any(m["name"] == group["default_model"] for m in options):
                for model in options:
                    model["isFamilyDefault"] = model["name"] == group["default_model"]
            if not any(m.get("isFamilyDefault") for m in options):
                options[0]["isFamilyDefault"] = True
        models.sort(key=lambda model: model["name"] != group["default_model"])
        desktop_client_id = os.getenv("PORTAL_DESKTOP_CLIENT_ID")
        if desktop_client_id:
            desktop_client_id = str(UUID(desktop_client_id))
        return JSONResponse(
            export_client(
                {**config, "models": models},
                auth_mode="msal" if desktop_client_id else "azure-cli",
                client_id=desktop_client_id,
            ),
            headers={"Content-Disposition": 'attachment; filename="client.json"'},
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    static = Path(os.getenv("PORTAL_STATIC_DIR", "portal/web/dist")).resolve()
    if static.is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}")
        def index(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return app
