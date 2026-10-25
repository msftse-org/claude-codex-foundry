import json
import plistlib
import subprocess

import pytest
from test_configuration_refresh import pilot as pilot
from test_configuration_refresh import read, settings, write

from claude_foundry import configuration_schedule as schedule
from claude_foundry.configuration_client import (
    AccessUnavailable,
    ConfigurationError,
    SignInRequired,
)


@pytest.fixture
def launchctl(monkeypatch):
    calls = []
    loaded = set()

    def command(*args):
        calls.append(args)
        if args[0] == "print":
            return 0 if args[1] in loaded else 3
        if args[0] == "bootstrap":
            loaded.add(f"{args[1]}/{schedule.LABEL}")
        elif args[0] == "bootout":
            loaded.remove(args[1])
        else:
            pytest.fail("Unexpected launchctl command")
        return 0

    monkeypatch.setattr(schedule, "_launchctl", command)
    return calls, loaded


def test_enable_uses_only_fixed_local_job_and_preserves_client_files(pilot, launchctl):
    home, _, paths, _, enroll = pilot
    enroll()
    before = settings(paths)
    result = schedule.configure(enabled=True, home=home)
    assert result == {"automatic_refresh": True, "interval_seconds": 900}
    state, plist, record = schedule._paths(home)
    value = plistlib.loads(plist.read_bytes())
    binding = read(paths["binding"])
    assert value["ProgramArguments"] == [binding["entrypoint"], "refresh-background"]
    assert value["StartInterval"] == 900 and value["RunAtLoad"]
    assert not value["Disabled"]
    assert value["StandardOutPath"] == value["StandardErrorPath"] == "/dev/null"
    assert value["EnvironmentVariables"]["PATH"].startswith(f"{home}/.local/bin:")
    assert "https:" not in json.dumps(value) and "token" not in json.dumps(value)
    assert plist.stat().st_mode & 0o777 == record.stat().st_mode & 0o777 == 0o600
    assert state.stat().st_mode & 0o777 == 0o700
    assert settings(paths) == before
    assert [call[0] for call in launchctl[0]] == ["print", "bootstrap"]


def test_enable_is_idempotent_pause_unloads_and_reenable_loads(pilot, launchctl):
    home, _, _, _, enroll = pilot
    enroll()
    schedule.configure(enabled=True, home=home)
    launchctl[0].clear()
    schedule.configure(enabled=True, home=home)
    assert [c[0] for c in launchctl[0]] == ["print"]
    schedule.configure(enabled=False, home=home)
    _, plist, record = schedule._paths(home)
    assert plistlib.loads(plist.read_bytes())["Disabled"]
    assert not launchctl[1]
    assert record.exists()  # Pausing is not uninstalling.
    schedule.configure(enabled=False, home=home)
    schedule.configure(enabled=True, home=home)
    assert len(launchctl[1]) == 1
    assert [c[0] for c in launchctl[0]].count("bootout") == 1
    assert [c[0] for c in launchctl[0]].count("bootstrap") == 1


def test_no_job_before_successful_enrollment(pilot, launchctl):
    home, _, _, _, _ = pilot
    with pytest.raises(ConfigurationError):
        schedule.configure(enabled=True, home=home)
    assert not schedule._paths(home)[1].exists() and not launchctl[0]


@pytest.mark.parametrize("tamper", ["unowned", "modified", "missing", "symlink"])
def test_conflicting_job_requires_review_without_overwrite(pilot, launchctl, tamper, tmp_path):
    home, _, _, _, enroll = pilot
    enroll()
    _, plist, record = schedule._paths(home)
    if tamper == "unowned":
        write(plist, b"not ours")
    else:
        schedule.configure(enabled=True, home=home)
        if tamper == "modified":
            write(plist, b"user changed this")
        elif tamper == "missing":
            plist.unlink()
        else:
            destination = tmp_path / "other.plist"
            write(destination, b"other")
            plist.unlink()
            plist.symlink_to(destination)
    launchctl[0].clear()
    before = plist.read_bytes() if plist.exists() else None
    with pytest.raises(ConfigurationError):
        schedule.configure(enabled=True, home=home)
    assert (plist.read_bytes() if plist.exists() else None) == before
    assert not launchctl[0]
    if tamper == "unowned":
        assert not record.exists()


def test_load_failure_keeps_owned_job_for_explicit_retry(pilot, launchctl, monkeypatch):
    home, _, _, _, enroll = pilot
    enroll()
    command = schedule._launchctl
    monkeypatch.setattr(
        schedule, "_launchctl", lambda *args: 5 if args[0] == "bootstrap" else command(*args)
    )
    with pytest.raises(ConfigurationError, match="saved but could not be loaded"):
        schedule.configure(enabled=True, home=home)
    assert schedule._paths(home)[1].is_file() and schedule._paths(home)[2].is_file()
    monkeypatch.setattr(schedule, "_launchctl", command)
    schedule.configure(enabled=True, home=home)
    assert len(launchctl[1]) == 1


def test_bootout_failure_preserves_job(pilot, launchctl, monkeypatch):
    home, _, _, _, enroll = pilot
    enroll()
    schedule.configure(enabled=True, home=home)
    before = schedule._paths(home)[1].read_bytes()
    monkeypatch.setattr(schedule, "_launchctl", lambda *args: 4 if args[0] == "bootout" else 0)
    with pytest.raises(ConfigurationError, match="Could not pause"):
        schedule.configure(enabled=False, home=home)
    assert schedule._paths(home)[1].read_bytes() == before


def test_background_success_is_noninteractive_and_has_bounded_jitter(pilot, monkeypatch):
    home, _, _, calls, enroll = pilot
    enroll()
    calls.clear()
    waits = []
    monkeypatch.setattr(schedule.secrets, "randbelow", lambda limit: limit - 1)
    monkeypatch.setattr(schedule.time, "sleep", waits.append)
    assert schedule.run(home=home) == 0
    assert waits == [60] and ("login",) not in calls


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SignInRequired("private subject, token, URL"), "sign_in_required"),
        (AccessUnavailable("private subject, token, URL"), "access_unavailable"),
        (ConfigurationError("private subject, token, URL"), "refresh_failed"),
        (OSError("private subject, token, URL"), "refresh_failed"),
    ],
)
def test_background_failure_only_updates_safe_status(pilot, monkeypatch, error, code):
    home, _, paths, calls, enroll = pilot
    enroll()
    calls.clear()
    before = settings(paths)
    monkeypatch.setattr(schedule.time, "sleep", lambda _: None)

    def fail(**_):
        raise error

    monkeypatch.setattr(schedule.refresh, "refresh", fail)
    assert schedule.run(home=home) == 1
    after = settings(paths)
    assert {k: v for k, v in before.items() if k != "status"} == {
        k: v for k, v in after.items() if k != "status"
    }
    status = read(paths["status"])
    assert status["last_success"] == json.loads(before["status"])["last_success"]
    assert status["error_code"] == code and status["last_attempt"]
    assert b"private subject" not in after["status"] and ("login",) not in calls


def test_launchctl_is_bounded_quiet_and_uses_absolute_executable(monkeypatch):
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(schedule.subprocess, "run", command)
    assert schedule._launchctl("print", "gui/1/example") == 0
    args, kwargs = calls[0]
    assert args == ["/bin/launchctl", "print", "gui/1/example"]
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 20,
    }
