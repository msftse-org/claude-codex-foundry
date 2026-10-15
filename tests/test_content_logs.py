from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from claude_foundry.portal.content_logs import LogQuery, LogSource, list_logs, read_log


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
