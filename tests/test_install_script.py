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


def install(tmp_path, profiles, args=(), codex_installed=False):
    bundle = tmp_path / "bundle"
    (bundle / "scripts/macos").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/macos/install.sh", bundle / "scripts/macos/install.sh")
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
    }
    result = subprocess.run(
        ["/bin/bash", str(bundle / "Install.command"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    calls = [line.replace(str(bundle) + "/", "") for line in log.read_text().splitlines()]
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


def test_explicit_codex_profile_is_never_skipped(org, tmp_path):
    profiles = {"client.json": export_client(org), "codex.json": codex(org)}
    calls, _ = install(tmp_path, profiles, args=["--config", str(tmp_path / "bundle/codex.json")])
    assert calls == ["login --config codex.json", "install --config codex.json"]
