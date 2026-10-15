"""Dedicated Functions HTTP-streams host; never run through AsgiFunctionApp."""

import asyncio
import json
import os

import azure.functions as func
from azure.identity import ManagedIdentityCredential
from azurefunctions.extensions.http.fastapi import Request, StreamingResponse
from fastapi import HTTPException
from starlette.responses import JSONResponse

from claude_foundry.portal.budget_ledger import AzureRows, BudgetDenied, Ledger
from claude_foundry.portal.budget_policy import Snapshots, snapshot
from claude_foundry.portal.budget_proxy import MAX_BODY, BudgetProxy, GatewayIdentity

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
identity = GatewayIdentity(
    os.environ["BUDGET_TENANT_ID"],
    os.environ["BUDGET_AUDIENCE"],
    os.environ["BUDGET_APIM_PRINCIPAL_ID"],
)
account = os.environ["BUDGET_STORAGE_ACCOUNT"]
proxy = BudgetProxy(
    Ledger(AzureRows(account, os.environ["BUDGET_ORGANIZATION_ID"], credential)),
    Snapshots(account, credential),
    credential,
    os.environ["BUDGET_FOUNDRY_ENDPOINT"],
    json.loads(os.environ["BUDGET_DEPLOYMENTS_JSON"]),
)


@app.route(route="{provider}/v1/{operation}", methods=["GET", "POST"])
async def inference(req: Request) -> StreamingResponse:
    try:
        await asyncio.to_thread(identity.validate, req.headers.get("authorization"))
        bootstrap = os.getenv("BUDGET_BOOTSTRAP_JSON")
        if bootstrap:
            # Server-side one-time seed for private storage; an existing ledger is
            # never reset or overwritten, even if an operator leaves this setting.
            meta, _ = await asyncio.to_thread(proxy.ledger.rows.read, "policy")
            if not meta:
                reviewed = snapshot(json.loads(bootstrap))
                await asyncio.to_thread(proxy.snapshots.put, reviewed)
                await asyncio.to_thread(proxy.ledger.activate, reviewed, expected="")
        provider, operation = req.path_params["provider"], req.path_params["operation"]
        principal = req.headers.get("x-budget-principal-id", "")
        request_id = req.headers.get("x-budget-request-id", "")
        if req.method == "GET" and operation == "budget":
            policy, member, group = await proxy.authorize(principal)
            result = await asyncio.to_thread(proxy.ledger.balances, policy, principal)
            if member["role"] not in {"owner", "admin"}:
                result["scopes"] = [
                    scope for scope in result["scopes"] if scope["scope"] == "user-" + principal
                ]
            pending = await asyncio.to_thread(proxy.ledger.rows.pending, principal=principal)
            result["pending"] = [
                {k: r[k] for k in ["request_id", "model", "at", "reserved"]} for r in pending[:50]
            ]
            result["has_more_pending"] = len(pending) > 50
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        if req.method == "GET" and operation == "models":
            policy, member, group = await proxy.authorize(principal)
            allowed = set(group["model_ids"] + member.get("extra_models", []))
            data = [
                {
                    "id": p["model"],
                    "object": "model",
                    "created": 0,
                    "owned_by": "azure",
                    "type": "model",
                    "display_name": p["model"],
                    "created_at": "2026-01-01T00:00:00Z",
                }
                for p in policy["budget_prices"]
                if p["provider"] == provider and p["model"] in allowed
            ]
            return JSONResponse(
                {
                    "object": "list",
                    "data": data,
                    "has_more": False,
                    "first_id": data[0]["id"] if data else None,
                    "last_id": data[-1]["id"] if data else None,
                }
            )
        if req.method != "POST" or (provider, operation) not in {
            ("anthropic", "messages"),
            ("openai", "responses"),
            ("anthropic", "count-tokens"),
        }:
            raise HTTPException(404, "Unsupported operation")
        raw = bytearray()
        async for chunk in req.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise HTTPException(413, "Request exceeds 8 MiB")
        if operation == "count-tokens":
            return await proxy.count_tokens(principal, json.loads(raw))
        return await proxy.infer(provider, principal, request_id, json.loads(raw))
    except BudgetDenied as error:
        return JSONResponse(
            {"type": "error", "error": {"type": "budget_denied", "message": str(error)}},
            status_code=403,
        )
    except HTTPException as error:
        return JSONResponse({"error": {"message": error.detail}}, status_code=error.status_code)
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": {"message": "Invalid request or expired model verification"}}, status_code=422
        )
    except Exception:
        return JSONResponse(
            {"error": {"message": "Budget admission unavailable; no automatic retry or refund"}},
            status_code=503,
        )
