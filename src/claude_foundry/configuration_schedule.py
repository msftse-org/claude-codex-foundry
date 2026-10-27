"""Per-user launchd scheduling for enrollment; no server-supplied job or commands."""

import hashlib
import os
import plistlib
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from . import configuration_refresh as refresh
from .configuration_client import AccessUnavailable, ConfigurationError, SignInRequired
from .local_update import FileTransaction, _read

LABEL = "com.claude-foundry.configuration-refresh"
INTERVAL_SECONDS = 900
MAX_JITTER_SECONDS = 60


def _paths(home):
    state = home / refresh.STATE / "scheduler"
    return (
        state,
        home / "Library/LaunchAgents" / f"{LABEL}.plist",
        state / "agent.json",
    )


def _job(binding, home, enabled):
    return {
        "Label": LABEL,
        "ProgramArguments": [binding["entrypoint"], "refresh-background"],
        "EnvironmentVariables": {
            "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "RunAtLoad": enabled,
        "StartInterval": INTERVAL_SECONDS,
        "Disabled": not enabled,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        # Never persist credentials, URLs, user identifiers or raw errors in job logs.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def _launchctl(*arguments):
    try:
        return subprocess.run(
            ["/bin/launchctl", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigurationError("Could not manage the per-user refresh job.") from None


def configure(*, enabled, home=None):
    """Enroll/resume/pause, refusing a job edited outside this installation."""
    home = home or Path.home()
    binding, _, _ = refresh._binding(home)
    refresh.status(home=home)  # Require a completed enrollment, not just a binding.
    state, plist, record = _paths(home)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    with FileTransaction(state, [plist, record]) as transaction:
        stored = _read(plist)
        ownership, record_body = refresh._object_file(record, default={}, private=True)
        if stored is None:
            if ownership or not enabled:
                raise ConfigurationError("Refresh job is missing; review scheduler recovery.")
        elif (
            set(ownership) != {"schema_version", "sha256"}
            or type(ownership["schema_version"]) is not int
            or ownership["schema_version"] != 1
            or ownership["sha256"] != hashlib.sha256(stored[0]).hexdigest()
        ):
            raise ConfigurationError("Refresh job was changed locally; review before replacing.")
        body = plistlib.dumps(_job(binding, home, enabled), sort_keys=True)
        changed = stored is None or stored[0] != body
        loaded = _launchctl("print", target) == 0
        if loaded and (changed or not enabled):
            if _launchctl("bootout", target):
                raise ConfigurationError("Could not pause the existing refresh job.")
            loaded = False
        transaction.stage(plist, body, expected=None if stored is None else stored[0])
        transaction.stage(
            record,
            refresh._json({"schema_version": 1, "sha256": hashlib.sha256(body).hexdigest()}),
            expected=record_body,
        )
        transaction.commit()
        if enabled and not loaded and _launchctl("bootstrap", domain, str(plist)):
            # Retain the known job for explicit retry; do not repeatedly bootstrap.
            raise ConfigurationError(
                "Refresh job was saved but could not be loaded; retry enable-refresh in a GUI session."
            )
    return {"automatic_refresh": enabled, "interval_seconds": INTERVAL_SECONDS}


def pause_for_restore(*, home):
    """No-op only if neither an owned job nor a loaded job exists."""
    _, plist, record = _paths(home)
    if _read(plist) is None and _read(record) is None:
        if _launchctl("print", f"gui/{os.getuid()}/{LABEL}") == 0:
            raise ConfigurationError("An unowned refresh job is loaded; review before restoring.")
        return
    configure(enabled=False, home=home)


def _record_failure(home, code):
    try:
        _, _, descriptor = refresh._binding(home)
        paths = refresh._paths(home, descriptor)
        with FileTransaction(home / refresh.STATE, paths.values()) as transaction:
            # Only status changes. Never store exception text or discard last_success.
            value, before = refresh._object_file(paths["status"], private=True)
            value.update(last_attempt=datetime.now(UTC).isoformat(), error_code=code)
            transaction.stage(paths["status"], refresh._json(value), expected=before)
            transaction.commit()
    except (OSError, RuntimeError):
        # A locked, damaged or conflicted enrollment must not be overwritten.
        pass


def run(*, home=None):
    """One bounded attempt with jitter; no retry loop, login, or client restart."""
    time.sleep(secrets.randbelow(MAX_JITTER_SECONDS + 1))
    try:
        refresh.refresh(home=home)
        return 0
    except (OSError, RuntimeError) as error:
        code = (
            "sign_in_required"
            if isinstance(error, SignInRequired)
            else "access_unavailable"
            if isinstance(error, AccessUnavailable)
            else "refresh_failed"
        )
        _record_failure(home or Path.home(), code)
        return 1
