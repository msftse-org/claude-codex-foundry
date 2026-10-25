"""Cost query bounds, literals, partial-result handling, and disclosure limits."""

import io
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from claude_foundry.portal.costs import CostQuery, build_query, cost_analysis


@pytest.mark.parametrize(
    "start,end",
    [("2024-03-02", "2024-03-01"), ("2024-01-01", "2024-04-03"), ("2099-01-01", "2099-01-02")],
)
def test_cost_period_is_bounded(start, end):
    with pytest.raises(ValidationError):
        CostQuery(start=start, end=end)


def test_cost_query_escapes_filters_and_uses_fixed_scope(org):
    malicious = 'x"); union secrets; //'
    filters = CostQuery(
        start="2024-03-01", end="2024-03-02", model_ids=[malicious], grouping="member"
    )
    query = build_query(org, filters, [])
    assert "datetime(2024-02-28)" in query
    assert "datetime(2024-03-03)" in query
    assert f"dynamic({json.dumps([malicious])})" in query
    assert f"== {json.dumps(org['api_id'])}" in query
    assert "key=principal_id" in query
    with pytest.raises(ValidationError):
        CostQuery(start="2024-03-01", end="2024-03-02", grouping="anything; secrets")
    with pytest.raises(ValidationError):
        CostQuery(start="2024-03-01", end="2024-03-02", member_ids=["x\n;"])


def test_cost_partial_query_never_becomes_a_successful_total(org, monkeypatch):
    monkeypatch.setattr(
        "claude_foundry.portal.costs.urlopen",
        lambda *args, **kwargs: io.BytesIO(
            json.dumps({"error": {"code": "PartialError"}, "tables": []}).encode()
        ),
    )
    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test"))
    with pytest.raises(ValueError, match="incomplete"):
        cost_analysis(
            "test-app", credential, org, CostQuery(start="2024-03-01", end="2024-03-02"), []
        )


def test_cost_breakdown_limit_preserves_full_totals_and_marks_truncation(org, monkeypatch):
    rows = [["totals", json.dumps({"period": "current", "input_tokens": 9000})]]
    rows += [
        ["breakdown", json.dumps({"period": "current", "key": str(i), "input_tokens": 1})]
        for i in range(2501)
    ]
    payload = {"tables": [{"columns": [{"name": "bucket"}, {"name": "data"}], "rows": rows}]}
    monkeypatch.setattr(
        "claude_foundry.portal.costs.urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()),
    )
    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test"))
    result = cost_analysis(
        "test-app", credential, org, CostQuery(start="2024-03-01", end="2024-03-02"), []
    )
    assert result["totals"][0]["input_tokens"] == 9000
    assert len(result["breakdown"]) == 2500
    assert result["truncated"] is True


def test_shared_cost_scope_includes_exactly_the_server_configured_apis(org):
    config = {
        **org,
        "reporting_apis": [{"api_id": "claude-governed"}, {"api_id": "codex-governed"}],
    }
    query = build_query(config, CostQuery(start="2024-03-01", end="2024-03-02"), [])
    assert 'dynamic(["claude-governed", "codex-governed"])' in query
    assert json.dumps(org["apim_name"]) in query
    assert '== "claude-foundry"' not in query
    with pytest.raises(ValidationError):
        CostQuery(start="2024-03-01", end="2024-03-02", api_ids=["other-api"])
