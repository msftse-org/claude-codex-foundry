"""Small ARM client. Only organization administrators need Azure CLI."""

import json
import shutil
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def find_az():
    executable = shutil.which("az")
    if not executable:
        for candidate in ("/opt/homebrew/bin/az", "/usr/local/bin/az"):
            if shutil.which(candidate):
                executable = candidate
                break
    if not executable:
        raise RuntimeError(
            "Azure CLI is not installed. Run scripts/macos/install.sh --install-azure-cli, or use MSAL mode."
        )
    return executable


def az_json(*args):
    result = subprocess.run(
        [find_az(), *args, "--only-show-errors", "-o", "json"],
        text=True,
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


class Azure:
    def __init__(self, subscription_id):
        self.subscription_id = subscription_id
        self._token = None
        self._expiry = 0

    def token(self):
        if self._token is None or time.time() > self._expiry:
            data = az_json(
                "account",
                "get-access-token",
                "--subscription",
                self.subscription_id,
                "--resource",
                "https://management.azure.com/",
            )
            self._token = data["accessToken"]
            self._expiry = time.time() + 240
        return self._token

    def request(self, method, path, body=None, version="2024-05-01"):
        url = "https://management.azure.com" + path + "?" + urlencode({"api-version": version})
        req = Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=90) as response:
                raw = response.read().decode("utf-8-sig")
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except HTTPError as error:
            detail = error.read().decode()
            raise AzureError(error.code, detail) from None


class AzureError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f"Azure HTTP {status}: {detail}")
        self.status = status


def account_path(c):
    return (
        f"/subscriptions/{c['subscription_id']}/resourceGroups/{c['foundry_resource_group']}"
        f"/providers/Microsoft.CognitiveServices/accounts/{c['foundry_name']}"
    )


def api_path(c):
    return (
        f"/subscriptions/{c['subscription_id']}/resourceGroups/{c['apim_resource_group']}"
        f"/providers/Microsoft.ApiManagement/service/{c['apim_name']}/apis/{c['api_id']}"
    )
