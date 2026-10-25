"""Recoverable multi-file updates for explicitly named local settings.

Atomic replacement protects each file; a private journal protects the set.
This is not protection against a malicious process running as the same user.
Callers supply trusted target paths, never paths received from a server.
"""

import base64
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path

from .configuration_client import ConfigurationError, _pairs

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_BYTES = 40 * 1024 * 1024
MAX_FILES = 32


def _path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigurationError("Local update requires unambiguous absolute paths.")
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ConfigurationError("Local update cannot follow symlinks.")
    return path


def _read(path, *, limit=MAX_FILE_BYTES):
    path = _path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or info.st_size > limit
        ):
            raise ConfigurationError("Local update found an unsafe file or size.")
        body = stream.read(limit + 1)
        if len(body) > limit:
            raise ConfigurationError("Local update exceeds the file size limit.")
        return body, stat.S_IMODE(info.st_mode)


def _sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace(path, value):
    _path(path)
    if value is None:
        path.unlink(missing_ok=True)
        _sync_directory(path.parent)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _path(path)
    fd, temporary = tempfile.mkstemp(prefix=".configuration-update-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value[0])
            os.fchmod(stream.fileno(), value[1])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _encode(value):
    return (
        None
        if value is None
        else {"body": base64.b64encode(value[0]).decode("ascii"), "mode": value[1]}
    )


def _decode(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"body", "mode"}
        or type(value["mode"]) is not int
        or value["mode"] not in {0o600, 0o644, 0o700, 0o755}
        or not isinstance(value["body"], str)
    ):
        raise ValueError
    body = base64.b64decode(value["body"], validate=True)
    if len(body) > MAX_FILE_BYTES:
        raise ValueError
    return body, value["mode"]


class FileTransaction:
    """Lock, stage, compare, journal and replace a bounded set of settings.

    Recovery rolls back an incomplete set before another update. If someone
    edited a target since the interruption, recovery stops without overwriting
    that edit. Rollback does not affect server-side authorization.
    """

    def __init__(self, directory, allowed_paths):
        self.directory = _path(directory)
        self.allowed = {_path(path) for path in allowed_paths}
        self.journal = self.directory / "pending-update.json"
        self.lock = self.directory / "update.lock"
        if {self.journal, self.lock} & self.allowed or len(self.allowed) > MAX_FILES:
            raise ConfigurationError("Invalid local update target set.")
        self.staged = {}
        self.fd = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ConfigurationError("Local update state must be private and owned by you.")
        self.fd = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ConfigurationError("Unsafe local update lock.")
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigurationError("Another configuration update is running.") from None
            self.recover()
            return self
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def stage(self, path, body, *, expected, mode=0o600):
        path = _path(path)
        if self.fd is None or path not in self.allowed or path in self.staged:
            raise ConfigurationError("Invalid or duplicate local update target.")
        if not isinstance(body, bytes) or len(body) > MAX_FILE_BYTES or mode not in {0o600, 0o700}:
            raise ConfigurationError("Invalid local update content or permissions.")
        before = _read(path)
        if (None if before is None else before[0]) != expected:
            raise ConfigurationError("Settings changed during rendering; update cancelled.")
        if before is not None and before[1] not in {0o600, 0o644, 0o700, 0o755}:
            raise ConfigurationError("Unsupported existing settings permissions.")
        after = body, mode
        if before != after:
            self.staged[path] = before, after

    def commit(self):
        if self.fd is None:
            raise ConfigurationError("Local updates require the update lock.")
        for path, (before, _) in self.staged.items():
            if _read(path) != before:
                raise ConfigurationError("Settings changed during preparation; update cancelled.")
        if not self.staged:
            return False
        body = json.dumps(
            {
                "schema_version": 1,
                "files": [
                    {"path": str(path), "before": _encode(before), "after": _encode(after)}
                    for path, (before, after) in self.staged.items()
                ],
            },
            separators=(",", ":"),
        ).encode()
        if len(body) > MAX_JOURNAL_BYTES:
            raise ConfigurationError("Local update exceeds the recovery size limit.")
        _replace(self.journal, (body, 0o600))
        try:
            for path, (before, after) in self.staged.items():
                if _read(path) != before:
                    raise ConfigurationError("Settings changed during update; recovery required.")
                _replace(path, after)
            self.journal.unlink()
            _sync_directory(self.directory)
        except BaseException:
            self.recover()
            raise
        self.staged.clear()
        return True

    def recover(self):
        if self.fd is None:
            raise ConfigurationError("Local recovery requires the update lock.")
        stored = _read(self.journal, limit=MAX_JOURNAL_BYTES)
        if stored is None:
            return
        try:
            if stored[1] != 0o600:
                raise ValueError
            record = json.loads(stored[0], object_pairs_hook=_pairs)
            if (
                not isinstance(record, dict)
                or set(record) != {"schema_version", "files"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] != 1
                or not isinstance(record["files"], list)
                or not 1 <= len(record["files"]) <= MAX_FILES
            ):
                raise ValueError
            files = {}
            for item in record["files"]:
                if not isinstance(item, dict) or set(item) != {"path", "before", "after"}:
                    raise ValueError
                path = _path(item["path"])
                if path not in self.allowed or path in files:
                    raise ValueError
                before, after = _decode(item["before"]), _decode(item["after"])
                if after is None or after[1] not in {0o600, 0o700}:
                    raise ValueError
                files[path] = before, after
            # Check every target before restoring any, preserving post-crash edits.
            current = {path: _read(path) for path in files}
            if any(current[path] not in values for path, values in files.items()):
                raise ConfigurationError(
                    "Interrupted update conflicts with later settings edits; review recovery."
                )
            for path, (before, _) in reversed(list(files.items())):
                if current[path] != before:
                    _replace(path, before)
            self.journal.unlink()
            _sync_directory(self.directory)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ConfigurationError(
                "Invalid local recovery journal; manual review required."
            ) from None
