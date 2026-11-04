"""Offline Responses schema compatibility and budget-admission regression tests."""

import asyncio
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from claude_foundry.portal import budget_proxy
from claude_foundry.portal.budget_ledger import Ledger, MemoryRows
from claude_foundry.portal.budget_policy import snapshot
from claude_foundry.portal.budget_pricing import BudgetPrice
from claude_foundry.portal.budget_proxy import BudgetProxy
from claude_foundry.portal.domain import seed
from claude_foundry.portal.tool_schemas import adapt_legacy_tools

MISSING = object()
VARIANTS_NOTE = "Arguments must match one of the documented variants."


@pytest.fixture
def automation_update():
    # Public Codex search finds test fixtures, not the app's production definition:
    # https://github.com/openai/codex/blob/ba0dfd38979033f514a28d9514215bad46957a18/codex-rs/tools/src/tool_search_tests.rs#L174-L207
    # This representative oneOf reproduces the reported app schema shape.
    schedule = {
        "type": "object",
        "properties": {"timezone": {"type": "string"}},
        "required": ["timezone"],
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "name": "automation_update",
        "description": (
            "Manage synthetic automations: create requires prompt and schedule; update requires id."
        ),
        "strict": True,
        "defer_loading": True,
        "parameters": {
            "description": "Representative automation options.",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["create"]},
                        "prompt": {"type": "string"},
                        "schedule": schedule,
                    },
                    "required": ["mode", "prompt", "schedule"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["update"]},
                        "id": {"type": "string"},
                        "prompt": {"type": "string"},
                        "schedule": copy.deepcopy(schedule),
                    },
                    "required": ["mode", "id"],
                    "additionalProperties": False,
                },
            ],
        },
    }


@pytest.fixture
def dispatch(org):
    def run(body, *, provider="openai", capability=False, events=None):
        events = events if events is not None else []
        model = body["model"]
        state = seed(org, str(uuid4()))
        state["settings"].update(budget_enforcement=True, spend_limit_usd="1")
        state["groups"][0].update(
            spend_ceiling_usd="1",
            model_ids=[model],
            delegated_models=[model],
            default_model=model,
        )
        state["budget_prices"] = [
            {
                "model": model,
                "provider": provider,
                "deployment_version": "1",
                "max_input_tokens": 1000,
                "max_output_tokens": 1000,
                "default_output_tokens": 100,
                "input_ceiling_per_million": "3",
                "output_ceiling_per_million": "15",
                "source_url": "https://example.com/synthetic-price",
                "verified_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "valid_until": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                "note": "Synthetic local fixture, not production pricing",
            }
        ]
        policy = snapshot(state)
        price = BudgetPrice.model_validate(policy["budget_prices"][0])
        prepared, maximum, _ = price.prepare(body)
        ledger = Ledger(MemoryRows())
        ledger.activate(policy)
        principal, request_id = policy["members"][0]["id"], str(uuid4())
        deployment = {
            "provider": provider,
            "version": "1",
            "max_input_tokens": 1000,
            "max_output_tokens": 1000,
        }
        if capability is not MISSING:
            deployment["supports_reasoning"] = capability
        observations = []
        original_reserve = ledger.reserve

        def reserve(*args, **kwargs):
            events.append("reserve")
            return original_reserve(*args, **kwargs)

        ledger.reserve = reserve

        def get_token(scope):
            events.append("token")
            return SimpleNamespace(token="synthetic")

        def handler(request):
            events.append("send")
            payload = json.loads(request.content)
            reservation, _ = ledger.rows.read("request-" + request_id)
            adapted_payload, adapted_maximum, _ = price.prepare(payload)
            assert adapted_payload == payload
            assert reservation["state"] == "reserved"
            assert reservation["reserved"] == adapted_maximum == maximum
            assert (
                reservation["fingerprint"]
                == hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            observations.append(
                SimpleNamespace(
                    content=request.content,
                    payload=payload,
                    reservation=reservation,
                    prepared=prepared,
                    maximum=maximum,
                )
            )
            return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 10}})

        async def infer():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                proxy = BudgetProxy(
                    ledger,
                    SimpleNamespace(get=lambda digest: policy),
                    SimpleNamespace(get_token=get_token),
                    "https://test.services.ai.azure.com",
                    {model: deployment},
                    client,
                )
                response = await proxy.infer(provider, principal, request_id, body)
                assert response.status_code == 200

        asyncio.run(infer())
        assert len(observations) == 1
        settled, _ = ledger.rows.read("request-" + request_id)
        assert settled["state"] == "settled" and settled["charged"] == 450
        assert all(
            scope["reserved_micro_usd"] == 0
            for scope in ledger.balances(policy, principal)["scopes"]
        )
        return observations[0]

    return run


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1"])
def test_legacy_deployments_adapt_automation_update(dispatch, automation_update, model):
    body = {
        "model": model,
        "input": "Synthetic schema compatibility check.",
        "tools": [automation_update],
        "reasoning": {"effort": "low"},
        "metadata": {"fixture": "synthetic"},
    }
    original = copy.deepcopy(body)
    observed = dispatch(body)
    tool = observed.payload["tools"][0]
    assert tool["parameters"]["type"] == "object"
    assert "oneOf" not in tool["parameters"]
    assert set(tool["parameters"]["properties"]) == {"mode", "prompt", "schedule", "id"}
    assert tool["parameters"]["required"] == ["mode"]
    assert tool["strict"] is False
    assert tool["description"] == automation_update["description"] + " " + VARIANTS_NOTE
    assert "reasoning" not in observed.payload
    assert observed.payload["metadata"] == body["metadata"]
    assert body == original


@pytest.mark.parametrize("composition", ["oneOf", "anyOf", "allOf"])
@pytest.mark.parametrize("explicit_type", [False, True])
def test_variant_union_preserves_definitions_and_common_requirements(
    automation_update, composition, explicit_type
):
    schema = automation_update["parameters"]
    schema[composition] = schema.pop("oneOf")
    if explicit_type:
        schema["type"] = "object"
    original = copy.deepcopy(automation_update)
    tools = [automation_update]
    adapted = adapt_legacy_tools(tools)
    tool, parameters = adapted[0], adapted[0]["parameters"]
    assert not {"oneOf", "anyOf", "allOf"} & parameters.keys()
    assert parameters == {
        "type": "object",
        "description": "Representative automation options.",
        "properties": {
            "mode": {
                "anyOf": [
                    {"type": "string", "enum": ["create"]},
                    {"type": "string", "enum": ["update"]},
                ]
            },
            "prompt": {"type": "string"},
            "schedule": schema[composition][0]["properties"]["schedule"],
            "id": {"type": "string"},
        },
        "required": ["mode"],
        "additionalProperties": False,
    }
    assert tool["description"] == original["description"] + " " + VARIANTS_NOTE
    assert tool["strict"] is False
    assert tool["defer_loading"] is True
    assert tools == [original]
    assert adapted is not tools and tool is not automation_update
    assert adapt_legacy_tools(adapted) is adapted


@pytest.mark.parametrize("strict", [MISSING, None, False, True])
def test_inferred_type_disables_unsatisfiable_strict_mode(strict):
    tool = {
        "type": "function",
        "name": "lookup",
        "parameters": {
            "properties": {"id": {"type": "string"}, "optional": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    }
    if strict is not MISSING:
        tool["strict"] = strict
    adapted = adapt_legacy_tools([tool])[0]
    assert adapted["parameters"] == {**tool["parameters"], "type": "object"}
    assert adapted["description"] == VARIANTS_NOTE
    assert adapted["strict"] is False
    assert "type" not in tool["parameters"] and "description" not in tool
    assert adapt_legacy_tools([adapted])[0] is adapted


def test_satisfiable_strict_mode_is_preserved():
    tool = {
        "type": "function",
        "name": "lookup",
        "strict": True,
        "description": "",
        "parameters": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": [mode]}},
                    "required": ["mode"],
                    "additionalProperties": False,
                }
                for mode in ("first", "second")
            ]
        },
    }
    adapted = adapt_legacy_tools([tool])[0]
    assert adapted["strict"] is True
    assert adapted["parameters"]["required"] == ["mode"]
    assert adapted["parameters"]["additionalProperties"] is False
    assert adapted["description"] == VARIANTS_NOTE


def test_inferred_type_preserves_satisfiable_strict_mode():
    tool = {
        "type": "function",
        "name": "lookup",
        "strict": True,
        "parameters": {
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    }
    adapted = adapt_legacy_tools([tool])[0]
    assert adapted["strict"] is True
    assert adapted["parameters"] == {**tool["parameters"], "type": "object"}


def test_top_level_shared_properties_definitions_and_requirements_are_preserved(automation_update):
    schema = automation_update["parameters"]
    schema.update(
        properties={"shared": {"$ref": "#/$defs/shared"}},
        required=["shared"],
        **{"$defs": {"shared": {"type": "string"}}, "title": "Synthetic variants"},
    )
    adapted = adapt_legacy_tools([automation_update])[0]["parameters"]
    assert adapted["properties"]["shared"] == {"$ref": "#/$defs/shared"}
    assert adapted["$defs"] == schema["$defs"]
    assert adapted["title"] == schema["title"]
    assert adapted["required"] == ["shared", "mode"]


def test_missing_branch_requirements_are_not_invented(automation_update):
    schema = automation_update["parameters"]
    del schema["oneOf"][1]["required"]
    del schema["oneOf"][1]["additionalProperties"]
    adapted = adapt_legacy_tools([automation_update])[0]
    assert adapted["parameters"]["required"] == []
    assert "additionalProperties" not in adapted["parameters"]
    assert adapted["strict"] is False


def test_equal_definitions_are_deduplicated_but_boolean_and_numeric_values_differ():
    tool = {
        "type": "function",
        "name": "choose",
        "parameters": {
            "oneOf": [{"properties": {"value": {"const": value}}} for value in (True, 1, True)]
        },
    }
    adapted = adapt_legacy_tools([tool])[0]
    variants = adapted["parameters"]["properties"]["value"]["anyOf"]
    assert len(variants) == 2
    assert variants[0]["const"] is True
    assert type(variants[1]["const"]) is int and variants[1]["const"] == 1


def test_namespace_only_adapts_function_children(automation_update):
    plain = {"type": "function", "name": "plain", "parameters": {"type": "object"}}
    custom = {"type": "custom", "name": "patch", "format": {"type": "text"}}
    nested = {"type": "namespace", "tools": [automation_update]}
    namespace = {
        "type": "namespace",
        "name": "codex_app",
        "description": "Synthetic namespace.",
        "tools": [automation_update, plain, custom, nested, None],
    }
    adapted = adapt_legacy_tools([namespace])[0]
    assert adapted is not namespace
    assert adapted["name"] == namespace["name"]
    assert adapted["description"] == namespace["description"]
    assert adapted["tools"][0]["parameters"]["type"] == "object"
    assert all(
        before is after
        for before, after in zip(namespace["tools"][1:], adapted["tools"][1:], strict=True)
    )
    assert namespace["tools"][0] is automation_update
    assert "type" not in automation_update["parameters"]
    assert adapt_legacy_tools([adapted])[0] is adapted


def test_plain_custom_and_unknown_tools_remain_identical(automation_update):
    plain = {
        "type": "function",
        "name": "plain",
        "strict": True,
        "description": "Unchanged.",
        "parameters": {
            "type": "object",
            "properties": {"choice": {"oneOf": [{"type": "string"}, {"type": "number"}]}},
        },
    }
    custom = {
        "type": "custom",
        "name": "patch",
        "parameters": automation_update["parameters"],
        "format": {"type": "grammar", "syntax": "lark", "definition": 'start: "synthetic"'},
    }
    unknown = {"type": "future_tool", "parameters": automation_update["parameters"]}
    tools = [plain, custom, unknown, {"type": "namespace", "tools": [plain, custom]}]
    assert adapt_legacy_tools(tools) is tools
    mixed = adapt_legacy_tools([automation_update, *tools])
    assert all(before is after for before, after in zip(tools, mixed[1:], strict=True))


@pytest.mark.parametrize("tools", [None, False, 7, "tools", {}, ()])
def test_malformed_tools_containers_are_unchanged(tools):
    assert adapt_legacy_tools(tools) is tools


@pytest.mark.parametrize(
    "tool",
    [
        None,
        False,
        7,
        "tool",
        [],
        {},
        {"type": "namespace"},
        {"type": "namespace", "tools": {}},
        {"type": "function", "function": {"name": "chat_completions_shape"}},
        {"type": "function", "parameters": {"properties": {}}},
    ],
)
def test_malformed_and_unrecognized_tools_are_unchanged(tool):
    tools = [tool]
    assert adapt_legacy_tools(tools) is tools


@pytest.mark.parametrize(
    "schema",
    [
        None,
        False,
        7,
        "schema",
        [],
        {},
        {"type": "string"},
        {"type": ["object", "null"], "properties": {}},
        {"properties": []},
        {"properties": {"bad": 7}},
        {"properties": {}, "required": "bad"},
        {"properties": {}, "required": [7]},
        {"properties": {}, "required": ["missing"]},
        {"properties": {}, "additionalProperties": {"type": "string"}},
        {"oneOf": []},
        {"oneOf": {}},
        {"oneOf": [None]},
        {"oneOf": [{}]},
        {"oneOf": [False]},
        {"oneOf": [{"type": "string"}]},
        {"oneOf": [{"$ref": "#/$defs/missing"}]},
        {"oneOf": [{"type": "object", "properties": []}]},
        {"oneOf": [{"properties": {}, "required": [None]}]},
        {"oneOf": [{"properties": {}, "required": ["missing"]}]},
        {"oneOf": [{"properties": {}, "minProperties": 1}]},
        {"oneOf": [{"properties": {}}], "anyOf": [{"properties": {}}]},
        {"oneOf": [{"properties": {}}], "$ref": "#/$defs/missing"},
        {
            "properties": {"id": {"type": "string"}},
            "oneOf": [{"properties": {"id": {"type": "number"}}}],
        },
    ],
)
def test_malformed_or_unrecognized_schemas_never_raise_and_are_unchanged(schema):
    tools = [{"type": "function", "name": "malformed", "parameters": schema}]
    assert adapt_legacy_tools(tools) is tools


@pytest.mark.parametrize("description", [None, 7, [], {}])
def test_malformed_descriptions_are_unchanged(automation_update, description):
    automation_update["description"] = description
    tools = [automation_update]
    assert adapt_legacy_tools(tools) is tools


@pytest.mark.parametrize("strict", [0, 1, "true", [], {}])
def test_malformed_strict_flags_are_unchanged(automation_update, strict):
    automation_update["strict"] = strict
    tools = [automation_update]
    assert adapt_legacy_tools(tools) is tools


def test_non_json_and_cyclic_property_definitions_do_not_raise(automation_update):
    properties = automation_update["parameters"]["oneOf"][0]["properties"]
    properties["invalid"] = {"default": object()}
    tools = [automation_update]
    assert adapt_legacy_tools(tools) is tools
    properties["invalid"] = {}
    properties["invalid"]["cycle"] = properties["invalid"]
    assert adapt_legacy_tools(tools) is tools


@pytest.mark.parametrize(
    ("model", "capability"),
    [
        ("gpt-4o", True),
        ("gpt-4.1", True),
        ("gpt-5", True),
        ("gpt-5.1", True),
        ("gpt-5", MISSING),
        ("gpt-4o", MISSING),
        ("gpt-4o", None),
        ("gpt-4o", 0),
        ("gpt-4o", ""),
        ("gpt-4o", "false"),
    ],
)
def test_non_target_deployments_are_byte_identical(
    dispatch, automation_update, monkeypatch, model, capability
):
    def unexpected_adaptation(tools):
        pytest.fail("A non-target deployment must not call the adapter")

    monkeypatch.setattr(budget_proxy, "adapt_legacy_tools", unexpected_adaptation)
    body = {
        "model": model,
        "input": "Synthetic byte-preservation check.",
        "tools": [automation_update],
        "reasoning": {"effort": "low"},
        "metadata": {"fixture": "synthetic"},
        "client_metadata": {"originator": "codex", "supports_reasoning": False},
        "text": {"format": {"type": "text"}},
        "parallel_tool_calls": True,
    }
    original = copy.deepcopy(body)
    observed = dispatch(body, capability=capability)
    expected = httpx.Request("POST", "https://example.com/responses", json=observed.prepared)
    assert observed.content == expected.content
    assert body == original


def test_anthropic_payload_is_byte_identical(dispatch, automation_update, monkeypatch):
    def unexpected_adaptation(tools):
        pytest.fail("Anthropic requests must not call the Responses adapter")

    monkeypatch.setattr(budget_proxy, "adapt_legacy_tools", unexpected_adaptation)
    body = {
        "model": "claude-test",
        "messages": [{"role": "user", "content": "Synthetic provider isolation check."}],
        "tools": [{"name": "automation_update", "input_schema": automation_update["parameters"]}],
        "metadata": {"fixture": "synthetic"},
    }
    original = copy.deepcopy(body)
    observed = dispatch(body, provider="anthropic", capability=False)
    assert (
        observed.content
        == httpx.Request("POST", "https://example.com/messages", json=observed.prepared).content
    )
    assert body == original


@pytest.mark.parametrize("include_tools", [False, True])
def test_legacy_non_target_payloads_are_byte_identical(dispatch, monkeypatch, include_tools):
    body = {
        "model": "gpt-4o",
        "input": "Synthetic no-rewrite check.",
        "reasoning": {"effort": "low"},
    }
    if include_tools:
        body["tools"] = [
            {"type": "function", "name": "plain", "parameters": {"type": "object"}},
            {"type": "custom", "name": "patch", "format": {"type": "text"}},
            {"type": "function", "name": "unknown", "parameters": {"$ref": "#/$defs/unknown"}},
        ]
    else:

        def unexpected_adaptation(tools):
            pytest.fail("Requests without tools must not call the adapter or gain a tools key")

        monkeypatch.setattr(budget_proxy, "adapt_legacy_tools", unexpected_adaptation)
    observed = dispatch(body)
    expected = observed.prepared
    expected.pop("reasoning")
    assert (
        observed.content
        == httpx.Request("POST", "https://example.com/responses", json=expected).content
    )


@pytest.mark.parametrize("namespaced", [False, True])
@pytest.mark.parametrize("output_tokens", [32, 100, 500])
def test_adaptation_precedes_fingerprint_and_reservation_without_other_payload_changes(
    dispatch, automation_update, monkeypatch, namespaced, output_tokens
):
    target = automation_update
    if namespaced:
        target = {
            "type": "namespace",
            "name": "codex_app",
            "description": "Synthetic namespace.",
            "tools": [automation_update],
        }
    plain = {
        "type": "function",
        "name": "plain",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    custom = {"type": "custom", "name": "patch", "format": {"type": "text"}}
    body = {
        "model": "gpt-4.1",
        "input": "Synthetic fingerprint and reservation check.",
        "max_output_tokens": output_tokens,
        "reasoning": {"effort": "low"},
        "tools": [plain, target, custom],
        "metadata": {"fixture": "synthetic"},
        "client_metadata": {"originator": "codex", "supports_reasoning": True},
        "parallel_tool_calls": True,
        "temperature": 0.2,
        "prompt_cache_key": "synthetic",
    }
    original = copy.deepcopy(body)
    events, adapted = [], []

    def observe_adaptation(tools):
        events.append("adapt")
        assert tools is body["tools"]
        result = adapt_legacy_tools(tools)
        assert result[0] is plain and result[2] is custom
        adapted.append(result)
        return result

    monkeypatch.setattr(budget_proxy, "adapt_legacy_tools", observe_adaptation)
    observed = dispatch(body, events=events)
    assert events == ["adapt", "token", "reserve", "send"]
    before = copy.deepcopy(observed.prepared)
    before.pop("reasoning")
    before_hash = hashlib.sha256(
        json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    expected = {**before, "tools": adapted[0]}
    assert (
        observed.content
        == httpx.Request("POST", "https://example.com/responses", json=expected).content
    )
    assert observed.reservation["fingerprint"] != before_hash
    assert observed.reservation["reserved"] == 3000 + 15 * output_tokens
    target = observed.payload["tools"][1]
    function = target["tools"][0] if namespaced else target
    assert function["parameters"]["type"] == "object"
    assert body == original
