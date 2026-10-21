"""Install Codex routing without moving sessions, credentials, or project trust."""

import copy
import json
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import tomlkit

from .client import revert_managed
from .config import read_json, validate_client, write_json

PROVIDER = "azure-foundry"


def write_text(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".codex-foundry-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sync_document(document, values):
    """Apply semantic changes while retaining comments and untouched tables."""
    for key in list(document):
        if key not in values:
            del document[key]
    for key, value in values.items():
        if key in document and isinstance(value, dict) and hasattr(document[key], "keys"):
            sync_document(document[key], value)
        elif key not in document or document[key] != value:
            document[key] = value
    return document


def check_managed_preferences():
    for p in [Path("/etc/codex/managed_config.toml"), Path("/etc/codex/requirements.toml")]:
        if p.exists():
            values = tomlkit.parse(p.read_text()).unwrap()
            if any(
                k in values
                for k in (
                    "model_provider",
                    "model_providers",
                    "model_catalog_json",
                    "forced_login_method",
                )
            ):
                raise RuntimeError(
                    "Codex provider/authentication is managed; ask your MDM administrator to deploy the gateway configuration"
                )
    for base in (
        Path("/Library/Managed Preferences"),
        Path("/Library/Managed Preferences") / os.environ.get("USER", ""),
    ):
        for bundle in ("com.openai.codex", "com.openai.chat"):
            path = base / (bundle + ".plist")
            if path.exists() and plistlib.loads(path.read_bytes()):
                raise RuntimeError(
                    "Codex/Desktop has managed preferences; deploy through your MDM administrator"
                )


def catalog_for(c, bundled):
    """Use client-matched metadata, with conservative fallback for legacy models."""
    models = bundled.get("models", []) if isinstance(bundled, dict) else bundled
    if not models or any(not isinstance(m, dict) or not m.get("slug") for m in models):
        raise ValueError("Invalid version-matched Codex catalog")
    by_name = {m["slug"]: m for m in models}
    result = []
    for i, m in enumerate(c["models"]):
        native = by_name.get(m["model"])
        entry = copy.deepcopy(native or models[0])
        entry.update(
            slug=m["name"],
            display_name=m["name"],
            visibility="list",
            supported_in_api=True,
            priority=i,
            upgrade=None,
            additional_speed_tiers=[],
            service_tiers=[],
            availability_nux=None,
            use_responses_lite=False,
            supports_search_tool=False,
            experimental_supported_tools=[],
            supports_experimental_context=False,
            default_reasoning_summary="none",
            supports_reasoning_effort_updates=False,
            supports_parallel_tool_calls=False,
        )
        # Azure HTTP Responses is the qualified transport. Provider-specific
        # fast modes, delegated tools, and websocket-only features are not enabled.
        entry.pop("tool_mode", None)
        entry.pop("multi_agent_version", None)
        entry.pop("multi_agent_reasoning_effort", None)
        if native:
            levels = [
                x
                for x in entry.get("supported_reasoning_levels", [])
                if x["effort"] in {"low", "medium", "high", "xhigh"}
            ]
            entry["supported_reasoning_levels"] = levels
            entry["default_reasoning_level"] = levels[0]["effort"] if levels else "none"
        else:
            reasoning = m["model"].startswith(("gpt-5", "gpt-6", "o1", "o3", "o4", "codex-mini"))
            efforts = (
                ["high"] if "-pro" in m["model"] else ["low", "medium", "high"] if reasoning else []
            )
            entry.update(
                description="Azure Responses compatibility profile; qualified separately from native Codex metadata.",
                context_window=32768,
                max_context_window=32768,
                input_modalities=["text"],
                apply_patch_tool_type=None,
                support_verbosity=False,
                supports_parallel_tool_calls=False,
                model_messages=None,
                default_reasoning_level=efforts[0] if efforts else "none",
                supported_reasoning_levels=[
                    {"effort": e, "description": e.title()} for e in efforts
                ],
            )
        result.append(entry)
    return {"models": result}


def bundled_catalog(executable):
    proc = subprocess.run(
        [str(executable), "debug", "models", "--bundled"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode:
        raise RuntimeError(
            "This Codex build cannot export its bundled catalog; install an approved current build"
        )
    return json.loads(proc.stdout)


def install(c, home=None, executable=None, catalog=None):
    validate_client(c)
    if c.get("client_type") != "codex" or not all(m.get("qualified") is True for m in c["models"]):
        raise ValueError("Install requires a qualified Codex client profile")
    if home is None:
        check_managed_preferences()
        if (
            os.environ.get("CODEX_HOME")
            and Path(os.environ["CODEX_HOME"]).resolve() != (Path.home() / ".codex").resolve()
        ):
            raise RuntimeError(
                "A custom CODEX_HOME is active; review its configuration before installing"
            )
    home = home or Path.home()
    state = home / ".config/claude-foundry/codex"
    config_path = home / ".codex/config.toml"
    for p in (config_path, state):
        if p.is_symlink():
            raise ValueError("Refusing a symlinked Codex configuration or installer state")
    original_text = config_path.read_text() if config_path.exists() else ""
    document = tomlkit.parse(original_text)
    before = document.unwrap()
    manifest_path = state / "managed-installation.json"
    old = read_json(manifest_path, default={})
    old_provider = before.get("model_providers", {}).get(PROVIDER)
    if old and old_provider != old.get("installed", {}).get("model_providers", {}).get(PROVIDER):
        raise ValueError(
            "The managed provider was edited after installation; review it before reinstalling"
        )
    if old_provider and not old:
        raise ValueError(
            "The azure-foundry provider already exists; choose or review its ownership before installing"
        )
    if before.get("model_catalog_json") and not old:
        raise ValueError(
            "A custom Codex catalog already exists; review it before replacing the model catalog"
        )
    if catalog is None:
        app_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
        catalog = bundled_catalog(app_binary if app_binary.exists() else "codex")
    model_catalog = catalog_for(c, catalog)
    helper = state / "credential-helper"
    installed = copy.deepcopy(before)
    installed.update(
        model=c["default_model"],
        model_provider=PROVIDER,
        model_catalog_json=str(state / "models.json"),
        model_reasoning_summary="none",
        web_search="disabled",
    )
    # A global effort overrides every model's metadata (for example, "low"
    # breaks Pro models). Let the selected model supply its supported default.
    installed.pop("model_reasoning_effort", None)
    installed.setdefault("model_providers", {})[PROVIDER] = {
        "name": "Azure Foundry",
        "base_url": c["gateway_url"],
        "wire_api": "responses",
        "request_max_retries": 2,
        "stream_idle_timeout_ms": 240000,
        "supports_websockets": False,
        "auth": {"command": str(helper), "timeout_ms": 30000, "refresh_interval_ms": 300000},
    }
    # Record each currently overwritten setting as the restore target. Keep the
    # original baseline only for values still owned by the previous installation.
    baseline = copy.deepcopy(old.get("baseline", before))
    previous = old.get("installed", {})
    for key in (
        "model",
        "model_provider",
        "model_catalog_json",
        "model_reasoning_effort",
        "model_reasoning_summary",
        "web_search",
    ):
        if old and before.get(key) != previous.get(key):
            if key in before:
                baseline[key] = before[key]
            else:
                baseline.pop(key, None)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = state / "backups" / timestamp / "config.toml"
    write_text(backup, original_text)
    write_json(state / "client.json", c)
    write_json(state / "models.json", model_catalog)
    entrypoint = executable or str(Path(sys.executable).parent / "claude-foundry")
    write_text(
        helper,
        "#!/bin/sh\nset -eu\nexec "
        + shlex.quote(entrypoint)
        + " token --config "
        + shlex.quote(str(state / "client.json"))
        + "\n",
        0o700,
    )
    write_json(
        manifest_path,
        {
            "baseline": baseline,
            "installed": installed,
            "config_existed": old.get("config_existed", config_path.exists()),
        },
    )
    write_text(config_path, tomlkit.dumps(sync_document(document, installed)))
    return {
        "config": str(config_path),
        "helper": str(helper),
        "backup": str(backup),
        "models": len(c["models"]),
    }


def restore(home=None):
    home = home or Path.home()
    state = home / ".config/claude-foundry/codex"
    path = home / ".codex/config.toml"
    if path.is_symlink():
        raise ValueError("Refusing a symlinked Codex configuration")
    manifest_path = state / "managed-installation.json"
    manifest = read_json(manifest_path)
    document = tomlkit.parse(path.read_text() if path.exists() else "")
    restored = revert_managed(document.unwrap(), manifest["installed"], manifest["baseline"])
    if restored or manifest["config_existed"]:
        write_text(path, tomlkit.dumps(sync_document(document, restored)))
    else:
        path.unlink(missing_ok=True)
    manifest_path.rename(state / f"restored-{uuid.uuid4()}.json")
    return "Restored Codex routing; later edits, local sessions, and sign-in credentials were preserved."
