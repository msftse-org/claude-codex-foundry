"""Conservative, copy-on-write tool envelopes for pre-GPT-5 Responses models."""

import json

_COMPOSITIONS = ("oneOf", "anyOf", "allOf")
_OBJECT_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "title",
    "description",
    "$comment",
    "$schema",
    "$defs",
    "definitions",
}
_BRANCH_KEYS = _OBJECT_KEYS - {"$schema", "$defs", "definitions"}
_VARIANTS_NOTE = "Arguments must match one of the documented variants."


def _object_parts(schema):
    if (
        not isinstance(schema, dict)
        or set(schema) - _OBJECT_KEYS
        or schema.get("type", "object") != "object"
        or not isinstance(schema.get("additionalProperties", True), bool)
    ):
        return None
    properties, required = schema.get("properties", {}), schema.get("required", [])
    if (
        not isinstance(properties, dict)
        or any(
            not isinstance(name, str) or not isinstance(value, dict)
            for name, value in properties.items()
        )
        or not isinstance(required, list)
        or any(not isinstance(name, str) for name in required)
    ):
        return None
    return properties, required


def _definition_key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _object_envelope(schema):
    if not isinstance(schema, dict):
        return None
    compositions = [key for key in _COMPOSITIONS if key in schema]
    if len(compositions) > 1:
        return None
    if not compositions and (schema.get("type") == "object" or "properties" not in schema):
        return None
    key = compositions[0] if compositions else None
    branches = schema[key] if key else [{"type": "object"}]
    if not isinstance(branches, list) or not branches:
        return None
    base = {name: value for name, value in schema.items() if name != key}
    parts = _object_parts(base)
    if parts is None:
        return None
    base_properties, base_required = parts
    definitions, common_required = {}, None
    for branch in branches:
        if (
            not isinstance(branch, dict)
            or set(branch) - _BRANCH_KEYS
            or not ("type" in branch or "properties" in branch)
        ):
            return None
        parts = _object_parts(branch)
        if parts is None:
            return None
        properties, required = parts
        # A conflicting sibling constraint is an intersection, not a variant.
        if any(
            name in base_properties
            and _definition_key(base_properties[name]) != _definition_key(value)
            for name, value in properties.items()
        ):
            return None
        properties = {**base_properties, **properties}
        required = set(base_required) | set(required)
        if required - properties.keys():
            return None
        common_required = required if common_required is None else common_required & required
        for name, value in properties.items():
            definitions.setdefault(name, {}).setdefault(_definition_key(value), value)
    properties = {}
    for name, variants in definitions.items():
        values = list(variants.values())
        properties[name] = values[0] if len(values) == 1 else {"anyOf": values}
    result = {
        **base,
        "type": "object",
        "properties": properties,
        "required": [name for name in properties if name in common_required],
    }
    if "additionalProperties" not in result and all(
        branch.get("additionalProperties") is False for branch in branches
    ):
        result["additionalProperties"] = False
    return result


def _adapt_function(tool):
    if (
        not isinstance(tool, dict)
        or tool.get("type") != "function"
        or not isinstance(tool.get("name"), str)
        or not isinstance(tool.get("description", ""), str)
        or ("strict" in tool and tool["strict"] is not None and type(tool["strict"]) is not bool)
    ):
        return tool
    try:
        parameters = _object_envelope(tool.get("parameters"))
    except (TypeError, ValueError, RecursionError):
        return tool
    if parameters is None:
        return tool
    description = tool.get("description", "")
    result = {
        **tool,
        "parameters": parameters,
        "description": description + (" " if description else "") + _VARIANTS_NOTE,
    }
    # Strict objects cannot have optional properties or permit additional ones.
    if parameters.get("additionalProperties") is not False or set(parameters["required"]) != set(
        parameters["properties"]
    ):
        result["strict"] = False
    return result


def _adapt_tool(tool):
    if isinstance(tool, dict) and tool.get("type") == "namespace":
        children = tool.get("tools")
        if not isinstance(children, list):
            return tool
        adapted = [_adapt_function(child) for child in children]
        if any(before is not after for before, after in zip(children, adapted, strict=True)):
            return {**tool, "tools": adapted}
        return tool
    return _adapt_function(tool)


def adapt_legacy_tools(tools):
    """Relax recognized object variants without mutating tools or guessing unknown schemas."""
    if not isinstance(tools, list):
        return tools
    adapted = [_adapt_tool(tool) for tool in tools]
    if any(before is not after for before, after in zip(tools, adapted, strict=True)):
        return adapted
    return tools
