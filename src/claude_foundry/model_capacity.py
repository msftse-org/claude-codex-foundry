"""Allocate unused metered quota to existing deployments without moving other capacity."""

import math

METERED_SKUS = {"GlobalStandard", "DataZoneStandard", "Standard"}


def plan_capacity(catalog, quotas, deployments):
    remaining = {
        q["name"]["value"]: max(0, math.floor(q["limit"] - q["currentValue"])) for q in quotas
    }
    plan = []
    for deployment in sorted(deployments, key=lambda d: d["name"]):
        properties, sku = deployment["properties"], deployment["sku"]
        current = sku.get("capacity")
        item = dict(name=deployment["name"], sku=sku["name"], current=current, action="skip")
        plan.append(item)
        if properties.get("provisioningState") != "Succeeded" or sku["name"] not in METERED_SKUS:
            item["reason"] = "Only succeeded metered Standard deployments can be scaled"
            continue
        if type(current) is not int or current < 1:
            item["reason"] = "Deployment has no verified current capacity"
            continue
        model = properties["model"]
        candidates = [
            s
            for m in catalog
            if all(m.get(k) == model.get(k) for k in ("name", "format", "version"))
            for s in m.get("skus", [])
            if s["name"] == sku["name"] and "finetune" not in s.get("usageName", "").lower()
        ]
        if len(candidates) != 1 or candidates[0].get("usageName") not in remaining:
            item["reason"] = "An unambiguous catalog SKU and quota pool are required"
            continue
        candidate = candidates[0]
        pool = candidate["usageName"]
        constraints = candidate.get("capacity") or {}
        step, minimum = constraints.get("step") or 1, constraints.get("minimum") or 1
        maximum = min(current + remaining[pool], constraints.get("maximum") or math.inf)
        target = int(maximum // step * step)
        if constraints.get("allowedValues"):
            target = max((v for v in constraints["allowedValues"] if v <= target), default=0)
        target = max(current, target)  # Never shrink an existing deployment.
        item.update(target=target, pool=pool)
        if target > current and target >= minimum:
            remaining[pool] -= target - current
            item["action"] = "scale"
        else:
            item.update(action="keep", reason="No additional allocatable quota")
    return plan
