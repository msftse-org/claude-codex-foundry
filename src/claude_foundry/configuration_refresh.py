"""Explicit enrollment and model-only refresh for the macOS pilot.

The reviewed, locally delivered descriptor pins identity and routing. Responses
can change model data, never commands, URLs or installer policy. This module
does not schedule itself, install software, or change server-side authorization.
"""

import base64
import copy
import hashlib
import json
import os
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

import tomlkit

from . import client, codex_client
from .configuration_client import (
    ConfigurationError,
    EnrollmentDescriptor,
    _pairs,
    _uuid,
    _validate,
    fetch_configuration,
    fetch_initial_configuration,
)
from .enrollment_auth import EnrolledAzureCli, _claims
from .local_update import FileTransaction, _read

STATE = ".config/claude-foundry/enrollment"
CLAUDE_ROUTING = (
    "inferenceProvider",
    "inferenceGatewayBaseUrl",
    "inferenceGatewayAuthScheme",
    "inferenceCredentialKind",
    "inferenceCredentialHelper",
    "inferenceCredentialHelperTtlSec",
    "inferenceCredentialHelperTimeoutSec",
)


def _json(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def _object_file(path, *, default=None, private=False):
    stored = _read(path)
    if stored is None:
        if default is None:
            raise ConfigurationError("Organization enrollment is incomplete; run enroll.")
        return copy.deepcopy(default), None
    if private and stored[1] != 0o600:
        raise ConfigurationError("Enrollment records must have mode 0600.")
    try:
        value = json.loads(stored[0], object_pairs_hook=_pairs)
        if not isinstance(value, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise ConfigurationError(
            "Invalid local configuration JSON; review before updating."
        ) from None
    return value, stored[0]


def _descriptor_value(descriptor):
    return {
        "schema_version": 1,
        "organization_id": descriptor.organization_id,
        "tenant_id": descriptor.tenant_id,
        "configuration_url": descriptor.configuration_url,
        "configuration_resource_id": descriptor.configuration_resource_id,
        "routes": {
            route.client: {"route_id": route.route_id, "gateway_url": route.gateway_url}
            for route in descriptor.routes
        },
    }


def _paths(home, descriptor):
    state = home / STATE
    # Deterministic, organization-specific filename, never a path from the API.
    profile_id = str(uuid5(UUID(descriptor.organization_id), "claude-foundry-enrollment-v1"))
    library = home / "Library/Application Support/Claude-3p/configLibrary"
    return {
        "binding": state / "enrollment.json",
        "ownership": state / "managed-settings.json",
        "cache": state / "configuration.json",
        "status": state / "status.json",
        "helper": state / "credential-helper",
        "catalog": state / "models.json",
        "claude_cli": home / ".claude/settings.json",
        "claude_profile": library / f"{profile_id}.json",
        "claude_meta": library / "_meta.json",
        "claude_mode": library.parent / "claude_desktop_config.json",
        "codex": home / ".codex/config.toml",
    }


def _binding(home):
    value, body = _object_file(home / STATE / "enrollment.json", private=True)
    if (
        set(value)
        != {
            "schema_version",
            "descriptor",
            "subject_id",
            "clients",
            "entrypoint",
            "codex_binary",
        }
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ConfigurationError("Invalid enrollment record.")
    descriptor = EnrollmentDescriptor.parse(value["descriptor"])
    _uuid(value["subject_id"])
    clients = value["clients"]
    if (
        not isinstance(clients, list)
        or not clients
        or any(not isinstance(name, str) for name in clients)
        or len(set(clients)) != len(clients)
        or set(clients) - {route.client for route in descriptor.routes}
    ):
        raise ConfigurationError("Invalid enrolled client selection.")
    for key in ["entrypoint", *(["codex_binary"] if "codex" in clients else [])]:
        path = value[key]
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or ".." in Path(path).parts
            or any(ord(char) < 32 for char in path)
        ):
            raise ConfigurationError("Invalid locally pinned executable.")
    return value, body, descriptor


def _local_executable(value):
    try:
        path = Path(value).resolve(strict=True)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError
        return str(path)
    except (OSError, ValueError):
        raise ConfigurationError(
            "Install an approved local client/helper executable first."
        ) from None


def _helper(binding):
    # The server cannot supply arguments, a command, or an executable path.
    return (
        "#!/bin/sh\nset -eu\nexec " + shlex.quote(binding["entrypoint"]) + " enrolled-token\n"
    ).encode()


def _cache(path, descriptor, subject_id):
    value, _ = _object_file(path, private=True)
    if set(value) != {"body", "etag"} or not isinstance(value["body"], str):
        raise ConfigurationError("Invalid verified configuration cache.")
    return _validate(value["body"].encode(), value["etag"], descriptor, subject_id)


def _hash(body):
    return hashlib.sha256(body).hexdigest()


def _guard(actual, expected):
    if actual != expected:
        raise ConfigurationError(
            "Enrolled routing or a managed file changed locally; review it before refreshing."
        )


def _claude_profile(descriptor, configuration):
    route = next(route for route in descriptor.routes if route.client == "claude")
    return {
        "gateway_url": route.gateway_url,
        "models": [
            {
                "name": model["name"],
                "anthropicFamilyTier": model["family_tier"],
                "isFamilyDefault": model["family_default"],
            }
            for model in configuration["models"]
        ],
    }


def _selection(current, configuration):
    if current is not None and not isinstance(current, str):
        raise ConfigurationError("Invalid local model selection.")
    names = {model["name"] for model in configuration["models"]}
    return current if current in names or not names else configuration["default_model"]


def _legacy_profile(directory, descriptor, gateway_url):
    value, _ = _object_file(directory / "client.json")
    if value.get("tenant_id") != descriptor.tenant_id or value.get("gateway_url") != gateway_url:
        raise ConfigurationError("Legacy profile identity or routing does not match enrollment.")


def _prepare_claude(paths, binding, configuration, previous, initial, migrate_existing):
    descriptor = EnrollmentDescriptor.parse(binding["descriptor"])
    profile = _claude_profile(descriptor, configuration)
    cli, cli_body = _object_file(paths["claude_cli"], default={})
    desktop, desktop_body = _object_file(paths["claude_profile"], default={})
    env = cli.get("env", {})
    if not isinstance(env, dict):
        raise ConfigurationError("Invalid existing Claude environment settings.")
    helper = paths["helper"]
    managed_aliases = {}
    if initial:
        legacy_helper = helper.parent.parent / "credential-helper"
        existing_helper = cli.get("apiKeyHelper")
        existing_gateway = env.get("ANTHROPIC_BASE_URL")
        if existing_helper is not None or existing_gateway is not None:
            if (
                not migrate_existing
                or existing_helper != shlex.quote(str(legacy_helper))
                or existing_gateway != profile["gateway_url"]
            ):
                raise ConfigurationError(
                    "Existing Claude routing requires explicit matching-profile migration."
                )
        if desktop:
            raise ConfigurationError("An enrollment profile already exists without its record.")
        if existing_helper is not None:
            _legacy_profile(helper.parent.parent, descriptor, profile["gateway_url"])
            legacy, _ = _object_file(helper.parent.parent / "managed-installation.json")
            profile_id = _uuid(legacy.get("profile_id"))
            legacy_path = paths["claude_profile"].parent / f"{profile_id}.json"
            desktop, _ = _object_file(legacy_path)
        desired_cli = client.merge_cli(cli, profile, helper)
        desired_desktop = {**desktop, **client.desktop_settings(profile, helper)}
        managed_aliases = {
            f"ANTHROPIC_DEFAULT_{model['anthropicFamilyTier'].upper()}_MODEL": model["name"]
            for model in profile["models"]
            if model["isFamilyDefault"]
        }
    else:
        _guard(cli.get("apiKeyHelper"), shlex.quote(str(helper)))
        _guard(env.get("ANTHROPIC_BASE_URL"), profile["gateway_url"])
        for key in CLAUDE_ROUTING:
            _guard(desktop.get(key), previous["claude_profile"][key])
        desired_cli, desired_desktop = copy.deepcopy(cli), copy.deepcopy(desktop)
        desired_env = desired_cli.setdefault("env", {})
        for key, old in previous["claude_aliases"].items():
            # Remove only aliases still owned by this installation.
            if desired_env.get(key) == old:
                desired_env.pop(key)
        for model in profile["models"]:
            if model["isFamilyDefault"]:
                key = f"ANTHROPIC_DEFAULT_{model['anthropicFamilyTier'].upper()}_MODEL"
                if key not in desired_env:
                    desired_env[key] = model["name"]
                    managed_aliases[key] = model["name"]
        desired_desktop["inferenceModels"] = client.desktop_settings(profile, helper)[
            "inferenceModels"
        ]
    selection = _selection(cli.get("model"), configuration)
    if selection is None:
        desired_cli.pop("model", None)
    else:
        desired_cli["model"] = selection
    changes = {
        "claude_cli": (_json(desired_cli), cli_body),
        "claude_profile": (_json(desired_desktop), desktop_body),
    }
    if initial:
        meta, meta_body = _object_file(paths["claude_meta"], default={"entries": []})
        mode, mode_body = _object_file(paths["claude_mode"], default={})
        entries = meta.get("entries")
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ConfigurationError("Invalid Claude Desktop profile library.")
        profile_id = paths["claude_profile"].stem
        if any(entry.get("id") == profile_id for entry in entries):
            raise ConfigurationError("Claude enrollment profile requires recovery review.")
        meta["entries"].append({"id": profile_id, "name": "Organization gateway"})
        meta["appliedId"] = profile_id
        mode["deploymentMode"] = "3p"
        changes.update(
            claude_meta=(_json(meta), meta_body),
            claude_mode=(_json(mode), mode_body),
        )
    return changes, {
        "claude_profile": {key: desired_desktop[key] for key in CLAUDE_ROUTING},
        "claude_aliases": managed_aliases,
    }


def _prepare_codex(paths, binding, configuration, previous, initial, migrate_existing):
    stored = _read(paths["codex"])
    before = None if stored is None else stored[0]
    try:
        document = tomlkit.parse((before or b"").decode())
    except (ValueError, UnicodeError):
        raise ConfigurationError("Invalid Codex settings; review before refreshing.") from None
    values = document.unwrap()
    descriptor = EnrollmentDescriptor.parse(binding["descriptor"])
    route = next(route for route in descriptor.routes if route.client == "codex")
    providers = values.get("model_providers", {})
    provider = {
        "name": "Azure Foundry",
        "base_url": route.gateway_url,
        "wire_api": "responses",
        "request_max_retries": 2,
        "stream_idle_timeout_ms": 240000,
        "supports_websockets": False,
        "auth": {
            "command": str(paths["helper"]),
            "timeout_ms": 30000,
            "refresh_interval_ms": 300000,
        },
    }
    if not isinstance(providers, dict):
        raise ConfigurationError("Invalid Codex providers.")
    if initial:
        if values.get("model_catalog_url"):
            raise ConfigurationError("Review the existing remote Codex catalog before enrolling.")
        if providers.get(codex_client.PROVIDER) or values.get("model_catalog_json"):
            legacy_state = paths["helper"].parent.parent / "codex"
            legacy, _ = _object_file(legacy_state / "managed-installation.json", default={})
            installed = legacy.get("installed")
            old_providers = (
                installed.get("model_providers") if isinstance(installed, dict) else None
            )
            expected = (
                old_providers.get(codex_client.PROVIDER)
                if isinstance(old_providers, dict)
                else None
            )
            if (
                not migrate_existing
                or not isinstance(expected, dict)
                or not expected
                or providers.get(codex_client.PROVIDER) != expected
                or expected.get("base_url") != route.gateway_url
                or values.get("model_catalog_json") != str(legacy_state / "models.json")
            ):
                raise ConfigurationError(
                    "Existing Codex routing requires explicit matching-profile migration."
                )
            _legacy_profile(legacy_state, descriptor, route.gateway_url)
        values.update(
            model_provider=codex_client.PROVIDER,
            model_catalog_json=str(paths["catalog"]),
            model_reasoning_summary="none",
            web_search="disabled",
        )
        values.pop("model_reasoning_effort", None)
        values.setdefault("model_providers", {})[codex_client.PROVIDER] = provider
    else:
        _guard(values.get("model_provider"), codex_client.PROVIDER)
        _guard(providers.get(codex_client.PROVIDER), provider)
        _guard(values.get("model_catalog_json"), str(paths["catalog"]))
        catalog = _read(paths["catalog"])
        _guard(None if catalog is None else _hash(catalog[0]), previous["codex_catalog_hash"])
    selection = _selection(values.get("model"), configuration)
    if selection is None:
        values.pop("model", None)
    else:
        values["model"] = selection
    catalog = codex_client.catalog_for(
        {"models": configuration["models"]}, codex_client.bundled_catalog(binding["codex_binary"])
    )
    catalog_body = _json(catalog)
    current_catalog = _read(paths["catalog"])
    return {
        "codex": (tomlkit.dumps(codex_client.sync_document(document, values)).encode(), before),
        "catalog": (catalog_body, None if current_catalog is None else current_catalog[0]),
    }, {"codex_catalog_hash": _hash(catalog_body)}


def _apply(home, transaction, paths, binding, verified, *, initial, migrate_existing=False):
    descriptor = EnrollmentDescriptor.parse(binding["descriptor"])
    verified = _validate(verified.body, verified.etag, descriptor, binding["subject_id"])
    ownership, ownership_body = _object_file(paths["ownership"], default={}, private=True)
    if initial:
        if ownership:
            raise ConfigurationError("Existing enrollment ownership requires recovery review.")
        helper_before = _read(paths["helper"])
        if helper_before is not None or _read(paths["catalog"]) is not None:
            raise ConfigurationError("Enrollment files already exist without their record.")
        baseline = {}
        previous = {}
    else:
        if set(ownership) != {"schema_version", "baseline", "installed"}:
            raise ConfigurationError("Invalid enrollment ownership record.")
        if type(ownership["schema_version"]) is not int or ownership["schema_version"] != 1:
            raise ConfigurationError("Unsupported enrollment ownership schema.")
        baseline, previous = ownership["baseline"], ownership["installed"]
        expected_keys = set()
        if "claude" in binding["clients"]:
            expected_keys.update(("claude_profile", "claude_aliases"))
        if "codex" in binding["clients"]:
            expected_keys.add("codex_catalog_hash")
        if (
            not isinstance(baseline, dict)
            or not isinstance(previous, dict)
            or set(previous) != expected_keys
            or (
                "claude" in binding["clients"]
                and (
                    not isinstance(previous["claude_profile"], dict)
                    or set(previous["claude_profile"]) != set(CLAUDE_ROUTING)
                    or not isinstance(previous["claude_aliases"], dict)
                    or any(
                        name
                        not in {
                            "ANTHROPIC_DEFAULT_SONNET_MODEL",
                            "ANTHROPIC_DEFAULT_OPUS_MODEL",
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                        }
                        or not isinstance(value, str)
                        for name, value in previous["claude_aliases"].items()
                    )
                )
            )
        ):
            raise ConfigurationError("Invalid enrollment ownership record.")
        _guard(_read(paths["helper"]), (_helper(binding), 0o700))
    changes, installed = {}, {}
    for name in binding["clients"]:
        configuration = verified.data["clients"][name]
        if initial and not configuration["models"]:
            raise ConfigurationError("A selected client has no currently published models.")
        prepare = _prepare_claude if name == "claude" else _prepare_codex
        rendered, owned = prepare(
            paths, binding, configuration, previous, initial, migrate_existing
        )
        changes.update(rendered)
        installed.update(owned)
    if initial:
        # Private recovery snapshot only; no credentials are sent to a server.
        baseline = {
            name: None if before is None else base64.b64encode(before).decode()
            for name, (_, before) in changes.items()
        }
    for name, (body, before) in changes.items():
        transaction.stage(paths[name], body, expected=before)
    if initial:
        transaction.stage(paths["helper"], _helper(binding), expected=None, mode=0o700)
        transaction.stage(paths["binding"], _json(binding), expected=None)
    transaction.stage(
        paths["ownership"],
        _json({"schema_version": 1, "baseline": baseline, "installed": installed}),
        expected=ownership_body,
    )
    cache_before = _read(paths["cache"])
    transaction.stage(
        paths["cache"],
        _json({"body": verified.body.decode(), "etag": verified.etag}),
        expected=None if cache_before is None else cache_before[0],
    )
    status_before = _read(paths["status"])
    status = {
        "state": verified.data["state"],
        "last_success": datetime.now(UTC).isoformat(),
        "models": {
            name: len(verified.data["clients"][name]["models"]) for name in binding["clients"]
        },
    }
    transaction.stage(
        paths["status"], _json(status), expected=None if status_before is None else status_before[0]
    )
    transaction.commit()
    return status


def enroll(
    descriptor_path,
    *,
    clients=None,
    home=None,
    executable=None,
    codex_binary=None,
    migrate_existing=False,
):
    """Explicit sign-in; repeat only for the same descriptor and enrolled user."""
    home = home or Path.home()
    descriptor_value, _ = _object_file(descriptor_path)
    descriptor = EnrollmentDescriptor.parse(descriptor_value)
    paths = _paths(home, descriptor)
    selected = sorted(clients or [route.client for route in descriptor.routes])
    if (
        not selected
        or len(set(selected)) != len(selected)
        or set(selected) - {route.client for route in descriptor.routes}
    ):
        raise ConfigurationError("Choose clients included in the reviewed descriptor.")
    with FileTransaction(home / STATE, paths.values()) as transaction:
        existing = _read(paths["binding"])
        if existing is not None:
            binding, _, enrolled_descriptor = _binding(home)
            _guard(enrolled_descriptor, descriptor)
            _guard(binding["clients"], selected)
        else:
            app = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
            binding = {
                "schema_version": 1,
                "descriptor": _descriptor_value(descriptor),
                "subject_id": None,
                "clients": selected,
                "entrypoint": _local_executable(
                    executable or Path(sys.executable).parent / "claude-foundry"
                ),
                "codex_binary": _local_executable(
                    codex_binary or (app if app.is_file() else shutil.which("codex") or app)
                )
                if "codex" in selected
                else None,
            }
        auth = EnrolledAzureCli(
            descriptor, home / STATE / "azure", subject_id=binding["subject_id"]
        )
        token = auth.login()
        verified = fetch_initial_configuration(descriptor, token)
        subject = _uuid(_claims(token).get("oid"))
        _guard(verified.subject_id, subject)
        if binding["subject_id"] is not None:
            _guard(verified.subject_id, binding["subject_id"])
        binding["subject_id"] = verified.subject_id
        # Validate inference credentials before replacing a working installation.
        EnrolledAzureCli(
            descriptor, home / STATE / "azure", subject_id=verified.subject_id
        ).inference_token()
        return _apply(
            home,
            transaction,
            paths,
            binding,
            verified,
            initial=existing is None,
            migrate_existing=migrate_existing,
        )


def refresh(*, home=None, login=False):
    """Noninteractive unless login is explicit; failed fetches preserve settings."""
    home = home or Path.home()
    binding, original, descriptor = _binding(home)
    paths = _paths(home, descriptor)
    with FileTransaction(home / STATE, paths.values()) as transaction:
        binding, current, descriptor = _binding(home)
        _guard(current, original)
        auth = EnrolledAzureCli(
            descriptor, home / STATE / "azure", subject_id=binding["subject_id"]
        )
        token = auth.login() if login else auth.configuration_token()
        verified = fetch_configuration(
            descriptor,
            binding["subject_id"],
            token,
            previous=_cache(paths["cache"], descriptor, binding["subject_id"]),
        )
        return _apply(home, transaction, paths, binding, verified, initial=False)


def inference_token(*, home=None):
    """Called only by the fixed helper, never grants access from cached model data."""
    home = home or Path.home()
    binding, _, descriptor = _binding(home)
    return EnrolledAzureCli(
        descriptor, home / STATE / "azure", subject_id=binding["subject_id"]
    ).inference_token()


def status(*, home=None):
    """No account identifiers or tokens in routine status output."""
    home = home or Path.home()
    binding, _, _ = _binding(home)
    value, _ = _object_file(home / STATE / "status.json", private=True)
    return {**value, "clients": binding["clients"]}
