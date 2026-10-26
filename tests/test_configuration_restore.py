import json
import plistlib

import pytest
import tomlkit
from test_configuration_refresh import pilot as pilot
from test_configuration_refresh import read, settings, write
from test_configuration_schedule import launchctl as launchctl

from claude_foundry import cli, local_update
from claude_foundry import configuration_refresh as refresh
from claude_foundry import configuration_schedule as schedule
from claude_foundry.configuration_client import AZURE_CLI_CLIENT_ID, ConfigurationError


@pytest.mark.parametrize("after_refresh", [False, True])
def test_unenroll_removes_only_owned_files_and_retains_credentials_and_history(
    pilot, launchctl, after_refresh
):
    home, _, paths, _, enroll = pilot
    history = home / ".codex/sessions/original.jsonl"
    credential = home / refresh.STATE / "credentials/opaque-store-marker"
    (home / refresh.STATE).mkdir(parents=True, mode=0o700)
    write(history, b"original history\n")
    write(credential, b"not an access token")
    enroll()
    if after_refresh:
        refresh.refresh(home=home)
    result = refresh.restore(home=home)
    assert result == {
        "state": "restored",
        "automatic_refresh": False,
        "credentials": "preserved_in_encrypted_store",
        "history": "preserved",
    }
    assert settings(paths).keys() == {"restored"}
    assert read(paths["restored"])["binding"]["clients"] == ["claude", "codex"]
    assert paths["restored"].stat().st_mode & 0o777 == 0o600
    assert history.read_bytes() == b"original history\n"
    assert credential.read_bytes() == b"not an access token"
    enroll()
    assert paths["binding"].exists() and paths["helper"].exists()


def test_cli_unenrollment_reports_provider_owned_cache(pilot, launchctl, monkeypatch):
    home, descriptor, _, _, enroll = pilot
    value = read(descriptor)
    value.update(schema_version=3, public_client_id=AZURE_CLI_CLIENT_ID)
    write(descriptor, value)
    monkeypatch.setattr(refresh, "EnrolledAzureCli", refresh.EnrolledMsal)
    enroll()
    result = refresh.restore(home=home)
    assert result["credentials"] == "preserved_in_azure_cli"
    assert not (home / refresh.STATE / "credentials").exists()


def test_unenroll_returns_exact_initial_settings_when_no_later_edits(pilot, launchctl):
    home, _, paths, _, enroll = pilot
    originals = {
        "claude_cli": b'{ "theme": "dark", "env": {"CUSTOM": "keep"} }\n',
        "claude_meta": b'{"entries": [{"id": "existing", "name": "Other"}], "appliedId": "existing"}',
        "claude_mode": b'{"deploymentMode": "firstParty", "custom": true}',
        "codex": b'# keep this comment\npersonality = "pragmatic"\n',
    }
    for name, body in originals.items():
        write(paths[name], body)
    enroll()
    refresh.refresh(home=home)
    refresh.restore(home=home)
    for name, body in originals.items():
        assert paths[name].read_bytes() == body


@pytest.mark.parametrize("refresh_between", [False, True])
def test_user_edits_survive_restore_even_when_refresh_adopted_them(
    pilot, launchctl, refresh_between
):
    home, _, paths, _, enroll = pilot
    enroll()
    value = read(paths["claude_cli"])
    value.update(theme="dark", custom={"nested": "keep"})
    value["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "user-choice"
    write(paths["claude_cli"], value)
    value = read(paths["claude_profile"])
    value["customPreference"] = "keep"
    write(paths["claude_profile"], value)
    value = tomlkit.parse(paths["codex"].read_text())
    value["personality"] = "pragmatic"
    write(paths["codex"], tomlkit.dumps(value).encode())
    if refresh_between:
        refresh.refresh(home=home)
    refresh.restore(home=home)
    assert read(paths["claude_cli"]) == {
        "theme": "dark",
        "custom": {"nested": "keep"},
        "env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "user-choice"},
    }
    assert read(paths["claude_profile"]) == {"customPreference": "keep"}
    assert tomlkit.parse(paths["codex"].read_text()).unwrap() == {"personality": "pragmatic"}


def test_later_desktop_profiles_and_selection_are_preserved(pilot, launchctl):
    home, _, paths, _, enroll = pilot
    enroll()
    meta = read(paths["claude_meta"])
    meta["entries"].append({"id": "new-personal-profile", "name": "Personal"})
    meta["appliedId"] = "new-personal-profile"
    write(paths["claude_meta"], meta)
    refresh.refresh(home=home)
    refresh.restore(home=home)
    assert read(paths["claude_meta"]) == {
        "entries": [{"id": "new-personal-profile", "name": "Personal"}],
        "appliedId": "new-personal-profile",
    }


def test_deleted_and_empty_user_preferences_are_not_resurrected(pilot, launchctl):
    home, _, paths, _, enroll = pilot
    write(paths["claude_cli"], {"theme": "light", "custom": {"remove": True}})
    enroll()
    value = read(paths["claude_cli"])
    value.pop("theme")
    value["custom"] = {}
    value["newEmpty"] = {}
    write(paths["claude_cli"], value)
    refresh.refresh(home=home)
    refresh.restore(home=home)
    assert read(paths["claude_cli"]) == {"custom": {}, "newEmpty": {}}


@pytest.mark.parametrize(
    "target", ["helper", "catalog", "claude_cli", "claude_profile", "codex", "claude_meta"]
)
def test_modified_route_helper_catalog_or_library_entry_requires_review_without_pause(
    pilot, launchctl, target
):
    home, _, paths, _, enroll = pilot
    enroll()
    schedule.configure(enabled=True, home=home)
    if target in {"helper", "catalog"}:
        paths[target].write_bytes(b"local edit")
    elif target == "codex":
        value = tomlkit.parse(paths[target].read_text())
        value["model_providers"]["azure-foundry"]["auth"]["command"] = "/user/helper"
        write(paths[target], tomlkit.dumps(value).encode())
    else:
        value = read(paths[target])
        if target == "claude_cli":
            value["apiKeyHelper"] = "/user/helper"
        elif target == "claude_profile":
            value["inferenceGatewayBaseUrl"] = "https://other.example.com"
        else:
            value["entries"][0]["name"] = "user edited enrolled entry"
        write(paths[target], value)
    before = settings(paths)
    launchctl[0].clear()
    with pytest.raises(ConfigurationError):
        refresh.restore(home=home)
    assert settings(paths) == before and launchctl[0] == []
    assert len(launchctl[1]) == 1


@pytest.mark.parametrize("bad_value", [{"entries": [None]}, {"env": []}, {"applied": []}])
def test_malformed_recovery_input_fails_without_settings_changes(pilot, launchctl, bad_value):
    home, _, paths, _, enroll = pilot
    enroll()
    name = (
        "claude_meta"
        if "entries" in bad_value
        else "claude_cli"
        if "env" in bad_value
        else "ownership"
    )
    value = read(paths[name])
    value.update(bad_value)
    write(paths[name], value)
    before = settings(paths)
    with pytest.raises(ConfigurationError):
        refresh.restore(home=home)
    assert settings(paths) == before and not launchctl[0]


def test_unenroll_pauses_job_before_settings_removal(pilot, launchctl, monkeypatch):
    home, _, paths, _, enroll = pilot
    enroll()
    schedule.configure(enabled=True, home=home)
    real_replace = local_update._replace

    def replace(path, value):
        if path == paths["helper"]:
            assert not launchctl[1]
            assert plistlib.loads(schedule._paths(home)[1].read_bytes())["Disabled"]
        real_replace(path, value)

    monkeypatch.setattr(local_update, "_replace", replace)
    refresh.restore(home=home)
    assert not launchctl[1] and not paths["helper"].exists()


def test_unload_failure_preserves_every_settings_file(pilot, launchctl, monkeypatch):
    home, _, paths, _, enroll = pilot
    enroll()
    schedule.configure(enabled=True, home=home)
    before = settings(paths)
    monkeypatch.setattr(schedule, "_launchctl", lambda *args: 4 if args[0] == "bootout" else 0)
    with pytest.raises(ConfigurationError, match="Could not pause"):
        refresh.restore(home=home)
    assert settings(paths) == before


def test_transaction_failure_restores_settings_but_leaves_job_paused(pilot, launchctl, monkeypatch):
    home, _, paths, _, enroll = pilot
    enroll()
    schedule.configure(enabled=True, home=home)
    before = settings(paths)
    real_replace = local_update._replace
    failed = False

    def replace(path, value):
        nonlocal failed
        if path == paths["helper"] and value is None and not failed:
            failed = True
            raise OSError("simulated failure")
        real_replace(path, value)

    monkeypatch.setattr(local_update, "_replace", replace)
    with pytest.raises(OSError, match="simulated"):
        refresh.restore(home=home)
    assert settings(paths) == before and not launchctl[1]
    assert plistlib.loads(schedule._paths(home)[1].read_bytes())["Disabled"]
    refresh.restore(home=home)
    assert not paths["binding"].exists()


def test_unowned_loaded_job_prevents_restore(pilot, launchctl):
    home, _, paths, _, enroll = pilot
    enroll()
    launchctl[1].add(f"gui/{schedule.os.getuid()}/{schedule.LABEL}")
    before = settings(paths)
    with pytest.raises(ConfigurationError, match="unowned"):
        refresh.restore(home=home)
    assert settings(paths) == before


def test_cli_unenroll_dispatch_and_legacy_restore_guard(pilot, monkeypatch, capsys):
    home, _, _, _, enroll = pilot
    enroll()
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.Path, "home", lambda: home)
    monkeypatch.setattr(cli.sys, "argv", ["claude-foundry", "restore"])
    assert cli.main() == 1
    assert "Use unenroll" in capsys.readouterr().err
    monkeypatch.setattr(cli.sys, "argv", ["claude-foundry", "unenroll"])
    monkeypatch.setattr(refresh, "restore", lambda: {"state": "restored"})
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "restored"
