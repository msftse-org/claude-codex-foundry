"""Bound request bodies before parsing and apply browser protections to rejections too."""

from urllib.parse import urlsplit

from starlette.datastructures import URL, Headers, MutableHeaders
from starlette.responses import JSONResponse

MAX_BODY_BYTES = 1_000_000


class RequestBoundary:
    def __init__(self, app, local=False):
        self.app, self.local = app, local

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Frame-Options"] = "DENY"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; font-src 'self'; connect-src 'self' https://login.microsoftonline.com; "
                    "frame-src https://login.microsoftonline.com; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
                )
                if scope["path"].startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
                if not self.local:
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, secure_send)

        headers = Headers(scope=scope)
        if self.local:
            if not scope.get("client") or scope["client"][0] not in {
                "127.0.0.1",
                "::1",
                "testclient",
            }:
                return await reject(403, "Local administrator mode accepts loopback requests only.")
            if (
                URL(scope=scope).hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}
                or (
                    headers.get("origin")
                    and urlsplit(headers["origin"]).hostname
                    not in {"localhost", "127.0.0.1", "::1"}
                )
                or headers.get("sec-fetch-site") == "cross-site"
            ):
                return await reject(403, "Use a loopback origin for local preview.")
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            return await reject(400, "Invalid content length.")
        if scope["method"] in {"POST", "PUT", "PATCH"} and "content-length" not in headers:
            return await reject(411, "A content length is required.")
        if declared < 0 or declared > MAX_BODY_BYTES:
            return await reject(413, "Request too large.")

        # Content-Length is untrusted. Bound actual chunks before JSON validation.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                return await reject(413, "Request too large.")
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        if len(body) != declared:
            return await reject(400, "Content length does not match the request body.")
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, secure_send)
