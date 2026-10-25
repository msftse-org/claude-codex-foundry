"""Bounded, data-only reads for a future enrolled client.

This module does not log in, persist enrollment, run commands or edit settings.
The descriptor must arrive through the trusted installation channel, not from
the configuration response. A refresh always requires the previously bound user.
"""

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .config import TIERS

CONFIGURATION_PATH = "/api/v1/me/client-configuration"
MAX_BYTES = 256 * 1024
TIMEOUT_SECONDS = 10
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class ConfigurationError(RuntimeError):
    """Safe to display: excludes server bodies, URLs and credential contents."""


class SignInRequired(ConfigurationError):
    pass


class AccessUnavailable(ConfigurationError):
    pass


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ConfigurationError("Unexpected configuration fields.")


def _uuid(value):
    if not isinstance(value, str):
        raise ConfigurationError("Invalid configuration identity.")
    try:
        if str(UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise ConfigurationError("Invalid configuration identity.")


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ConfigurationError("Invalid configuration model or route identifier.")
    return value


def _url(value, *, configuration=False):
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
    ):
        raise ConfigurationError("Invalid enrollment endpoint.")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.port not in {None, 443}
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
            or ".." in host
            or "." not in host
            or not re.search(r"\.[a-z]{2,}$", host)
            or host in {"metadata.google.internal", "instance-data.ec2.internal"}
            or host.endswith((".localhost", ".local", ".internal"))
            or not re.fullmatch(r"(?:/[A-Za-z0-9._-]+)+", parts.path)
            or any(part in {".", ".."} for part in parts.path.split("/"))
            or (configuration and parts.path != CONFIGURATION_PATH)
            or parts.netloc != host + (":443" if parts.port else "")
        ):
            raise ValueError
        # Reject IP literals entirely, including metadata/link-local/loopback.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return value
    except ValueError:
        pass
    raise ConfigurationError("Invalid enrollment endpoint.")


@dataclass(frozen=True)
class EnrolledRoute:
    client: str
    route_id: str
    gateway_url: str


@dataclass(frozen=True)
class EnrollmentDescriptor:
    organization_id: str
    tenant_id: str
    configuration_url: str
    configuration_resource_id: str
    public_client_id: str
    routes: tuple[EnrolledRoute, ...]

    @classmethod
    def parse(cls, value):
        _object(
            value,
            {
                "schema_version",
                "organization_id",
                "tenant_id",
                "configuration_url",
                "configuration_resource_id",
                "public_client_id",
                "routes",
            },
        )
        if type(value["schema_version"]) is not int or value["schema_version"] != 2:
            raise ConfigurationError("Unsupported enrollment schema.")
        resource = _uuid(value["configuration_resource_id"])
        public_client = _uuid(value["public_client_id"])
        if public_client in {resource, "04b07795-8ddb-461a-bbee-02f9e1bf7b46"}:
            raise ConfigurationError("Enrollment requires a separate organization public client.")
        routes = value["routes"]
        if not isinstance(routes, dict) or not routes or set(routes) - {"claude", "codex"}:
            raise ConfigurationError("Invalid enrollment routes.")
        parsed = []
        for client, route in sorted(routes.items()):
            _object(route, {"route_id", "gateway_url"})
            gateway = _url(route["gateway_url"])
            if client == "codex" and not urlsplit(gateway).path.endswith("/v1"):
                raise ConfigurationError("Codex enrollment requires a v1 gateway.")
            parsed.append(EnrolledRoute(client, _identifier(route["route_id"]), gateway))
        if len({route.route_id for route in parsed}) != len(parsed):
            raise ConfigurationError("Enrollment route identifiers must be distinct.")
        return cls(
            _uuid(value["organization_id"]),
            _uuid(value["tenant_id"]),
            _url(value["configuration_url"], configuration=True),
            resource,
            public_client,
            tuple(parsed),
        )

    @property
    def scope(self):
        return f"api://{self.configuration_resource_id}/configuration.read"


@dataclass(frozen=True)
class VerifiedConfiguration:
    body: bytes
    etag: str

    @property
    def data(self):
        # Return a new object so a caller cannot mutate the verified cache.
        return json.loads(self.body)

    @property
    def subject_id(self):
        return self.data["subject_id"]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _validate(body, etag, descriptor, subject_id):
    if not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ConfigurationError("Configuration exceeds the size limit.")
    expected = '"sha256:' + hashlib.sha256(body).hexdigest() + '"'
    if etag != expected:
        raise ConfigurationError("Configuration integrity check failed.")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs)
    except (ValueError, RecursionError):
        raise ConfigurationError("Invalid configuration JSON.") from None
    _object(
        value,
        {"schema_version", "organization_id", "tenant_id", "subject_id", "state", "clients"},
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ConfigurationError("Unsupported configuration schema.")
    if (
        _uuid(value["organization_id"]) != descriptor.organization_id
        or _uuid(value["tenant_id"]) != descriptor.tenant_id
        or (subject_id is not None and _uuid(value["subject_id"]) != subject_id)
    ):
        raise ConfigurationError("Configuration does not match the enrolled identity.")
    _uuid(value["subject_id"])
    clients = value["clients"]
    _object(clients, {route.client for route in descriptor.routes})
    has_models = False
    for route in descriptor.routes:
        client = clients[route.client]
        _object(client, {"route_id", "default_model", "models"})
        if client["route_id"] != route.route_id:
            raise ConfigurationError("Configuration cannot replace an enrolled route.")
        models = client["models"]
        if not isinstance(models, list) or len(models) > 100:
            raise ConfigurationError("Invalid configuration model list.")
        names, defaults = [], set()
        for model in models:
            if route.client == "claude":
                _object(model, {"name", "family_tier", "family_default"})
                name = _identifier(model["name"])
                tier = model["family_tier"]
                if not isinstance(tier, str) or tier not in TIERS:
                    raise ConfigurationError("Invalid Claude family.")
                if type(model["family_default"]) is not bool:
                    raise ConfigurationError("Invalid Claude family default.")
                if model["family_default"]:
                    if tier in defaults:
                        raise ConfigurationError("Duplicate Claude family default.")
                    defaults.add(tier)
            else:
                _object(model, {"name", "model"})
                name = _identifier(model["name"])
                _identifier(model["model"])
            names.append(name)
        if len(set(names)) != len(names):
            raise ConfigurationError("Duplicate configuration model.")
        default = client["default_model"]
        if (names and default not in names) or (not names and default != ""):
            raise ConfigurationError("Invalid default model.")
        has_models |= bool(names)
    if value["state"] != ("active" if has_models else "no_models"):
        raise ConfigurationError("Invalid configuration availability.")
    return VerifiedConfiguration(body, etag)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(descriptor, token, subject_id, previous):
    if not isinstance(descriptor, EnrollmentDescriptor):
        raise ConfigurationError("A reviewed enrollment descriptor is required.")
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 16384
        or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token)
    ):
        raise SignInRequired("A valid configuration credential is required.")
    if previous is not None:
        previous = _validate(previous.body, previous.etag, descriptor, subject_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if previous is not None:
        headers["If-None-Match"] = previous.etag
    request = Request(descriptor.configuration_url, headers=headers, method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ConfigurationError("Unexpected configuration response.")
            if (
                response.headers.get_content_type() != "application/json"
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise ConfigurationError("Unsupported configuration response format.")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                raise ConfigurationError("Configuration exceeds the size limit.")
            body = response.read(MAX_BYTES + 1)
            return _validate(body, response.headers.get("ETag"), descriptor, subject_id)
    except HTTPError as error:
        try:
            if error.code == 304:
                if previous is None or error.headers.get("ETag") != previous.etag:
                    raise ConfigurationError(
                        "Unexpected conditional configuration response."
                    ) from None
                return previous
            if error.code == 401:
                raise SignInRequired(
                    "Sign-in is required; background refresh cannot sign in."
                ) from None
            if error.code == 403:
                raise AccessUnavailable("Current organization access is unavailable.") from None
            raise ConfigurationError("Configuration service is unavailable; retry later.") from None
        finally:
            error.close()
    except (URLError, OSError, ValueError):
        raise ConfigurationError("Configuration could not be fetched; retry later.") from None


def fetch_initial_configuration(descriptor, access_token):
    """Explicit enrollment only: return the server-verified subject to bind."""
    return _fetch(descriptor, access_token, None, None)


def fetch_configuration(descriptor, subject_id, access_token, *, previous=None):
    """Refresh only the bound subject; no silent account replacement or login."""
    return _fetch(descriptor, access_token, _uuid(subject_id), previous)
