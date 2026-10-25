import json
import os

import pytest

from claude_foundry import local_update as update
from claude_foundry.configuration_client import ConfigurationError


def stage(transaction, path, body):
    transaction.stage(path, body, expected=path.read_bytes() if path.exists() else None)


def test_update_is_staged_atomic_private_and_unchanged_files_are_not_written(tmp_path):
    state, one, two = tmp_path / "state", tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"before")
    one.chmod(0o600)
    with update.FileTransaction(state, [one, two]) as transaction:
        stage(transaction, one, b"after")
        stage(transaction, two, b"new helper")
        assert one.read_bytes() == b"before" and not two.exists()
        assert transaction.commit()
    assert one.read_bytes() == b"after" and two.read_bytes() == b"new helper"
    assert one.stat().st_mode & 0o777 == two.stat().st_mode & 0o777 == 0o600
    assert not (state / "pending-update.json").exists()
    before = one.stat().st_mtime_ns
    with update.FileTransaction(state, [one]) as transaction:
        stage(transaction, one, b"after")
        assert not transaction.commit()
    assert one.stat().st_mtime_ns == before


def test_concurrent_refresh_cannot_acquire_lock(tmp_path):
    with update.FileTransaction(tmp_path / "state", []) as first:
        with pytest.raises(ConfigurationError, match="running"):
            with update.FileTransaction(tmp_path / "state", []):
                pytest.fail("Acquired a second lock")
        assert first.fd is not None
    with update.FileTransaction(tmp_path / "state", []):
        pass


def test_regular_failure_restores_all_originals_and_permissions(monkeypatch, tmp_path):
    state, one, two = tmp_path / "state", tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"original")
    one.chmod(0o644)
    real_replace = update._replace
    failed = False

    def fail_second(path, value):
        nonlocal failed
        if path == two and not failed:
            failed = True
            raise OSError("disk full")
        real_replace(path, value)

    monkeypatch.setattr(update, "_replace", fail_second)
    with update.FileTransaction(state, [one, two]) as transaction:
        stage(transaction, one, b"changed")
        stage(transaction, two, b"created")
        with pytest.raises(OSError, match="disk full"):
            transaction.commit()
    assert one.read_bytes() == b"original" and one.stat().st_mode & 0o777 == 0o644
    assert not two.exists() and not (state / "pending-update.json").exists()


def interrupted(state, files):
    state.mkdir(mode=0o700)
    record = {
        "schema_version": 1,
        "files": [
            {
                "path": str(path),
                "before": update._encode(before),
                "after": update._encode(after),
            }
            for path, before, after in files
        ],
    }
    journal = state / "pending-update.json"
    journal.write_text(json.dumps(record))
    journal.chmod(0o600)
    return journal


def test_next_run_recovers_interrupted_mixed_files_before_new_work(tmp_path):
    state, one, two = tmp_path / "state", tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"new")
    one.chmod(0o600)
    two.write_bytes(b"original")
    two.chmod(0o600)
    journal = interrupted(
        state,
        [
            (one, None, (b"new", 0o600)),
            (two, (b"original", 0o600), (b"candidate", 0o600)),
        ],
    )
    with update.FileTransaction(state, [one, two]):
        assert not one.exists()
        assert two.read_bytes() == b"original"
        assert not journal.exists()


def test_recovery_prechecks_all_files_and_preserves_post_crash_user_edits(tmp_path):
    state, one, two = tmp_path / "state", tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"new")
    one.chmod(0o600)
    two.write_bytes(b"later user edit")
    two.chmod(0o600)
    journal = interrupted(
        state,
        [
            (one, (b"old", 0o600), (b"new", 0o600)),
            (two, (b"original", 0o600), (b"candidate", 0o600)),
        ],
    )
    with pytest.raises(ConfigurationError, match="conflicts"):
        with update.FileTransaction(state, [one, two]):
            pass
    assert one.read_bytes() == b"new"
    assert two.read_bytes() == b"later user edit" and journal.exists()


def test_render_and_prepare_races_are_rejected_without_writes(tmp_path):
    path = tmp_path / "settings"
    path.write_bytes(b"later edit")
    path.chmod(0o600)
    with update.FileTransaction(tmp_path / "state", [path]) as transaction:
        with pytest.raises(ConfigurationError, match="rendering"):
            transaction.stage(path, b"candidate", expected=b"old")
        stage(transaction, path, b"candidate")
        path.write_bytes(b"another edit")
        with pytest.raises(ConfigurationError, match="preparation"):
            transaction.commit()
    assert path.read_bytes() == b"another edit"


def test_settings_and_credentials_cannot_be_updated_outside_explicit_target_set(tmp_path):
    path = tmp_path / "token-cache"
    with update.FileTransaction(tmp_path / "state", []) as transaction:
        with pytest.raises(ConfigurationError, match="target"):
            transaction.stage(path, b"not allowed", expected=None)
    assert not path.exists()


def test_journal_paths_are_not_authority_for_recovery(tmp_path):
    path, state = tmp_path / "unrelated-settings", tmp_path / "state"
    path.write_bytes(b"untouched")
    journal = interrupted(state, [(path, None, (b"untouched", 0o644))])
    with pytest.raises(ConfigurationError, match="journal"):
        with update.FileTransaction(state, []):
            pass
    assert path.read_bytes() == b"untouched" and journal.exists()


@pytest.mark.parametrize("kind", ["target", "parent", "state", "lock", "journal", "hardlink"])
def test_symlink_or_hardlink_substitution_rejected(tmp_path, kind):
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    target.chmod(0o600)
    state, path = tmp_path / "state", tmp_path / "settings"
    if kind == "target":
        path.symlink_to(target)
    elif kind == "parent":
        parent = tmp_path / "linked-parent"
        parent.symlink_to(tmp_path, target_is_directory=True)
        path = parent / "settings"
    elif kind == "state":
        state.symlink_to(tmp_path, target_is_directory=True)
    elif kind in {"lock", "journal"}:
        state.mkdir(mode=0o700)
        (state / ("update.lock" if kind == "lock" else "pending-update.json")).symlink_to(target)
    else:
        os.link(target, path)
    with pytest.raises((ConfigurationError, OSError)):
        with update.FileTransaction(state, [path]) as transaction:
            stage(transaction, path, b"candidate")
    assert target.read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        "not JSON",
        '{"schema_version":1,"schema_version":1,"files":[]}',
        '{"schema_version":true,"files":[]}',
        '{"schema_version":1,"files":[{"path":null,"before":null,"after":null}]}',
    ],
)
def test_malformed_journal_is_not_deleted_or_replayed(tmp_path, content):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    journal = state / "pending-update.json"
    journal.write_text(content)
    journal.chmod(0o600)
    with pytest.raises(ConfigurationError, match="journal"):
        with update.FileTransaction(state, []):
            pass
    assert journal.read_text() == content


def test_size_and_permission_limits_fail_before_changes(monkeypatch, tmp_path):
    path = tmp_path / "settings"
    with update.FileTransaction(tmp_path / "state", [path]) as transaction:
        with pytest.raises(ConfigurationError, match="content"):
            transaction.stage(path, b"too large", expected=None, mode=0o644)
        monkeypatch.setattr(update, "MAX_FILE_BYTES", 2)
        with pytest.raises(ConfigurationError, match="content"):
            transaction.stage(path, b"too large", expected=None)
        monkeypatch.setattr(update, "MAX_FILE_BYTES", 1024)
        stage(transaction, path, b"candidate")
        monkeypatch.setattr(update, "MAX_JOURNAL_BYTES", 2)
        with pytest.raises(ConfigurationError, match="recovery size"):
            transaction.commit()
    assert not path.exists()


def test_all_replacements_fsync_file_and_directory(monkeypatch, tmp_path):
    calls, real_sync = [], os.fsync

    def sync(fd):
        calls.append(fd)
        return real_sync(fd)

    monkeypatch.setattr(update.os, "fsync", sync)
    path = tmp_path / "settings"
    with update.FileTransaction(tmp_path / "state", [path]) as transaction:
        transaction.stage(path, b"candidate", expected=None, mode=0o700)
        transaction.commit()
    # Journal file+directory, settings file+directory, journal deletion directory.
    assert len(calls) >= 5 and path.stat().st_mode & 0o777 == 0o700


def test_deletions_commit_and_do_not_create_missing_targets(tmp_path):
    state, one, absent = tmp_path / "state", tmp_path / "one", tmp_path / "absent"
    one.write_bytes(b"owned")
    one.chmod(0o600)
    with update.FileTransaction(state, [one, absent]) as transaction:
        stage(transaction, one, None)
        stage(transaction, absent, None)
        assert one.exists()
        assert transaction.commit()
    assert not one.exists() and not absent.exists()


def test_failure_after_deletion_restores_original(monkeypatch, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"original")
    one.chmod(0o644)
    real_replace = update._replace
    failed = False

    def fail(path, value):
        nonlocal failed
        if path == two and not failed:
            failed = True
            raise OSError("disk full")
        real_replace(path, value)

    monkeypatch.setattr(update, "_replace", fail)
    with update.FileTransaction(tmp_path / "state", [one, two]) as transaction:
        stage(transaction, one, None)
        stage(transaction, two, b"created")
        with pytest.raises(OSError):
            transaction.commit()
    assert one.read_bytes() == b"original" and one.stat().st_mode & 0o777 == 0o644
    assert not two.exists()


@pytest.mark.parametrize("conflict", [False, True])
def test_interrupted_deletion_recovers_unless_user_recreated_file(tmp_path, conflict):
    one, state = tmp_path / "one", tmp_path / "state"
    journal = interrupted(state, [(one, (b"original", 0o644), None)])
    if conflict:
        one.write_bytes(b"later user content")
        one.chmod(0o600)
        with pytest.raises(ConfigurationError, match="conflicts"):
            with update.FileTransaction(state, [one]):
                pass
        assert one.read_bytes() == b"later user content" and journal.exists()
    else:
        with update.FileTransaction(state, [one]):
            assert one.read_bytes() == b"original"
            assert one.stat().st_mode & 0o777 == 0o644
        assert not journal.exists()
