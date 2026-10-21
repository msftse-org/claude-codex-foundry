"""Fail-closed admission and streaming inference. Never retries a model dispatch."""

import asyncio
import hashlib
import json
import logging
from uuid import UUID

import httpx
import jwt
from fastapi import HTTPException
from starlette.responses import JSONResponse, StreamingResponse

from .budget_ledger import BudgetDenied, limits_for
from .budget_pricing import BudgetPrice, usage_tokens
from .budget_stream import UsageStream

LOG = logging.getLogger(__name__)
MAX_BODY = 8 * 1024 * 1024


class GatewayIdentity:
    """Validate an app-only MI token for this service, from this APIM identity only."""

    def __init__(self, tenant, audience, gateway_oid):
        self.tenant = str(UUID(tenant))
        self.audience = audience
        self.gateway_oid = str(UUID(gateway_oid))
        self.keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant}/discovery/v2.0/keys"
        )

    def validate(self, authorization):
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or len(authorization) > 32768
        ):
            raise HTTPException(401, "Gateway authentication required")
        token = authorization[7:]
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=[self.audience, self.audience.removeprefix("api://")],
                options={"require": ["exp", "iat", "nbf", "oid", "tid", "aud", "iss"]},
            )
            issuer = (
                f"https://sts.windows.net/{self.tenant}/"
                if claims.get("ver") == "1.0"
                else f"https://login.microsoftonline.com/{self.tenant}/v2.0"
            )
            if (
                claims["iss"] != issuer
                or claims["tid"] != self.tenant
                or claims["oid"] != self.gateway_oid
                or claims.get("scp")
            ):
                raise ValueError("Wrong caller")
        except (jwt.PyJWTError, ValueError, KeyError, TypeError):
            raise HTTPException(401, "Gateway authentication failed") from None


class BudgetProxy:
    def __init__(self, ledger, snapshots, credential, endpoint, deployments, client=None):
        if not endpoint.startswith("https://") or not endpoint.rstrip("/").endswith(
            ".services.ai.azure.com"
        ):
            raise ValueError("A fixed Azure Foundry services endpoint is required")
        self.ledger, self.snapshots, self.credential = ledger, snapshots, credential
        self.endpoint, self.deployments = endpoint.rstrip("/"), deployments
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(240, connect=15), follow_redirects=False
        )

    async def authorize(self, principal):
        principal = str(UUID(principal))
        digest = await asyncio.to_thread(self.ledger.active)
        policy = await asyncio.to_thread(self.snapshots.get, digest)
        member, group, _ = limits_for(policy, principal)
        return policy, member, group

    async def cache_status(self, principal):
        _, member, _ = await self.authorize(principal)
        if member["role"] not in {"owner", "admin"}:
            raise HTTPException(
                403, "Only organization owners and administrators can inspect cache diagnostics"
            )
        return {"scope": "current_worker", **self.snapshots.cache_info()}

    async def count_tokens(self, principal, body):
        """Anthropic documents token counting as free; never treat this as a cost bound."""
        policy, member, group = await self.authorize(principal)
        if not isinstance(body, dict) or body.get("model") not in set(
            group["model_ids"] + member.get("extra_models", [])
        ):
            raise BudgetDenied("Model access is required for token counting")
        values = [
            p
            for p in policy["budget_prices"]
            if p["model"] == body.get("model") and p["provider"] == "anthropic"
        ]
        if len(values) != 1:
            raise BudgetDenied("Verified model pricing is required")
        price = BudgetPrice.model_validate(values[0])
        price.prepare({**body, "max_tokens": 1})
        allowed = {"model", "messages", "tools", "system", "thinking", "tool_choice"}
        if set(body) - allowed:
            raise ValueError("Unsupported token-counting fields")
        token = await asyncio.to_thread(
            self.credential.get_token, "https://cognitiveservices.azure.com/.default"
        )
        response = await self.client.post(
            self.endpoint + "/anthropic/v1/messages/count_tokens",
            json=body,
            headers={"Authorization": "Bearer " + token.token, "anthropic-version": "2023-06-01"},
        )
        value = response.json()
        return JSONResponse(
            value, status_code=response.status_code, headers={"Cache-Control": "no-store"}
        )

    async def infer(self, provider, principal, request_id, body):
        if provider not in {"anthropic", "openai"}:
            raise ValueError("Unsupported inference protocol")
        request_id, principal = str(UUID(request_id)), str(UUID(principal))
        policy, _, group = await self.authorize(principal)
        if not isinstance(body, dict):
            raise ValueError("A JSON object is required")
        prices = [
            p
            for p in policy["budget_prices"]
            if p["model"] == body.get("model") and p["provider"] == provider
        ]
        if len(prices) != 1:
            raise BudgetDenied("No verified price is configured for this model/protocol")
        price = BudgetPrice.model_validate(prices[0])
        deployment = self.deployments.get(price.model, {})
        if (
            deployment.get("version") != price.deployment_version
            or deployment.get("provider") != provider
            or deployment.get("max_input_tokens") != price.max_input_tokens
            or deployment.get("max_output_tokens") != price.max_output_tokens
        ):
            raise BudgetDenied(
                "The deployment's pinned capacity/version does not match reviewed pricing"
            )
        payload, maximum, output_bound = price.prepare(body)
        if provider == "openai" and deployment.get("supports_reasoning") is False:
            # Mirrors the qualified baseline profile for non-reasoning models.
            # This capability is server-owned, never supplied by the client.
            payload.pop("reasoning", None)
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        # Token acquisition is before reservation; a token failure cannot strand funds.
        token = await asyncio.to_thread(
            self.credential.get_token, "https://cognitiveservices.azure.com/.default"
        )
        await asyncio.to_thread(
            self.ledger.reserve, request_id, principal, price.model, maximum, policy, fingerprint
        )
        path = "/anthropic/v1/messages" if provider == "anthropic" else "/openai/v1/responses"
        headers = {"Authorization": "Bearer " + token.token, "Content-Type": "application/json"}
        if provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if "context_management" in payload:
                headers["anthropic-beta"] = "context-management-2025-06-27"
        try:
            response = await self.client.send(
                self.client.build_request(
                    "POST", self.endpoint + path, json=payload, headers=headers
                ),
                stream=True,
            )
        except Exception:
            LOG.warning("Inference outcome unknown; reservation held: %s", request_id)
            raise HTTPException(
                502, "Inference outcome unknown; its budget reservation remains held"
            ) from None
        # These values originate only in the reviewed ledger policy, never client headers.
        public_headers = {
            "x-budget-request-id": request_id,
            "x-budget-group-id": group["id"],
            "Cache-Control": "no-store",
        }

        async def settle(tokens):
            inputs, outputs = tokens
            # Violations never release money, even if another rate happens to mask it.
            if inputs > price.max_input_tokens or outputs > output_bound:
                await asyncio.to_thread(self.ledger.settle, request_id, maximum + 1)
                return
            await asyncio.to_thread(self.ledger.settle, request_id, price.cost(inputs, outputs))

        if payload.get("stream") and response.is_success:
            usage = UsageStream(provider)

            async def chunks():
                try:
                    async for chunk in response.aiter_bytes():
                        usage.feed(chunk)
                        yield chunk
                    await settle(usage.tokens())
                except BaseException:
                    # Cancellation, absent usage, parse errors, and settlement failures
                    # all keep the reservation. Never log prompts, output, or tokens.
                    LOG.warning(
                        "Stream did not confirm settlement; check reservation: %s", request_id
                    )
                    raise
                finally:
                    await response.aclose()

            return StreamingResponse(
                chunks(),
                status_code=response.status_code,
                media_type="text/event-stream",
                headers=public_headers,
            )
        try:
            # Non-streaming responses are bounded; caller cancellation conservatively
            # retains the reservation just like streaming cancellation.
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("Response exceeds the bounded JSON response size")
            value = json.loads(raw)
            if response.is_success:
                try:
                    await settle(usage_tokens(provider, value.get("usage")))
                except Exception:
                    LOG.warning("Response usage not settled; check reservation: %s", request_id)
            # Even a 4xx/5xx can follow billable work. No automatic refund.
            return JSONResponse(value, status_code=response.status_code, headers=public_headers)
        finally:
            await response.aclose()
