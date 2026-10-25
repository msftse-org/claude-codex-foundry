"""Authenticated administration API and static portal entry point."""

import copy
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from azure.identity import AzureCliCredential, ManagedIdentityCredential
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..client_bundle import build_bundle, build_template
from ..config import export_client, load_org
from ..unified import catalog, provider_configs, reporting_config
from .auth import Authenticator
from .boundary import RequestBoundary
from .budget_ledger import AzureRows, BudgetDenied, Contended, Ledger
from .budget_policy import Snapshots, draft_impact, group_publication, portal_seed, snapshot
from .budget_proxy import GatewayIdentity
from .content_logs import LogQuery, LogRead, LogSource, list_logs, read_log
from .costs import CostQuery, cost_analysis, planning_prices
from .domain import Change, apply_change, audit, effective_models, import_members, now, seed
from .governance import impact
from .policy_cache import MAX_SHARED_BYTES
from .service import ManagedAzure, publish, telemetry
from .store import BlobStore, Conflict, LocalStore


def bundle_template_path():
    # Azure's worker may run outside wwwroot. Resolve beside the deployed package.
    return Path(
        os.getenv(
            "PORTAL_CLIENT_BUNDLE_TEMPLATE",
            str(Path(__file__).resolve().parents[2] / "client-bundle.zip"),
        )
    )


def create_app(config=None, store=None, credential=None, local=None, budget=None):
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
    app = FastAPI(title="Claude & Codex Admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.telemetry_cache = None
    app.state.cost_cache = {}
    budget_account = os.getenv("BUDGET_STORAGE_ACCOUNT")
    if budget is None and budget_account:
        budget = (
            Ledger(AzureRows(budget_account, os.environ["BUDGET_ORGANIZATION_ID"], credential)),
            Snapshots(budget_account, credential),
        )
    policy_impact = draft_impact if budget is not None else impact
    budget_models = json.loads(os.getenv("PORTAL_BUDGET_MODELS_JSON", "[]"))
    providers = provider_configs(
        config, json.loads(os.getenv("PORTAL_PROVIDER_CONFIGS_JSON", "{}"))
    )
    if providers and budget is None:
        raise ValueError("Shared provider administration requires the budget runtime")
    reporting = reporting_config(config, providers) if providers else config
    # Separate machine-only origin for APIM's immutable snapshot cache. This
    # portal always reads Blob directly on a miss, preventing cache recursion.
    origin_audience = os.getenv("BUDGET_POLICY_CACHE_AUDIENCE", "")
    origin_gateway = os.getenv("BUDGET_POLICY_CACHE_APIM_PRINCIPAL_ID", "")
    cache_origin = (
        GatewayIdentity(config["tenant_id"], origin_audience, origin_gateway)
        if origin_audience and origin_gateway
        else None
    )
    model_catalog = config["models"] + [
        m for m in budget_models if m["name"] not in {m["name"] for m in config["models"]}
    ]
    if providers:
        model_catalog = catalog(providers)
    content_source = (
        LogSource.model_validate_json(os.environ["PORTAL_CONTENT_LOGS_JSON"])
        if os.getenv("PORTAL_CONTENT_LOGS_JSON")
        else None
    )

    def storage():
        if app.state.store is None:
            initial = seed(
                {**config, "models": model_catalog},
                owner_id,
                os.getenv("PORTAL_OWNER_NAME", "Organization owner"),
                os.getenv("PORTAL_OWNER_EMAIL", "owner@example.com"),
            )
            initial["budget_prices"] = json.loads(
                os.getenv("PORTAL_INITIAL_BUDGET_PRICES_JSON", "[]")
            )
            app.state.store = (
                LocalStore(os.getenv("PORTAL_LOCAL_PATH", ".local/portal/state.json"), initial)
                if local
                else BlobStore(
                    os.environ["PORTAL_STORAGE_ACCOUNT"],
                    credential,
                    lambda: portal_seed(initial, *budget, owner_id) if budget else initial,
                )
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
        if member["role"] in {"developer", "group_admin"}:
            raise HTTPException(403, "An organization administration role is required.")

    actor_dependency = Depends(actor)

    def content_access(member):
        if member["role"] not in {"owner", "admin", "group_admin"}:
            raise HTTPException(
                403, "Only portal owners and administrators can access content logs."
            )

    def still_authorized(member):
        latest, _ = storage().read()
        active = next(
            (m for m in latest["members"] if m["id"] == member["id"] and m["status"] == "active"),
            None,
        )
        if not active:
            raise HTTPException(403, "Your portal access has changed.")
        content_access(active)
        if active["role"] != member["role"] or active["group_id"] != member["group_id"]:
            raise HTTPException(403, "Your portal scope has changed.")

    @app.get("/api/content-logs/config")
    def content_config(access=actor_dependency):
        _, _, member = access
        content_access(member)
        return {
            "configured": content_source is not None,
            "capture_enabled": bool(content_source and content_source.capture_enabled),
            "api_ids": content_source.api_ids if content_source else [],
            "retention_days": content_source.retention_days if content_source else 7,
            "max_message_bytes": content_source.max_message_bytes if content_source else 262144,
        }

    @app.post("/api/content-logs/search")
    def content_search(body: LogQuery, access=actor_dependency):
        _, _, member = access
        content_access(member)
        if not content_source:
            raise HTTPException(409, "Content logging has not been configured for this portal.")
        if body.api_id and body.api_id not in content_source.api_ids:
            raise HTTPException(422, "Choose a configured API.")
        try:
            result = list_logs(
                content_source,
                credential,
                body,
                **({"group_id": member["group_id"]} if member["role"] == "group_admin" else {}),
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Content log search failed (%s).", type(exc).__name__
            )
            raise HTTPException(
                502,
                "Content logs could not be loaded. Check log ingestion and the portal identity's workspace access.",
            ) from None
        still_authorized(member)
        return result

    @app.post("/api/content-logs/read")
    def content_read(body: LogRead, access=actor_dependency):
        data, etag, member = access
        content_access(member)
        if not content_source:
            raise HTTPException(409, "Content logging has not been configured for this portal.")
        if data["publish_status"] == "publishing":
            raise HTTPException(409, "Wait for gateway publication to finish, then retry.")
        # Persist the access intent BEFORE querying content. An audit failure denies the read.
        audit(
            data,
            member,
            "content.view_requested",
            str(body.correlation_id),
            None,
            {"reason": body.reason},
            group_id=member["group_id"] if member["role"] == "group_admin" else None,
        )
        data["revision"] += 1
        try:
            storage().save(data, etag)
        except Conflict:
            raise HTTPException(
                409, "Portal state changed. Reload before opening content."
            ) from None
        except Exception:
            raise HTTPException(
                503, "The access audit could not be saved. Content was not opened."
            ) from None
        try:
            result = read_log(
                content_source,
                credential,
                body.correlation_id,
                **({"group_id": member["group_id"]} if member["role"] == "group_admin" else {}),
            )
        except Exception as exc:
            logging.getLogger(__name__).warning("Content log read failed (%s).", type(exc).__name__)
            raise HTTPException(
                502, "Captured content could not be loaded. Retry after checking log ingestion."
            ) from None
        still_authorized(member)
        if result is None:
            raise HTTPException(
                404, "No captured content was found within the permitted APIs and retention window."
            )
        return result

    @app.get("/api/bootstrap")
    def bootstrap():
        return {
            "tenantId": config["tenant_id"],
            "clientId": client_id,
            "local": local,
            "configured": bool(client_id) or local,
            "name": "Claude & Codex Admin",
        }

    @app.get("/api/internal/budget-policies/{digest}")
    def policy_cache_origin(digest: str, request: Request):
        if cache_origin is None or budget is None:
            raise HTTPException(404, "Policy cache origin is not configured")
        cache_origin.validate(request.headers.get("authorization"))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(400, "Invalid policy digest")
        try:
            value = budget[1].get(digest)
        except Exception:
            raise HTTPException(503, "The durable policy source is unavailable") from None
        response = JSONResponse(value, headers={"Cache-Control": "no-store"})
        if len(response.body) > MAX_SHARED_BYTES:
            raise HTTPException(413, "Policy is too large for the shared cache")
        return response

    @app.get("/api/state")
    def state(access=actor_dependency):
        data, _, member = access
        data.update(
            viewer=member,
            models=model_catalog,
            budget_configured=budget is not None,
            client_types=list(providers) if providers else [config.get("client_type", "claude")],
            gateway_url=config["gateway_url"],
            tenant_id=config["tenant_id"],
            policy_hash=policy_impact(config, data)["policy_hash"],
        )
        if budget is not None:
            data["prices"] = planning_prices(data)
        if member["role"] == "developer":
            data.update(
                members=[member],
                groups=[g for g in data["groups"] if g["id"] == member["group_id"]],
                audit=[],
                prices=[],
                budget_prices=[],
                settings={"name": data["settings"]["name"]},
                requests=[r for r in data["requests"] if r["member_id"] == member["id"]],
            )
        if member["role"] == "group_admin":
            group_id = member["group_id"]
            members = [m for m in data["members"] if m["group_id"] == group_id]
            identifiers = {m["id"] for m in members}
            data.update(
                members=members,
                groups=[g for g in data["groups"] if g["id"] == group_id],
                audit=[a for a in data["audit"] if a.get("group_id") == group_id],
                settings={"name": data["settings"]["name"]},
                budget_prices=[],
                requests=[r for r in data["requests"] if r["member_id"] in identifiers],
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
                copy.deepcopy(data), body, member, [m["name"] for m in model_catalog]
            )
            # Validate the resulting policy size and model references before persistence.
            policy_impact(config, updated)
            storage().save(updated, etag)
            return {
                "revision": updated["revision"],
                "saved": True,
                "gateway_changed": policy_impact(config, updated)["policy_hash"]
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
        if member["role"] not in {"owner", "admin", "group_admin"}:
            raise HTTPException(403, "Only owners and administrators can import members.")
        try:
            members = import_members(data, body, member)
            data["members"].extend(members)
            effect = policy_impact(config, data)
            return {
                "count": len(members),
                "members": members[:20],
                "policy_bytes": effect["policy_bytes"],
            }
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/impact")
    def preview(access=actor_dependency):
        data, _, member = access
        administrative(member)
        return policy_impact(config, data)

    @app.post("/api/publish")
    def deploy(body: dict, access=actor_dependency):
        data, etag, member = access
        if member["role"] not in {"owner", "admin"}:
            raise HTTPException(403, "Only owners and administrators can publish gateway access.")
        if budget is not None:
            raise HTTPException(
                409, "Use budget policy publication. Legacy gateway publication is disabled here."
            )
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
            published_hash=policy_impact(config, data)["policy_hash"],
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
            result = telemetry(os.getenv("PORTAL_INSIGHTS_APP_ID"), credential, reporting, days)
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
        if member["role"] == "group_admin":
            body = body.model_copy(update={"group_ids": [member["group_id"]]})
        else:
            administrative(member)
        prices = planning_prices(data) if budget is not None else data["prices"]
        key = body.model_dump_json() + json.dumps(prices, sort_keys=True)
        cached = app.state.cost_cache.get(key)
        if cached and (datetime.now(UTC) - cached[0]).total_seconds() < 60:
            return cached[1]
        try:
            result = cost_analysis(
                os.getenv("PORTAL_INSIGHTS_APP_ID"), credential, reporting, body, prices
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

    def client_config(group_id, access, client="claude"):
        data, _, member = access
        profile_actor = member
        profile_config = config
        if providers:
            if client not in providers:
                raise HTTPException(409, "This provider is not configured for the portal")
            profile_config = providers[client]
        elif client != config.get("client_type", "claude"):
            raise HTTPException(409, "This provider is not configured for the portal")
        if budget is not None:
            url = (
                profile_config["gateway_url"]
                if providers
                else os.getenv("BUDGET_CLAUDE_GATEWAY_URL")
            )
            if not url:
                raise HTTPException(
                    409,
                    "Your organization's setup is not ready yet. "
                    "Ask your administrator to finish connecting the portal, then retry.",
                )
            try:
                data = budget[1].get(budget[0].active())
                member = next(
                    m
                    for m in data["members"]
                    if m["id"] == member["id"] and m["status"] == "active"
                )
            except Exception:
                raise HTTPException(
                    409, "Publish this member's budget access before exporting a profile"
                ) from None
            if member["group_id"] != profile_actor["group_id"]:
                raise HTTPException(409, "Publish the membership change before exporting a profile")
            profile_config = {**profile_config, "gateway_url": url}
        allowed = effective_models(data, member)
        group = next(g for g in data["groups"] if g["id"] == member["group_id"])
        if group_id:
            if profile_actor["role"] not in {"owner", "admin"} and not (
                profile_actor["role"] == "group_admin" and group_id == profile_actor["group_id"]
            ):
                raise HTTPException(403, "Only administrators can export another group's profile.")
            group = next((g for g in data["groups"] if g["id"] == group_id), None)
            if not group:
                raise HTTPException(404, "Group not found.")
            allowed = group["model_ids"]
        models = copy.deepcopy([m for m in profile_config["models"] if m["name"] in allowed])
        if not models:
            raise HTTPException(
                409, "This group has no model access. Grant a model before exporting."
            )
        for family in {m["anthropicFamilyTier"] for m in models} if client == "claude" else set():
            options = [m for m in models if m["anthropicFamilyTier"] == family]
            if any(m["name"] == group["default_model"] for m in options):
                for model in options:
                    model["isFamilyDefault"] = model["name"] == group["default_model"]
            if not any(m.get("isFamilyDefault") for m in options):
                options[0]["isFamilyDefault"] = True
        models.sort(key=lambda model: model["name"] != group["default_model"])
        if client == "codex":
            names = {m["name"] for m in models}
            profile_config = {
                **profile_config,
                "default_model": group["default_model"]
                if group["default_model"] in names
                else next(
                    (m["name"] for m in models if m["name"] == profile_config.get("default_model")),
                    models[0]["name"],
                ),
            }
        desktop_client_id = os.getenv("PORTAL_DESKTOP_CLIENT_ID")
        if desktop_client_id:
            desktop_client_id = str(UUID(desktop_client_id))
        return export_client(
            {**profile_config, "models": models},
            auth_mode="msal" if desktop_client_id else "azure-cli",
            client_id=desktop_client_id,
        )

    @app.get("/api/client-profile")
    def client_profile(
        group_id: str | None = None,
        client: Literal["claude", "codex"] = "claude",
        access=actor_dependency,
    ):
        return JSONResponse(
            client_config(group_id, access, client),
            headers={"Content-Disposition": f'attachment; filename="{client}-client.json"'},
        )

    @app.get("/api/client-bundle")
    def client_bundle(
        group_id: str | None = None,
        client: Literal["claude", "codex"] = "claude",
        access=actor_dependency,
    ):
        profile = client_config(group_id, access, client)
        template_path = bundle_template_path()
        try:
            template = (
                build_template(Path(__file__).resolve().parents[3])
                if local and not template_path.exists()
                else template_path.read_bytes()
            )
        except OSError:
            raise HTTPException(
                503,
                "The setup bundle is unavailable. Ask your administrator to redeploy the portal package.",
            ) from None
        return Response(
            build_bundle(profile, template, governed=budget is None, budgeted=budget is not None),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="claude-foundry-macos.zip"'},
        )

    @app.get("/api/model-policy")
    def model_policy(access=actor_dependency):
        data, _, member = access
        if member["role"] not in {"owner", "admin"}:
            raise HTTPException(403, "Only organization administrators can export the model policy")
        return JSONResponse(
            {"budget_prices": data.get("budget_prices", [])},
            headers={
                "Content-Disposition": 'attachment; filename="model-policy.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/budgets/balance")
    def budget_balance(member_id: str | None = None, access=actor_dependency):
        data, _, member = access
        target = next((m for m in data["members"] if m["id"] == (member_id or member["id"])), None)
        if not target or (
            target["id"] != member["id"]
            and member["role"] not in {"owner", "admin", "billing", "auditor"}
            and not (member["role"] == "group_admin" and target["group_id"] == member["group_id"])
        ):
            raise HTTPException(403, "This member is outside your budget scope")
        if budget is None:
            return {"configured": False, "active": False, "scopes": []}
        ledger, snapshots = budget
        try:
            digest = ledger.active()
            policy = snapshots.get(digest)
            # A member who moved groups cannot expose their previous team's totals.
            published_member = next((m for m in policy["members"] if m["id"] == target["id"]), None)
            if not published_member or published_member["group_id"] != target["group_id"]:
                raise BudgetDenied("Membership changes await budget publication")
            result = ledger.balances(policy, target["id"])
            if member["role"] == "developer":
                result["scopes"] = [s for s in result["scopes"] if s["scope"].startswith("user-")]
            elif member["role"] == "group_admin":
                result["scopes"] = [s for s in result["scopes"] if s["scope"] != "org"]
            return {
                **result,
                "configured": True,
                "active": True,
                "published_hash": digest,
                "draft_hash": None,
            }
        except (BudgetDenied, ValueError) as error:
            return {"configured": True, "active": False, "scopes": [], "note": str(error)}
        except Exception:
            raise HTTPException(
                503, "The budget ledger is unavailable; inference admission fails closed"
            ) from None

    @app.get("/api/budgets/reservations")
    def budget_reservations(access=actor_dependency):
        _, _, member = access
        if budget is None:
            return {"items": [], "has_more": False}
        try:
            rows = budget[0].rows.pending(
                group_id=member["group_id"] if member["role"] == "group_admin" else None,
                principal=member["id"] if member["role"] == "developer" else None,
            )
            fields = {
                "request_id",
                "principal_id",
                "group_id",
                "model",
                "month",
                "at",
                "reserved",
                "state",
            }
            return {
                "items": [{k: v for k, v in row.items() if k in fields} for row in rows[:50]],
                "has_more": len(rows) > 50,
            }
        except Exception:
            raise HTTPException(503, "Outstanding reservations could not be loaded") from None

    @app.post("/api/budgets/close-reservation")
    def close_reservation(body: dict, access=actor_dependency):
        data, etag, member = access
        if member["role"] != "owner":
            raise HTTPException(403, "Only the owner can close an uncertain reservation")
        if budget is None:
            raise HTTPException(409, "The budget ledger is not configured")
        try:
            request_id = str(UUID(body.get("request_id", "")))
            reason = body.get("reason")
            if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 300:
                raise ValueError("Provide a reason of 5–300 characters")
            reservation, _ = budget[0].rows.read("request-" + request_id)
            if not reservation:
                raise ValueError("Reservation not found")
            if reservation["state"] != "reserved":
                raise ValueError("The reservation has already been settled")
            audit(
                data,
                member,
                "budget.close_at_maximum_requested",
                request_id,
                None,
                {"reason": reason, "charge_micro_usd": reservation["reserved"]},
                reservation["group_id"],
            )
            data["revision"] += 1
            storage().save(data, etag)
            # This is a conservative charge, never an arbitrary administrative refund.
            budget[0].settle(request_id, reservation["reserved"])
            return {"closed": True, "charged_micro_usd": reservation["reserved"]}
        except (ValueError, BudgetDenied) as error:
            raise HTTPException(422, str(error)) from error
        except (Conflict, Contended) as error:
            raise HTTPException(409, str(error)) from error
        except Exception:
            raise HTTPException(
                503, "Closure could not be confirmed; inspect the reservation before retrying"
            ) from None

    @app.post("/api/budgets/publish")
    def publish_budgets(body: dict, access=actor_dependency):
        data, etag, member = access
        if member["role"] not in {"owner", "group_admin"}:
            raise HTTPException(403, "An owner or delegated group administrator is required")
        if budget is None:
            raise HTTPException(409, "Deploy and validate the budget gateway before publishing")
        if data["publish_status"] == "publishing":
            raise HTTPException(409, "Wait for the current gateway publication to finish")
        if (
            body.get("revision") != data["revision"]
            or body.get("organization") != data["settings"]["name"]
        ):
            raise HTTPException(409, "Review the current revision and organization name")
        ledger, snapshots = budget
        try:
            expected = body.get("published_hash")
            if member["role"] == "group_admin":
                expected = ledger.active()
                policy = group_publication(snapshots.get(expected), data, member)
            else:
                policy = snapshot(data)
            digest = snapshots.put(policy)
            # Save the audit before activation; never publish when the audit cannot persist.
            audit(
                data,
                member,
                "budget.publish_requested",
                digest,
                None,
                {"reviewed_revision": body["revision"]},
                member["group_id"] if member["role"] == "group_admin" else None,
            )
            data["revision"] += 1
            storage().save(data, etag)
            ledger.activate(policy, expected=expected)
            return {
                "published": True,
                "policy_hash": digest,
                "note": "Applies only to configured budget gateway routes; other endpoints are unchanged",
            }
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except (Conflict, Contended) as error:
            raise HTTPException(409, str(error)) from error
        except (ValueError, BudgetDenied) as error:
            raise HTTPException(422, str(error)) from error
        except Exception:
            raise HTTPException(
                503, "Activation could not be confirmed. Read the active policy before retrying"
            ) from None

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "setup_ready": budget is None
            or bool(providers)
            or bool(os.getenv("BUDGET_CLAUDE_GATEWAY_URL")),
            "client_types": list(providers) or [config.get("client_type", "claude")],
            "bundle_ready": local or bundle_template_path().is_file(),
        }

    static = Path(os.getenv("PORTAL_STATIC_DIR", "portal/web/dist")).resolve()
    if static.is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}")
        def index(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return app
