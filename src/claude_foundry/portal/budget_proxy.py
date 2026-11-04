"""Fail-closed admission and streaming inference. Never retries a model dispatch."""

import asyncio
import hashlib
import json
import logging
from uuid import UUID

import httpx
import jwt
from anyio import CancelScope
from azure.core.exceptions import HttpResponseError
from fastapi import HTTPException
from starlette.responses import JSONResponse, StreamingResponse

from .budget_ledger import BudgetDenied, Contended, limits_for, policy_digest
from .budget_pricing import BudgetPrice, usage_tokens
from .budget_stream import UsageStream

LOG = logging.getLogger(__name__)
MAX_BODY = 8 * 1024 * 1024


# Rejections that stop before the model runs are not billed: throttling, sign-in,
# routing and invalid requests (bad tool schema, context too long). Their full
# reservation is released at once. Content-filter rejections may bill the prompt,
# and 5xx or interrupted outcomes may follow billable work, so those stay held.
UNBILLED_REJECTIONS = {400, 401, 403, 404, 413, 415, 422, 429}
BILLABLE_REJECTION_MARKERS = (b"content_filter", b"ResponsibleAIPolicyViolation")


def unbilled_rejection(status, body):
    return status in UNBILLED_REJECTIONS and not any(
        marker in body for marker in BILLABLE_REJECTION_MARKERS
    )


def enforce_review(policy):
    """Owners opt into refusing models whose rate review date has passed."""
    return bool(policy["settings"].get("enforce_price_review", False))


def settlement_message(error):
    """Only application-authored ledger messages are safe to copy into telemetry."""
    messages = {
        ValueError: {
            "Invalid settlement amount",
            "Unknown reservation",
            "Conflicting settlement; manual reconciliation required",
            "Ledger inconsistency; manual reconciliation required",
        },
        BudgetDenied: {"Usage exceeded its verified reservation; enforcement is paused"},
        Contended: {"Settlement remains reserved; retry reconciliation"},
    }
    if (
        len(error.args) == 1
        and type(error.args[0]) is str
        and error.args[0] in messages.get(type(error), set())
    ):
        return error.args[0]
    if (
        isinstance(error, HttpResponseError)
        and type(error.status_code) is int
        and 100 <= error.status_code <= 599
    ):
        return f"Ledger storage request failed (HTTP {error.status_code}); details redacted"
    if isinstance(error, TimeoutError):
        return "Ledger operation timed out; details redacted"
    return "Ledger write failed; details redacted"


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
    def __init__(
        self,
        ledger,
        snapshots,
        credential,
        endpoint,
        deployments,
        client=None,
        content_capture=None,
    ):
        if not endpoint.startswith("https://") or not endpoint.rstrip("/").endswith(
            ".services.ai.azure.com"
        ):
            raise ValueError("A fixed Azure Foundry services endpoint is required")
        self.ledger, self.snapshots, self.credential = ledger, snapshots, credential
        self.endpoint, self.deployments = endpoint.rstrip("/"), deployments
        self.content_capture = content_capture
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
        policy, member, _ = await self.authorize(principal)
        if member["role"] not in {"owner", "admin"}:
            raise HTTPException(
                403, "Only organization owners and administrators can inspect cache diagnostics"
            )
        probe = await asyncio.to_thread(self.snapshots.probe_shared, policy_digest(policy))
        return {"scope": "current_worker", **self.snapshots.cache_info(), "shared_probe": probe}

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
        price.prepare({**body, "max_tokens": 1}, enforce_review(policy))
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
        payload, maximum, output_bound = price.prepare(body, enforce_review(policy))
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
        hold_recorded = False

        async def held(reason, error=None):
            nonlocal hold_recorded
            if hold_recorded:
                return
            hold_recorded = True
            message = "Reservation held: request=%s reason=%s error=%s"
            args = [request_id, reason, type(error).__name__ if error is not None else "None"]
            if reason == "settlement_failed" and error is not None:
                message += " message=%s"
                args.append(settlement_message(error))
            LOG.warning(message, *args)
            # Starlette cancels its task group on disconnect. Finish the bounded
            # metadata attempt even inside that cancelled scope.
            with CancelScope(shield=True):
                try:
                    await asyncio.to_thread(self.ledger.note_hold, request_id, reason)
                except Exception:
                    pass

        capture_name = capture_record = None
        if self.content_capture:
            try:
                capture_name, capture_record = await asyncio.to_thread(
                    self.content_capture.begin,
                    request_id,
                    provider,
                    principal,
                    group["id"],
                    price.model,
                    payload,
                )
            except asyncio.CancelledError as error:
                await held("client_disconnected", error)
                raise
            except Exception as error:
                await held("dispatch_unknown", error)
                raise
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
        except asyncio.CancelledError as error:
            await held("client_disconnected", error)
            raise
        except Exception as error:
            await held("dispatch_unknown", error)
            raise HTTPException(
                502, "Inference outcome unknown; its budget reservation remains held"
            ) from None
        # These values originate only in the reviewed ledger policy, never client headers.
        public_headers = {
            "x-budget-request-id": request_id,
            "x-budget-group-id": group["id"],
            "Cache-Control": "no-store",
        }

        async def settle(usage_value):
            try:
                inputs, outputs = usage_tokens(provider, usage_value)
                # Violations never release money, even if another rate happens to mask it.
                amount = (
                    maximum + 1
                    if inputs > price.max_input_tokens or outputs > output_bound
                    else price.usage_cost(provider, usage_value)
                )
            except Exception as error:
                await held("usage_unreadable", error)
                raise
            try:
                await asyncio.to_thread(self.ledger.settle, request_id, amount)
            except Exception as error:
                await held("settlement_failed", error)
                raise

        if payload.get("stream") and response.is_success:
            usage = UsageStream(provider)

            def incomplete_stream_event():
                message = (
                    "The upstream model stream ended before a terminal event; "
                    "the budget reservation remains held for review."
                )
                value = (
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": message},
                    }
                    if provider == "anthropic"
                    else {
                        "type": "error",
                        "error": {
                            "code": "upstream_stream_incomplete",
                            "message": message,
                            "param": None,
                        },
                    }
                )
                return ("data: " + json.dumps(value, separators=(",", ":")) + "\n\n").encode()

            async def chunks():
                settled = False
                captured = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if self.content_capture and len(captured) < self.content_capture.max_bytes:
                            captured.extend(chunk[: self.content_capture.max_bytes - len(captured)])
                        usage.feed(chunk)
                        # Native clients can disconnect immediately after the terminal
                        # event. Commit its authoritative usage before exposing it.
                        if usage.complete and not settled:
                            await settle(usage.final())
                            settled = True
                        yield chunk
                    if not settled:
                        try:
                            await settle(usage.final())
                            settled = True
                        except Exception as error:
                            # Normalize every unsettled terminal path. Some compatible
                            # providers emit a failure-shaped event that a native client
                            # does not recognize; the canonical error prevents that event
                            # from degrading into a misleading transport disconnect.
                            await held("stream_incomplete", error)
                            yield incomplete_stream_event()
                except (asyncio.CancelledError, GeneratorExit) as error:
                    if not settled:
                        await held("client_disconnected", error)
                    raise
                except Exception as error:
                    # Upstream read errors and settlement failures keep the reservation,
                    # but clients still receive a terminal protocol error when possible.
                    if not settled:
                        await held("stream_incomplete", error)
                    yield incomplete_stream_event()
                finally:
                    if self.content_capture:
                        await asyncio.to_thread(
                            self.content_capture.finish,
                            capture_name,
                            capture_record,
                            bytes(captured),
                            response.status_code,
                            settled,
                        )
                    await response.aclose()

            return StreamingResponse(
                chunks(),
                status_code=response.status_code,
                media_type="text/event-stream",
                headers=public_headers,
            )
        settled = False
        try:
            # Non-streaming responses are bounded; caller cancellation conservatively
            # retains the reservation just like streaming cancellation.
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("Response exceeds the bounded JSON response size")
            if not response.is_success and unbilled_rejection(response.status_code, raw):
                try:
                    await asyncio.to_thread(self.ledger.settle, request_id, 0)
                    settled = True
                except Exception as error:
                    await held("settlement_failed", error)
            value = json.loads(raw)
            if response.is_success:
                try:
                    await settle(value.get("usage"))
                    settled = True
                except Exception as error:
                    await held("usage_unreadable", error)
            elif not settled:
                await held("rejected_possibly_billed")
            # Other 4xx/5xx responses can follow billable work. No automatic refund.
            if self.content_capture:
                await asyncio.to_thread(
                    self.content_capture.finish,
                    capture_name,
                    capture_record,
                    bytes(raw),
                    response.status_code,
                    response.is_success,
                )
            return JSONResponse(value, status_code=response.status_code, headers=public_headers)
        except asyncio.CancelledError as error:
            if not settled:
                await held("client_disconnected", error)
            raise
        except Exception as error:
            if not settled:
                await held(
                    "usage_unreadable" if response.is_success else "rejected_possibly_billed",
                    error,
                )
            raise
        finally:
            await response.aclose()
