from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from claude_foundry.codex_models import plan_models
from claude_foundry.models import apply


def model(name="gpt-6.1-sol", **kw):
    return {
        "format": "OpenAI",
        "name": name,
        "version": "2026-09-29",
        "capabilities": {"responses": "true"},
        "skus": [
            {"name": "GlobalStandard", "usageName": "shared", "capacity": {"minimum": 1, "step": 1}}
        ],
        **kw,
    }


def quota(free=15):
    return [{"name": {"value": "shared"}, "limit": free, "currentValue": 0}]


def test_plan_reserves_shared_quota_and_never_uses_ptu():
    plan = plan_models([model("gpt-5"), model("gpt-6-sol"), model("gpt-6-astra")], quota(), [])
    assert [p.get("capacity", 0) for p in plan] == [10, 5, 0]
    assert sum(p.get("capacity", 0) for p in plan) == 15
    ptu = model(skus=[{"name": "GlobalProvisionedManaged", "usageName": "shared"}])
    assert plan_models([ptu], quota(), [])[0]["action"] == "skip"


def test_retired_modalities_and_name_collisions_are_not_deployed():
    retired = model(deprecation={"inference": "2026-01-01T00:00:00Z"})
    now = datetime(2026, 10, 15, tzinfo=UTC)
    assert plan_models([retired], quota(), [], now=now)[0]["reason"] == "retired"
    assert plan_models([model("gpt-image-2")], quota(), [])[0]["action"] == "skip"
    occupied = [{"name": "gpt-6.1-sol", "properties": {"model": {"format": "Anthropic"}}}]
    assert plan_models([model()], quota(), occupied)[0]["action"] == "skip"


def test_falls_back_to_datazone_and_respects_capacity_step():
    m = model(
        skus=[
            {
                "name": "DataZoneStandard",
                "usageName": "shared",
                "capacity": {"minimum": 15, "step": 5},
            }
        ]
    )
    p = plan_models([m], quota(100), [])[0]
    assert p["sku"] == "DataZoneStandard" and p["capacity"] == 15
    assert plan_models([m], quota(14), [])[0]["action"] == "skip"


def test_existing_deployment_keeps_name_sku_and_capacity():
    d = {
        "name": "company-model",
        "sku": {"name": "DataZoneStandard", "capacity": 30},
        "properties": {
            "model": {"name": "gpt-6.1-sol", "format": "OpenAI", "version": "2026-09-29"},
            "provisioningState": "Succeeded",
        },
    }
    p = plan_models([model()], quota(), [d])[0]
    assert p["action"] == "keep" and p["deployment_name"] == "company-model" and p["capacity"] == 30


def test_openai_deployment_does_not_send_anthropic_attestation(org, tmp_path):
    az = Mock()
    az.request.return_value = {"properties": {"provisioningState": "Succeeded"}}
    org["model_provider_data"] = {"organizationName": "Example"}
    apply(org, az, plan_models([model()], quota(), []), tmp_path / "report.json")
    body = az.request.call_args.args[2]
    assert body["properties"]["model"]["format"] == "OpenAI"
    assert "modelProviderData" not in body["properties"]


@pytest.mark.parametrize("capacity", [0, -1, True])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        plan_models([model()], quota(), [], capacity=capacity)


def test_explicit_sku_does_not_fallback_or_allow_provisioned_capacity():
    assert plan_models([model()], quota(), [], sku_name="DataZoneStandard")[0]["action"] == "skip"
    with pytest.raises(ValueError, match="metered"):
        plan_models([model()], quota(), [], sku_name="GlobalProvisionedManaged")
