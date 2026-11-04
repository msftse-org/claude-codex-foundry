"""Small ARM client. Only organization administrators need Azure CLI."""

import html
import json
import math
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
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
    def __init__(
        self,
        subscription_id,
        *,
        sleep=time.sleep,
        clock=time.monotonic,
        wall_clock=time.time,
        jitter=random.uniform,
    ):
        self.subscription_id = subscription_id
        self._token = None
        self._expiry = 0
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._jitter = jitter

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

    def request(self, method, path, body=None, version="2024-05-01", *, idempotent=False):
        """Send an ARM request, retrying transient failures only when safe.

        GET, PUT, PATCH, DELETE and HEAD retry automatically. Other methods,
        including POST, require the caller to confirm the operation is safe to
        repeat and explicitly pass ``idempotent=True`` to enable retries.
        """
        deadline = self._clock() + 300
        method = method.upper()
        retryable_method = method in {"GET", "PUT", "PATCH", "DELETE", "HEAD"} or idempotent
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
        failure = AzureError(None, "Request deadline exceeded")
        for attempt in range(1, 7):
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                with urlopen(req, timeout=min(90, remaining)) as response:
                    raw = response.read().decode("utf-8-sig")
                    if not raw:
                        return None
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return raw
            except HTTPError as error:
                headers = error.headers or {}
                with error:
                    detail = error.read().decode("utf-8-sig", errors="replace")
                failure = AzureError(error.code, detail)
                retryable = retryable_method and error.code in {429, 500, 502, 503, 504}
                status = f"HTTP {error.code}"
            except (URLError, ConnectionError, TimeoutError, RemoteDisconnected) as error:
                headers = {}
                detail = str(error.reason if isinstance(error, URLError) else error)
                failure = AzureError(None, detail or type(error).__name__)
                retryable = retryable_method
                status = type(error).__name__
            if not retryable or attempt == 6:
                break
            backoff = min(60, 2**attempt)
            wait = max(
                self._jitter(backoff, min(60, backoff * 2)),
                _retry_after(headers, self._wall_clock()),
            )
            # Never shorten a server's delay to fit the backoff cap or deadline.
            if wait > 60 or wait >= deadline - self._clock():
                break
            print(
                f"Azure {method} {path.split('?', 1)[0]}: {status}; retrying in {wait:g}s",
                file=sys.stderr,
            )
            self._sleep(wait)
        raise failure from None


def _retry_after(headers, now):
    delays = [0]
    for name, scale in (("Retry-After", 1), ("x-ms-retry-after-ms", 0.001)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            delay = float(value) * scale
        except (TypeError, ValueError):
            if name != "Retry-After":
                continue
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                delay = date.timestamp() - now
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(delay):
            delays.append(delay)
    return max(delays)


def _error_detail(detail):
    limit = 500
    detail = str(detail)
    try:
        data = json.loads(detail)
    except json.JSONDecodeError:
        data = None
    error = data.get("error", data) if isinstance(data, dict) else None
    if isinstance(error, dict):
        details = error.get("details")
        first_detail = details[0] if isinstance(details, list) and details else None
        summaries = []
        for item in (error, first_detail):
            if isinstance(item, dict):
                parts = (" ".join(str(item.get(key) or "").split()) for key in ("code", "message"))
                summary = ": ".join(part for part in parts if part)
                if summary:
                    summaries.append(summary)
        if summaries:
            # Reserve room for the actionable detail when the parent summary is long.
            if len(summaries) == 2 and len(summaries[0]) > limit // 2:
                summaries[0] = summaries[0][: limit // 2 - 3].rstrip() + "..."
            return "; ".join(summaries)[:limit]
    title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", detail, re.IGNORECASE | re.DOTALL)
    if title:
        summary = html.unescape(re.sub(r"<[^>]*>", " ", title.group(1)))
    else:
        summary = next((line for line in detail.splitlines() if line.strip()), "Empty response")
    return " ".join(summary.split())[:limit] or "Empty response"


class AzureError(RuntimeError):
    def __init__(self, status, detail):
        prefix = f"Azure HTTP {status}" if status is not None else "Azure request failed"
        super().__init__(f"{prefix}: {_error_detail(detail)}")
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
