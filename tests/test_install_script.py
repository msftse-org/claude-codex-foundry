"""Installer flow with stubbed uv/runtime; never touches the real Claude or Codex settings."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claude_foundry.config import export_client

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
    fail_bootstrap="",
    bootstrap_uv=False,
):
    bundle = tmp_path / "bundle"
    (bundle / "scripts/macos").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/macos/install.sh", bundle / "scripts/macos/install.sh")
    shutil.copy2(ROOT / "Install.command", bundle / "Install.command")
    (bundle / "requirements-auth.lock").write_text("")
    for name, profile in profiles.items():
        (bundle / name).write_text(json.dumps(profile))
    home, log = tmp_path / "home", tmp_path / "calls.log"
    (home / ".local/bin").mkdir(parents=True, exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    bootstrap_uv = bootstrap_uv or fail_bootstrap in {"curl", "uv install"}
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        "command() {\n"
        '  if [[ "$*" == "-v uv" && "$BOOTSTRAP_UV" == 1 ]]; then return 1; fi\n'
        '  builtin command "$@"\n'
        "}\n"
    )
    curl = home / ".local/bin/curl"
    curl.write_text(
        "#!/bin/bash\n"
        'echo "curl $*" >> "$BOOTSTRAP_LOG"\n'
        'if [[ "$BOOTSTRAP_UV" != 1 || "$FAIL_BOOTSTRAP" == curl ]]; then\n'
        '  echo "stubbed curl failure" >&2; exit 23\n'
        "fi\n"
        "cat > \"${@: -1}\" <<'EOF'\n"
        "#!/bin/sh\n"
        'echo "uv install" >> "$BOOTSTRAP_LOG"\n'
        'if [ "$FAIL_BOOTSTRAP" = "uv install" ]; then\n'
        '  echo "stubbed uv install failure" >&2; exit 23\n'
        "fi\n"
        "EOF\n"
    )
    curl.chmod(0o755)
    uv = home / ".local/bin/uv"
    uv.write_text(
        "#!/bin/bash\n"
        'echo "$*" >> "$BOOTSTRAP_LOG"\n'
        'if [[ "$FAIL_BOOTSTRAP" == "$1" || "$FAIL_BOOTSTRAP" == "$1 ${2:-}" ]]; then\n'
        '  echo "stubbed $FAIL_BOOTSTRAP failure" >&2; exit 23\n'
        "fi\n"
        'if [[ "$1" == venv ]]; then\n'
        '  runtime="${@: -1}"; mkdir -p "$runtime/bin"\n'
        f'  ln -sf "{sys.executable}" "$runtime/bin/python"\n'
        '  printf \'#!/bin/bash\\necho "$*" >> "$CALLS_LOG"\\n\' > "$runtime/bin/claude-foundry"\n'
        '  if [[ "${FAIL_LOGIN:-}" == 1 ]]; then printf \'if [[ "$1" == login ]]; then echo "admin approval required" >&2; exit 1; fi\\n\' >> "$runtime/bin/claude-foundry"; fi\n'
        '  chmod +x "$runtime/bin/claude-foundry"\n'
        "fi\n"
    )
    uv.chmod(0o755)
    codex_app = tmp_path / "codex"
    if codex_installed:
        codex_app.write_text("")
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "CLAUDE_FOUNDRY_CODEX_APP": str(codex_app),
        "CLAUDE_FOUNDRY_CODEX_CLI": "codex-absent-in-test",
        "FAIL_LOGIN": "1" if fail_login else "0",
        "FAIL_BOOTSTRAP": fail_bootstrap,
        "BOOTSTRAP_UV": "1" if bootstrap_uv else "0",
        "BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        "CALLS_LOG": str(log),
        "BASH_ENV": str(bash_env),
        "TMPDIR": str(work),
    }
    result = subprocess.run(
        ["/bin/bash", str(bundle / "Install.command"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert not any(work.iterdir())
    if fail_login or fail_bootstrap:
        assert result.returncode != 0, result.stdout
    else:
        assert result.returncode == 0, result.stderr
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
    _, result = install(tmp_path, {"client.json": codex(org)}, fail_login=True)
    assert result.returncode == 1
    assert "SETUP DID NOT COMPLETE" in result.stderr
    assert "restarting it will not activate this organization" in result.stderr
    assert "adminconsent?client_id=" in result.stderr
    assert "Restart the app only after this installer prints 'SETUP COMPLETE'" in result.stderr


@pytest.mark.parametrize(
    "fail_bootstrap", ["curl", "uv install", "venv", "pip sync", "pip install"]
)
@pytest.mark.parametrize("existing_settings", [False, True])
def test_failed_bootstrap_explains_recovery_without_changing_clients(
    org, tmp_path, fail_bootstrap, existing_settings
):
    home = tmp_path / "home"
    settings = [
        home / ".claude/settings.json",
        home / ".codex/config.toml",
        home / "Library/Application Support/Claude/claude_desktop_config.json",
        home / "Library/Application Support/Claude-3p/configLibrary/organization.json",
    ]
    if existing_settings:
        for path in settings:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("existing client settings\n")
    calls, result = install(
        tmp_path, {"client.json": export_client(org)}, fail_bootstrap=fail_bootstrap
    )
    assert calls == []
    assert result.stderr.count("SETUP DID NOT COMPLETE") == 1
    assert f"stubbed {fail_bootstrap} failure" in result.stderr
    assert "Claude Code, Codex and Claude Desktop settings are unchanged" in result.stderr
    for host in ("astral.sh", "releases.astral.sh", "pypi.org", "files.pythonhosted.org"):
        assert host in result.stderr
    assert "Rerunning the same command is safe" in result.stderr
    assert "IT team" in result.stderr
    assert "proxy settings" in result.stderr
    assert "organization-approved offline bundle" in result.stderr
    assert "SETUP COMPLETE" not in result.stdout
    if existing_settings:
        assert all(path.read_text() == "existing client settings\n" for path in settings)
    else:
        assert not (home / ".claude").exists()
        assert not (home / ".codex").exists()
        assert not (home / "Library").exists()


@pytest.mark.parametrize("bootstrap_uv", [False, True])
def test_bootstrap_preserves_secure_downloads_and_existing_uv(org, tmp_path, bootstrap_uv):
    calls, result = install(
        tmp_path, {"client.json": export_client(org)}, bootstrap_uv=bootstrap_uv
    )
    bootstrap = (tmp_path / "bootstrap.log").read_text()
    assert "venv --python 3.12 --managed-python" in bootstrap
    assert "pip sync --python" in bootstrap
    assert "--require-hashes" in bootstrap
    assert "pip install --python" in bootstrap and "--no-deps" in bootstrap
    for unsafe in ("--trusted-host", "--insecure", "--no-verify"):
        assert unsafe not in bootstrap
    if bootstrap_uv:
        assert (
            "curl --proto =https --tlsv1.2 -fsSL https://astral.sh/uv/0.11.13/install.sh"
            in bootstrap
        )
        assert "uv install\n" in bootstrap
    else:
        assert "curl " not in bootstrap
        assert "uv install\n" not in bootstrap
    assert calls == ["login --config client.json", "install --config client.json"]
    assert "SETUP COMPLETE" in result.stdout


def test_dependency_download_failure_can_be_rerun_safely(org, tmp_path):
    profiles = {"client.json": export_client(org)}
    calls, failed = install(tmp_path, profiles, fail_bootstrap="pip sync")
    assert calls == []
    assert "SETUP DID NOT COMPLETE" in failed.stderr
    calls, succeeded = install(tmp_path, profiles)
    assert calls == ["login --config client.json", "install --config client.json"]
    assert "SETUP COMPLETE" in succeeded.stdout
    bootstrap = (tmp_path / "bootstrap.log").read_text()
    assert bootstrap.count("venv ") == 1
    assert bootstrap.count("pip sync ") == 2
