"""Call the administration portal's audited API as the signed-in Azure CLI user.

Requires Azure CLI to be pre-authorized on the portal's own registration
(scripts/register_portal.py does this). The portal still enforces the caller's
member role on every request; this client grants nothing by itself.
"""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .azure import az_json


class PortalError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f"Portal returned {status}: {detail}")
        self.status = status


class PortalClient:
    def __init__(self, url, client_id, tenant_id, token=None):
        self.url = url.rstrip("/")
        self.token = (
            token
            or az_json(
                "account",
                "get-access-token",
                "--tenant",
                tenant_id,
                "--scope",
                f"api://{client_id}/access_as_user",
            )["accessToken"]
        )

    def request(self, method, path, body=None):
        request = Request(
            self.url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=120) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as error:
            try:
                detail = json.loads(error.read()).get("detail", error.reason)
            except (ValueError, AttributeError):
                detail = error.reason
            raise PortalError(error.code, detail) from None

    def state(self):
        return self.request("GET", "/api/state")

    def change(self, revision, kind, value):
        return self.request(
            "POST", "/api/changes", {"revision": revision, "kind": kind, "value": value}
        )

    def publish_budgets(self):
        """Publish the current draft as the active budget policy, as the UI does."""
        state = self.state()
        balance = self.request("GET", "/api/budgets/balance")
        return self.request(
            "POST",
            "/api/budgets/publish",
            {
                "revision": state["revision"],
                "organization": state["settings"]["name"],
                "published_hash": balance.get("published_hash"),
            },
        )
