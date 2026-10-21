"""Machine-only APIM L2 cache client. The caller still verifies the policy hash."""

import re
from uuid import UUID

import httpx

MAX_SHARED_BYTES = 1024 * 1024


class ApimPolicyCache:
    def __init__(self, url, audience, credential, client=None):
        if not re.fullmatch(
            r"https://[a-z0-9-]+\.azure-api\.net/[a-z0-9-]+/v1/policy-snapshots", url
        ):
            raise ValueError("Use the fixed HTTPS APIM policy snapshot endpoint")
        if audience != "api://" + str(UUID(audience.removeprefix("api://"))):
            raise ValueError("Use the dedicated budget API audience")
        self.url, self.audience, self.credential = url, audience, credential
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(8, connect=3), follow_redirects=False
        )

    def get(self, digest):
        token = self.credential.get_token(self.audience + "/.default").token
        with self.client.stream(
            "GET", self.url + "/" + digest, headers={"Authorization": "Bearer " + token}
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_SHARED_BYTES:
                    raise ValueError("Policy is too large for the shared cache")
            return bytes(raw), response.headers.get("x-budget-policy-cache", "unknown")
