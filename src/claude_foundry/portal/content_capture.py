"""Private, retention-bounded prompt and streamed-response capture."""

import json
from datetime import UTC, datetime
from uuid import UUID

from azure.storage.blob import BlobServiceClient, ContentSettings


class ContentCapture:
    def __init__(self, account, credential, container="content-logs", max_bytes=262144):
        if not account.isalnum() or not 3 <= len(account) <= 24:
            raise ValueError("Invalid content-log storage account")
        if not 1024 <= max_bytes <= 262144:
            raise ValueError("Invalid content-log message limit")
        self.container = BlobServiceClient(
            f"https://{account}.blob.core.windows.net", credential=credential
        ).get_container_client(container)
        self.max_bytes = max_bytes

    def _name(self, request_id, at):
        # Sort new captures after legacy UUID-only names and newest-first
        # within a day, so readers need not download the full retention window.
        at = at.astimezone(UTC)
        return f"{at:%Y/%m/%d}/z-{at:%H%M%S%f}-{UUID(request_id)}.json"

    def begin(self, request_id, provider, principal_id, group_id, model, request):
        now = datetime.now(UTC)
        record = {
            "version": 1,
            "correlation_id": str(UUID(request_id)),
            "request_id": str(UUID(request_id)),
            "timestamp": now.isoformat(),
            "api_id": "claude-governed" if provider == "anthropic" else "codex-governed",
            "provider": provider,
            "principal_id": str(UUID(principal_id)),
            "group_id": str(UUID(group_id)),
            "model": model,
            "status": None,
            "request": self.bounded_json(request),
            "response": None,
            "complete": False,
        }
        name = self._name(request_id, now)
        self._write(name, record)
        return name, record

    def finish(self, name, record, response, status, complete):
        record = {
            **record,
            "status": status,
            "response": self.bounded_text(response),
            "complete": bool(complete),
            "finished_at": datetime.now(UTC).isoformat(),
        }
        self._write(name, record)

    def _write(self, name, record):
        self.container.upload_blob(
            name,
            json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode(),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )

    def bounded_json(self, value):
        return self.bounded_text(
            json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        )

    def bounded_text(self, value):
        if value is None:
            return None
        raw = value if isinstance(value, bytes) else bytes(value)
        truncated = len(raw) > self.max_bytes
        raw = raw[: self.max_bytes]
        return {
            "content": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "captured_bytes": len(raw),
        }
