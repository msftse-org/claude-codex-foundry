from claude_foundry.model_capacity import plan_capacity


def deployment(name="model-a", capacity=10, sku="GlobalStandard"):
    return {
        "name": name,
        "sku": {"name": sku, "capacity": capacity},
        "properties": {
            "provisioningState": "Succeeded",
            "model": {"name": name, "version": "1", "format": "OpenAI"},
        },
    }


def model(name="model-a", **capacity):
    return {
        "name": name,
        "version": "1",
        "format": "OpenAI",
        "skus": [
            {"name": "GlobalStandard", "usageName": "pool", "capacity": capacity},
            {"name": "GlobalStandard", "usageName": "pool-finetune"},
        ],
    }


def quota(limit=100, used=10):
    return [{"name": {"value": "pool"}, "limit": limit, "currentValue": used}]


def test_maximum_preserves_allocations_on_other_resources():
    item = plan_capacity([model()], quota(100, 60), [deployment()])[0]
    assert item["action"] == "scale" and item["target"] == 50


def test_shared_pool_is_not_allocated_twice_and_no_shrinking():
    plan = plan_capacity(
        [model("model-a"), model("model-b")],
        quota(100, 60),
        [deployment("model-b", 20), deployment("model-a", 10)],
    )
    assert [p["target"] for p in plan] == [50, 20]
    assert [p["action"] for p in plan] == ["scale", "keep"]
    item = plan_capacity([model(maximum=5)], quota(100, 100), [deployment()])[0]
    assert item["target"] == 10 and item["action"] == "keep"


def test_obeys_capacity_steps_maximum_and_allowed_values():
    item = plan_capacity(
        [model(minimum=15, step=5, maximum=37)], quota(100, 20), [deployment(capacity=15)]
    )[0]
    assert item["target"] == 35
    item = plan_capacity([model(allowedValues=[10, 30, 60])], quota(100, 60), [deployment()])[0]
    assert item["target"] == 30


def test_missing_or_ambiguous_quota_and_provisioned_capacity_are_skipped():
    assert plan_capacity([model()], [], [deployment()])[0]["action"] == "skip"
    assert plan_capacity([model(), model()], quota(), [deployment()])[0]["action"] == "skip"
    assert (
        plan_capacity([model()], quota(), [deployment(sku="GlobalProvisionedManaged")])[0]["action"]
        == "skip"
    )
    assert (
        plan_capacity([{**model(), "version": "2"}], quota(), [deployment()])[0]["action"] == "skip"
    )
