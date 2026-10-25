import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from claude_foundry.portal.content_logs import (
    LogQuery,
    LogSource,
    _stream_record,
    _stream_records,
    list_logs,
    read_log,
)


def source():
    return LogSource(
        workspace_id=uuid4(),
        resource_id=f"/subscriptions/{uuid4()}/resourceGroups/test/providers/Microsoft.ApiManagement/service/test",
        api_ids=["claude-preview", "codex-foundry"],
    )


def test_search_projects_metadata_and_scopes_every_query(monkeypatch):
    queries = []
    monkeypatch.setattr(
        "claude_foundry.portal.content_logs.run_query", lambda s, c, q: queries.append(q) or []
    )
    s = source()
    now = datetime.now(UTC)
    f = LogQuery(start=now - timedelta(days=20), end=now, model='evil" | union secrets //')
    assert list_logs(s, object(), f) == {"items": [], "has_more": False}
    q = queries[0]
    assert s.resource_id.lower() in q
    assert 'ApiId in ("claude-preview","codex-foundry")' in q
    assert 'evil\\" | union secrets //' in q
    assert (now - timedelta(days=20)).isoformat() not in q
    assert "has_request=countif(isnotempty(RequestMessages))" in q
    assert "take 51" in q


def test_read_scoped_id_and_fragments_preserved(monkeypatch):
    rows = [
        {"sequence": 0, "request": [{"role": "user", "content": "Hello"}], "response": None},
        {"sequence": 1, "request": None, "response": [{"role": "assistant", "content": "Hi"}]},
    ]
    queries = []
    monkeypatch.setattr(
        "claude_foundry.portal.content_logs.run_query", lambda s, c, q: queries.append(q) or rows
    )
    identifier = uuid4()
    result = read_log(source(), object(), identifier)
    assert result["chunks"] == rows
    assert str(identifier) in queries[0]
    assert "join kind=inner gateway" in queries[0]
    assert "take 129" in queries[0]


def test_oversized_fragment_set_fails_closed(monkeypatch):
    monkeypatch.setattr("claude_foundry.portal.content_logs.run_query", lambda *a: [{}] * 129)
    with pytest.raises(ValueError, match="Too many"):
        read_log(source(), object(), uuid4())


def test_stream_record_accepts_endpoint_uuid(monkeypatch):
    identifier = uuid4()

    class Blob:
        name = f"2026/10/25/{identifier}.json"

    class Download:
        @staticmethod
        def readall():
            return b'{"model":"gpt-chat-latest"}'

    class Container:
        @staticmethod
        def list_blobs(**kwargs):
            assert kwargs == {}
            return [Blob()]

        @staticmethod
        def download_blob(name):
            assert name == Blob.name
            return Download()

    monkeypatch.setattr(
        "claude_foundry.portal.content_logs._stream_container",
        lambda *args: Container(),
    )
    assert _stream_record(source(), object(), identifier)["model"] == "gpt-chat-latest"


def test_stream_records_reads_retention_days_newest_first(monkeypatch):
    now = datetime.now(UTC)

    class Blob:
        def __init__(self, name):
            self.name = name

    names = [
        f"{now:%Y/%m/%d}/z-235959999999-{uuid4()}.json",
        f"{now:%Y/%m/%d}/z-000001000000-{uuid4()}.json",
    ]
    records = {
        name: json.dumps(
            {
                "timestamp": now.isoformat(),
                "api_id": "codex-governed",
                "model": "gpt-6.1-sol",
                "status": 200,
                "request": {},
                "response": {},
                "principal_id": str(uuid4()),
                "correlation_id": str(uuid4()),
            }
        ).encode()
        for name in names
    }

    class Download:
        def __init__(self, value):
            self.value = value

        def readall(self):
            return self.value

    class Container:
        requested = []
        downloaded = []

        @classmethod
        def list_blobs(cls, name_starts_with):
            cls.requested.append(name_starts_with)
            return [Blob(name) for name in names]

        @classmethod
        def download_blob(cls, name):
            cls.downloaded.append(name)
            return Download(records[name])

    monkeypatch.setattr(
        "claude_foundry.portal.content_logs._stream_container",
        lambda *args: Container(),
    )
    s = source()
    s.stream_capture_account = "capture"
    query = LogQuery(start=now - timedelta(hours=1), end=now + timedelta(seconds=1))

    result = _stream_records(s, object(), query)

    assert len(result) == 2
    assert Container.requested == [f"{now:%Y/%m/%d}/"]
    assert Container.downloaded == sorted(names, reverse=True)


def test_list_logs_deduplicates_native_and_stream_capture(monkeypatch):
    identifier = uuid4()
    timestamp = datetime.now(UTC).isoformat()
    native = {
        "timestamp": timestamp,
        "correlation_id": str(identifier),
        "api_id": "codex-foundry",
        "model": "gpt-chat-latest",
        "model_name": "gpt-chat-latest",
        "status": 200,
        "has_request": True,
        "has_response": False,
        "chunks": 1,
        "principal_id": "developer",
    }
    captured = native | {"has_response": True}
    s = source()
    s.stream_capture_account = "capture"
    monkeypatch.setattr("claude_foundry.portal.content_logs.run_query", lambda *args: [native])
    monkeypatch.setattr(
        "claude_foundry.portal.content_logs._stream_records",
        lambda *args: [captured],
    )
    now = datetime.now(UTC)

    result = list_logs(
        s,
        object(),
        LogQuery(start=now - timedelta(days=1), end=now + timedelta(seconds=1)),
    )

    assert result == {"items": [captured], "has_more": False}


def test_cursor_ties_and_invalid_periods(monkeypatch):
    now = datetime.now(UTC)
    queries = []
    monkeypatch.setattr(
        "claude_foundry.portal.content_logs.run_query", lambda s, c, q: queries.append(q) or []
    )
    cursor = uuid4()
    list_logs(
        source(),
        object(),
        LogQuery(start=now - timedelta(days=1), end=now, before=now, before_id=cursor),
    )
    assert f'correlation_id > "{cursor}"' in queries[0]
    for kwargs in [
        {"start": now, "end": now},
        {"start": now - timedelta(days=31), "end": now},
        {"start": now - timedelta(days=1), "end": now, "before": now},
    ]:
        with pytest.raises(ValueError):
            LogQuery(**kwargs)


def test_group_logs_require_trusted_request_time_group():
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from claude_foundry.portal.content_logs import scoped_query

    group = str(uuid4())
    end = datetime.now(UTC)
    query = scoped_query(source(), end - timedelta(days=1), end, group)
    assert 'trace.source) == "budget-governance"' in query
    assert f'| where group_id == "{group}"' in query
    assert query.index(f'| where group_id == "{group}"') < query.index("let messages")
