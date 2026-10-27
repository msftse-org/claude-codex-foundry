"""Installer flow with stubbed uv/runtime; never touches the real Claude or Codex settings."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_configuration_client import descriptor_value

from claude_foundry.config import export_client
from claude_foundry.configuration_client import AZURE_CLI_CLIENT_ID

ROOT = Path(__file__).resolve().parents[1]
CLIENT_ID = "00000000-0000-0000-0000-000000000004"
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="The installer supports macOS")


def codex(org, **changes):
    profile = export_client(
        {
            **org,
            "client_type": "codex",
            "gateway_url": "https://test-apim.azure-api.net/codex/v1",
            "models": [{"name": "gpt-5.3-codex", "model": "gpt-5.3-codex", "qualified": True}],
            "default_model": "gpt-5.3-codex",
        },
        "msal",
        CLIENT_ID,
    )
    profile.update(changes)
    if profile["auth_mode"] == "azure-cli":
        profile.pop("client_id")
    return profile


def install(
    tmp_path,
    profiles,
    args=(),
    codex_installed=False,
    fail_login=False,
    fail_command="",
    expected_code=0,
    azure_cli_installed=True,
):
    bundle = tmp_path / "bundle"
    (bundle / "scripts/macos").mkdir(parents=True)
    # Do not discover the operator's real Homebrew executables in isolated tests.
    (bundle / "scripts/macos/install.sh").write_text(
        (ROOT / "scripts/macos/install.sh")
        .read_text()
        .replace("/opt/homebrew/bin:/usr/local/bin:", "")
    )
    (bundle / "scripts/macos/install.sh").chmod(0o755)
    shutil.copy2(ROOT / "Install.command", bundle / "Install.command")
    (bundle / "requirements-auth.lock").write_text("")
    for name, profile in profiles.items():
        (bundle / name).write_text(json.dumps(profile))
    home, log = tmp_path / "home", tmp_path / "calls.log"
    (home / ".local/bin").mkdir(parents=True)
    uv = home / ".local/bin/uv"
    uv.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == venv ]]; then\n'
        '  runtime="${@: -1}"; mkdir -p "$runtime/bin"\n'
        f'  ln -sf "{sys.executable}" "$runtime/bin/python"\n'
        f'  printf \'#!/bin/bash\\necho "$*" >> {log}\\n\' > "$runtime/bin/claude-foundry"\n'
        '  if [[ "${FAIL_LOGIN:-}" == 1 ]]; then printf \'if [[ "$1" == login ]]; then echo "admin approval required" >&2; exit 1; fi\\n\' >> "$runtime/bin/claude-foundry"; fi\n'
        '  printf \'if [[ "$1" == "${FAIL_COMMAND:-}" ]]; then exit 1; fi\\n\' >> "$runtime/bin/claude-foundry"\n'
        '  chmod +x "$runtime/bin/claude-foundry"\n'
        "fi\n"
    )
    uv.chmod(0o755)
    if azure_cli_installed:
        az = home / ".local/bin/az"
        az.write_text("#!/bin/bash\nexit 99\n")
        az.chmod(0o755)  # The installer must delegate sign-in, never run az itself.
    codex_app = tmp_path / "codex"
    if codex_installed:
        codex_app.write_text("")
        codex_app.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "CLAUDE_FOUNDRY_CODEX_APP": str(codex_app),
        "CLAUDE_FOUNDRY_CODEX_CLI": "codex-absent-in-test",
        "FAIL_LOGIN": "1" if fail_login else "0",
        "FAIL_COMMAND": fail_command,
        "PYTHONPATH": str(ROOT / "src"),
    }
    result = subprocess.run(
        ["/bin/bash", str(bundle / "Install.command"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == expected_code, result.stderr
    calls = [
        line.replace(str(bundle) + "/", "")
        for line in (log.read_text().splitlines() if log.exists() else [])
    ]
    return calls, result


def test_combined_bundle_signs_in_once_and_configures_both(org, tmp_path):
    profiles = {
        "client.json": export_client(org, "msal", CLIENT_ID),
        "codex-client.json": codex(org),
    }
    calls, result = install(tmp_path, profiles, codex_installed=True)
    assert calls == [
        "login --config client.json",
        "install --config client.json",
        "install --config codex-client.json",
    ]
    assert "--client claude;" in result.stdout and "--client codex" in result.stdout


def test_combined_bundle_skips_codex_when_it_is_not_installed(org, tmp_path):
    profiles = {
        "client.json": export_client(org, "msal", CLIENT_ID),
        "codex-client.json": codex(org),
    }
    calls, result = install(tmp_path, profiles, args=["--enable-history-import"])
    assert calls == [
        "login --config client.json",
        "install --config client.json --enable-history-import",
    ]
    assert "Codex is not installed" in result.stderr
    assert "--client codex" not in result.stdout


def test_distinct_identities_each_sign_in(org, tmp_path):
    profiles = {
        "client.json": export_client(org, "msal", CLIENT_ID),
        "codex-client.json": codex(org, auth_mode="azure-cli"),
    }
    calls, _ = install(tmp_path, profiles, codex_installed=True)
    assert [c for c in calls if c.startswith("login")] == [
        "login --config client.json",
        "login --config codex-client.json",
    ]


def test_single_profile_bundle_is_unchanged(org, tmp_path):
    calls, result = install(tmp_path, {"client.json": export_client(org)})
    assert calls == ["login --config client.json", "install --config client.json"]
    assert result.stdout.strip().endswith("restore --client claude")
    assert "SETUP COMPLETE. User actions:" in result.stdout
    assert "fully quit claude" in result.stdout
    assert "Restart the app" not in result.stderr


def test_explicit_codex_profile_is_never_skipped(org, tmp_path):
    profiles = {"client.json": export_client(org), "codex.json": codex(org)}
    calls, _ = install(tmp_path, profiles, args=["--config", str(tmp_path / "bundle/codex.json")])
    assert calls == ["login --config codex.json", "install --config codex.json"]


def test_failed_login_explains_that_setup_did_not_complete(org, tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "scripts/macos").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/macos/install.sh", bundle / "scripts/macos/install.sh")
    shutil.copy2(ROOT / "Install.command", bundle / "Install.command")
    (bundle / "requirements-auth.lock").write_text("")
    (bundle / "client.json").write_text(json.dumps(codex(org)))
    home, log = tmp_path / "home", tmp_path / "calls.log"
    (home / ".local/bin").mkdir(parents=True)
    uv = home / ".local/bin/uv"
    uv.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == venv ]]; then\n'
        '  runtime="${@: -1}"; mkdir -p "$runtime/bin"\n'
        f'  ln -sf "{sys.executable}" "$runtime/bin/python"\n'
        f'  printf \'#!/bin/bash\\necho "$*" >> {log}\\nif [[ "$1" == login ]]; then exit 1; fi\\n\' > "$runtime/bin/claude-foundry"\n'
        '  chmod +x "$runtime/bin/claude-foundry"\n'
        "fi\n"
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(bundle / "Install.command")],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "CLAUDE_FOUNDRY_CODEX_APP": str(tmp_path / "codex"),
            "CLAUDE_FOUNDRY_CODEX_CLI": "codex-absent-in-test",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "SETUP DID NOT COMPLETE" in result.stderr
    assert "restarting it will not activate this organization" in result.stderr
    assert "adminconsent?client_id=" in result.stderr
    assert "Restart the app only after this installer prints 'SETUP COMPLETE'" in result.stderr


def enrollment_files(clients=None):
    return {
        "enrollment.json": descriptor_value(),
        "installation.json": {"schema_version": 1, "clients": clients or ["claude"]},
    }


def test_enrollment_installer_uses_fixed_entrypoint_without_refresh_choice(tmp_path):
    calls, result = install(tmp_path, enrollment_files())
    assert calls == ["enroll --descriptor enrollment.json --client claude"]
    assert "SETUP COMPLETE" in result.stdout
    assert "Automatic model updates are active" in result.stdout
    assert "enable-refresh" not in result.stdout and "disable-refresh" not in result.stdout
    assert "restore --client" not in result.stdout


def test_cli_enrollment_requires_approved_cli_before_client_changes(tmp_path):
    files = enrollment_files()
    files["enrollment.json"].update(schema_version=3, public_client_id=AZURE_CLI_CLIENT_ID)
    calls, result = install(tmp_path, files, azure_cli_installed=False, expected_code=1)
    assert calls == []
    assert "Azure CLI sign-in" in result.stderr
    assert "No client settings were changed" in result.stderr


def test_invalid_enrollment_descriptor_stops_before_client_changes(tmp_path):
    files = enrollment_files()
    files["enrollment.json"]["public_client_id"] = "untrusted"
    calls, result = install(tmp_path, files, expected_code=1)
    assert calls == []
    assert "enrollment descriptor is invalid" in result.stderr


def test_enrollment_installer_both_apps_requires_only_explicit_migration(tmp_path):
    calls, result = install(
        tmp_path,
        enrollment_files(["codex", "claude"]),
        args=["--migrate-existing"],
        codex_installed=True,
    )
    assert calls == [
        "enroll --descriptor enrollment.json --client claude --client codex "
        f"--codex-binary {tmp_path}/codex --migrate-existing",
    ]
    assert "SETUP COMPLETE" in result.stdout
    assert "Automatic model updates are active" in result.stdout


def test_enrollment_installer_has_no_refresh_opt_in_flag(tmp_path):
    calls, result = install(
        tmp_path, enrollment_files(), args=["--enable-refresh"], expected_code=2
    )
    assert calls == []
    assert "Usage:" in result.stderr and "--enable-refresh" not in result.stderr


@pytest.mark.parametrize("flag", ["--no-login", "--enable-history-import"])
def test_enrollment_cannot_skip_signin_or_enable_history(tmp_path, flag):
    calls, _ = install(tmp_path, enrollment_files(), args=[flag], expected_code=2)
    assert calls == []


def test_ambiguous_bundle_never_selects_enrollment_or_legacy_implicitly(org, tmp_path):
    calls, result = install(
        tmp_path,
        {**enrollment_files(), "client.json": export_client(org)},
        expected_code=2,
    )
    assert calls == [] and "Ambiguous bundle" in result.stderr


@pytest.mark.parametrize(
    "selection",
    [
        {"schema_version": True, "clients": ["claude"]},
        {"schema_version": 1, "clients": ["claude", "claude"]},
        {"schema_version": 1, "clients": ["claude"], "command": "unexpected"},
    ],
)
def test_invalid_installation_selection_never_runs_enrollment(tmp_path, selection):
    calls, result = install(
        tmp_path, {**enrollment_files(), "installation.json": selection}, expected_code=1
    )
    assert calls == [] and "SETUP DID NOT COMPLETE" in result.stderr


def test_selected_missing_codex_is_not_silently_skipped(tmp_path):
    calls, result = install(tmp_path, enrollment_files(["codex"]), expected_code=1)
    assert calls == [] and "approved Codex" in result.stderr


def test_enrollment_failure_never_reports_setup_complete(tmp_path):
    calls, result = install(
        tmp_path,
        enrollment_files(),
        fail_command="enroll",
        expected_code=1,
    )
    assert len(calls) == 1
    assert "SETUP COMPLETE" not in result.stdout
    assert "SETUP DID NOT COMPLETE" in result.stderr
    assert "background-job error" in result.stderr
    assert "Client settings may already be configured" in result.stderr
