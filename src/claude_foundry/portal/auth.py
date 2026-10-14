"""Single-tenant access-token validation. Browser claims never authorize changes."""

from uuid import UUID

import jwt
from fastapi import HTTPException


class Authenticator:
    def __init__(self, tenant, client_id):
        self.tenant, self.client_id = str(UUID(tenant)), str(UUID(client_id)) if client_id else ""
        self.issuer = f"https://login.microsoftonline.com/{self.tenant}/v2.0"
        self.keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant}/discovery/v2.0/keys",
            cache_jwk_set=True,
        )

    def identity(self, authorization):
        if not self.client_id:
            raise HTTPException(503, "The Entra application registration has not been configured.")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Sign in with your organization account.")
        token = authorization[7:]
        if len(token) > 32768:
            raise HTTPException(401, "Invalid access token.")
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "nbf", "oid", "tid", "aud", "iss"]},
                leeway=30,
            )
            if (
                claims["tid"] != self.tenant
                or "access_as_user" not in claims.get("scp", "").split()
                or claims.get("azp") != self.client_id
            ):
                raise ValueError("Wrong tenant, scope, or client")
            return str(UUID(claims["oid"]))
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise HTTPException(401, "Your sign-in is invalid or expired. Sign in again.") from exc
