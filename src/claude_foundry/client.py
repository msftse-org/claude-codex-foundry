"""Install and restore settings without losing unrelated customer preferences."""

import copy
import getpass
import plistlib
import re
import shlex
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import read_json, validate_client, write_json

CONFLICTS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
)


def check_desktop_import_version(info_path=None):
    """Fail closed on builds that cannot enforce source-organization restrictions."""
    candidates = (
        [info_path]
        if info_path
        else [
            Path("/Applications/Claude.app/Contents/Info.plist"),
            Path.home() / "Applications/Claude.app/Contents/Info.plist",
        ]
    )
    path = next((p for p in candidates if p.is_file()), None)
    message = "History import requires Claude Desktop 2.26454.0 or later; install an approved build in Applications first."
    if path is None:
        raise RuntimeError(message)
    try:
        version = plistlib.loads(path.read_bytes())["CFBundleShortVersionString"]
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("Unknown version")
        if tuple(map(int, version.split("."))) < (2, 26454, 0):
            raise ValueError("Unsupported version")
    except (KeyError, ValueError, plistlib.InvalidFileException) as error:
        raise RuntimeError(message) from error


def check_managed_preferences():
    """Fail before editing if MDM controls the inference profile."""
    for base in (
        Path("/Library/Managed Preferences"),
        Path("/Library/Managed Preferences") / getpass.getuser(),
    ):
        path = base / "com.anthropic.claudefordesktop.plist"
        if path.exists():
            with path.open("rb") as stream:
                managed = plistlib.load(stream)
            if any(
                key.startswith("inference")
                or key in {"bootstrapUrl", "forceLoginOrgUUID", "claudeAiImport"}
                for key in managed
            ):
                raise RuntimeError(
                    f"Claude inference is managed by {path}. Ask your MDM administrator to deploy the profile; local installation cannot override it."
                )
    code_policy = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if code_policy.exists():
        managed = read_json(code_policy)
        if "apiKeyHelper" in managed or any(
            key.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")) for key in managed.get("env", {})
        ):
            raise RuntimeError(
                f"Claude Code inference is managed by {code_policy}; deploy these settings through your MDM administrator."
            )


def merge_cli(current, c, helper_path):
    result = copy.deepcopy(current)
    result["apiKeyHelper"] = shlex.quote(str(helper_path))
    result["model"] = c["models"][0]["name"]
    env = result.setdefault("env", {})
    for key in CONFLICTS:
        env.pop(key, None)
    env.update(
        ANTHROPIC_BASE_URL=c["gateway_url"],
        CLAUDE_CODE_API_KEY_HELPER_TTL_MS="300000",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    for model in c["models"]:
        if model.get("isFamilyDefault"):
            env[f"ANTHROPIC_DEFAULT_{model['anthropicFamilyTier'].upper()}_MODEL"] = model["name"]
    return result


def desktop_settings(c, helper):
    import json

    settings = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": c["gateway_url"],
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceCredentialKind": "helper-script",
        "inferenceCredentialHelper": str(helper),
        "inferenceCredentialHelperTtlSec": "300",
        "inferenceCredentialHelperTimeoutSec": "60",
        # created_at belongs to the HTTP model catalog, not Desktop's schema.
        "inferenceModels": json.dumps(
            [{k: v for k, v in m.items() if k != "created_at"} for m in c["models"]]
        ),
        "modelDiscoveryEnabled": "false",
        "chatTabEnabled": "true",
        "isClaudeCodeForDesktopEnabled": "true",
    }
    if "conversation_import" in c:
        # Use the vendor's importer, never rewrite conversation stores. An
        # explicit policy disables automatic copying of tasks/plugins and export.
        settings["claudeAiImport"] = json.dumps(
            {
                "automatic3pImport": False,
                "exportEnabled": False,
                "bannerBehavior": "detect",
                **c["conversation_import"],
            }
        )
    return settings


def install(c, home=None, executable=None):
    validate_client(c)
    if home is None and c.get("conversation_import", {}).get("enabled"):
        check_desktop_import_version()
    home = home or Path.home()
    state = home / ".config/claude-foundry"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path = state / "managed-installation.json"
    old_manifest = read_json(manifest_path, default={})
    desktop = home / "Library/Application Support/Claude-3p"
    cli_path = home / ".claude/settings.json"
    meta_path = desktop / "configLibrary/_meta.json"
    current_meta = read_json(meta_path, default={"entries": []})
    matching = [
        e["id"]
        for e in current_meta.get("entries", [])
        if e.get("name") == c.get("profile_name", "Claude on Azure")
    ]
    profile_id = old_manifest.get(
        "profile_id", matching[0] if len(matching) == 1 else str(uuid.uuid4())
    )
    uuid.UUID(profile_id)  # Never allow a profile ID to escape configLibrary.
    profile_path = desktop / "configLibrary" / f"{profile_id}.json"
    mode_path = desktop / "claude_desktop_config.json"
    helper = state / "credential-helper"
    current_cli = read_json(cli_path, default={})
    current_mode = read_json(mode_path, default={})
    current_profile = read_json(profile_path, default={})
    if "conversation_import" in c and "claudeAiImport" in current_profile:
        import json

        existing = current_profile["claudeAiImport"]
        if isinstance(existing, str):
            existing = json.loads(existing)
        # Never silently widen an existing source restriction or replace a
        # customer-owned import endpoint with the public Claude.ai service.
        if not isinstance(existing, dict):
            raise ValueError(
                "Existing Claude import policy is invalid; review it in Claude Desktop"
            )
        if any(key in existing for key in ("url", "oauthIssuer", "oauthClientId")):
            raise ValueError(
                "A custom history import endpoint is configured; manage it in Claude Desktop"
            )
        old_ids = existing.get("allowedOrganizationUuids")
        if old_ids is not None and "allowedOrganizationUuids" not in c["conversation_import"]:
            c = copy.deepcopy(c)
            c["conversation_import"]["allowedOrganizationUuids"] = old_ids
            validate_client(c)
    meta = copy.deepcopy(current_meta)
    if not any(e["id"] == profile_id for e in meta["entries"]):
        meta["entries"].append({"id": profile_id, "name": c.get("profile_name", "Claude on Azure")})
    meta["appliedId"] = profile_id
    desired = {
        str(cli_path): merge_cli(current_cli, c, helper),
        str(meta_path): meta,
        str(profile_path): {**current_profile, **desktop_settings(c, helper)},
        str(mode_path): {**current_mode, "deploymentMode": "3p"},
    }
    # Keep the first baseline across reinstall/upgrade. A snapshot per run also
    # protects later customer changes. Tokens never appear in these files.
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    snapshot = {
        p: {"exists": Path(p).exists(), "value": read_json(Path(p), default={})} for p in desired
    }
    write_json(state / "backups" / timestamp / "settings.json", snapshot)
    baseline = copy.deepcopy(old_manifest.get("baseline", snapshot))
    if "conversation_import" in c and "claudeAiImport" in current_profile:
        previous = old_manifest.get("installed", {}).get(str(profile_path), {})
        if current_profile["claudeAiImport"] != previous.get("claudeAiImport"):
            # Keep a customer-authored policy as the rollback target when this
            # upgrade starts managing import, including a previous deny list.
            baseline[str(profile_path)]["value"]["claudeAiImport"] = copy.deepcopy(
                current_profile["claudeAiImport"]
            )
    for path, after in desired.items():
        baseline[path]["value"] = update_baseline(
            snapshot[path]["value"],
            after,
            baseline[path]["value"],
            old_manifest.get("installed", {}).get(path, snapshot[path]["value"]),
        )
    entrypoint = executable or str(Path(sys.executable).parent / "claude-foundry")
    helper.write_text("#!/bin/sh\nset -eu\nexec " + shlex.quote(entrypoint) + " token\n")
    helper.chmod(0o700)
    write_json(state / "client.json", c)
    # Persist recovery data before mutations, so interrupted installations remain recoverable.
    write_json(
        manifest_path, {"profile_id": profile_id, "baseline": baseline, "installed": desired}
    )
    for path, value in desired.items():
        write_json(Path(path), value)
    return {
        "profile": str(profile_path),
        "helper": str(helper),
        "backup": str(state / "backups" / timestamp),
    }


_MISSING = object()


def update_baseline(before, after, baseline, previous):
    """Do not claim ownership of unrelated edits observed during an upgrade."""
    result = copy.deepcopy(baseline)
    for key in set(before) | set(after) | set(baseline) | set(previous):
        now, desired, original, installed = (
            before.get(key, _MISSING),
            after.get(key, _MISSING),
            baseline.get(key, _MISSING),
            previous.get(key, _MISSING),
        )
        if isinstance(now, dict) and isinstance(desired, dict):
            result[key] = update_baseline(
                now,
                desired,
                original if isinstance(original, dict) else {},
                installed if isinstance(installed, dict) else {},
            )
        elif now == desired and original == installed:
            if desired is _MISSING:
                result.pop(key, None)
            else:
                result[key] = copy.deepcopy(desired)
    return result


def revert_managed(current, installed, baseline):
    """Three-way restore: preserve values users changed after installation."""
    result = copy.deepcopy(current)
    for key in set(installed) | set(baseline):
        old, ours, now = (
            baseline.get(key, _MISSING),
            installed.get(key, _MISSING),
            current.get(key, _MISSING),
        )
        if old == ours:
            continue
        if isinstance(ours, dict) and isinstance(now, dict):
            result[key] = revert_managed(now, ours, old if isinstance(old, dict) else {})
            if not result[key] and old is _MISSING:
                result.pop(key, None)
        elif now == ours:
            if old is _MISSING:
                result.pop(key, None)
            else:
                result[key] = copy.deepcopy(old)
    return result


def restore(home=None):
    home = home or Path.home()
    state = home / ".config/claude-foundry"
    manifest = read_json(state / "managed-installation.json")
    profile_id = manifest["profile_id"]
    profile_path = (
        home / "Library/Application Support/Claude-3p/configLibrary" / f"{profile_id}.json"
    )
    remaining_profile = revert_managed(
        read_json(profile_path, default={}),
        manifest["installed"][str(profile_path)],
        manifest["baseline"][str(profile_path)]["value"],
    )
    for filename, installed in manifest["installed"].items():
        path = Path(filename)
        path.resolve().relative_to(home.resolve())
        current = read_json(path, default={})
        baseline = manifest["baseline"][filename]
        restored = revert_managed(current, installed, baseline["value"])
        if (
            path.name == "_meta.json"
            and not remaining_profile
            and not manifest["baseline"][str(profile_path)]["exists"]
        ):
            # Remove only our new entry, preserving profiles added after install.
            restored["entries"] = [
                e for e in current.get("entries", []) if e.get("id") != profile_id
            ]
        if not baseline["exists"] and not restored:
            path.unlink(missing_ok=True)
        else:
            write_json(path, restored)
    # Keep backups, credentials and conversations; restore only managed settings.
    (state / "managed-installation.json").rename(state / f"restored-{uuid.uuid4()}.json")
    print(
        "Restored unchanged managed settings. Restart Claude Desktop. Conversations and backups were preserved."
    )
