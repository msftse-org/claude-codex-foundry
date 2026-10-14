"""Versioned private state with optimistic concurrency; no last-writer-wins saves."""

import json
import threading
from pathlib import Path

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob import BlobClient, ContentSettings

from ..config import write_json


class Conflict(Exception):
    pass


class LocalStore:
    def __init__(self, path, initial):
        self.path = Path(path)
        self.lock = threading.RLock()
        if not self.path.exists():
            write_json(self.path, initial)

    def read(self):
        with self.lock:
            state = json.loads(self.path.read_text())
            return state, state["revision"]

    def save(self, state, etag):
        with self.lock:
            if self.read()[1] != etag:
                raise Conflict("Another administrator saved changes. Reload and review again.")
            write_json(self.path, state)


class BlobStore:
    def __init__(self, account, credential, initial):
        self.blob = BlobClient(
            f"https://{account}.blob.core.windows.net",
            "portal-state",
            "organization.json",
            credential=credential,
        )
        try:
            self.blob.upload_blob(
                json.dumps(initial),
                overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except ResourceExistsError:
            pass

    def read(self):
        download = self.blob.download_blob()
        return json.loads(download.readall()), download.properties.etag

    def save(self, state, etag):
        try:
            self.blob.upload_blob(
                json.dumps(state),
                overwrite=True,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except (ResourceModifiedError, ResourceNotFoundError) as exc:
            raise Conflict("Another administrator saved changes. Reload and review again.") from exc
