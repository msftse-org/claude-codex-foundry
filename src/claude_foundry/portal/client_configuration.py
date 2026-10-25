"""Data-only client projection. Callers supply an authorized policy snapshot.

This module performs no I/O and does not authorize a request. In a budgeted
installation the policy must be the verified active publication, not a draft.
Group exports require separate administrative authorization at the route.
"""

import copy

from .domain import effective_models


def project_profile(config, policy, member, *, group_id=None):
    """Select one provider's grants/defaults without modifying shared inputs.

    Empty model sets are intentional: continuous clients need to represent a
    removed final grant. The existing nonempty installer export validates this
    separately. Personal grants are never included in a group-wide export.
    """
    group = next(g for g in policy["groups"] if g["id"] == (group_id or member["group_id"]))
    allowed = group["model_ids"] if group_id else effective_models(policy, member)
    models = copy.deepcopy([m for m in config["models"] if m["name"] in allowed])
    client = config.get("client_type", "claude")
    if client == "claude":
        for family in {m["anthropicFamilyTier"] for m in models}:
            options = [m for m in models if m["anthropicFamilyTier"] == family]
            if any(m["name"] == group["default_model"] for m in options):
                for model in options:
                    model["isFamilyDefault"] = model["name"] == group["default_model"]
            if not any(m.get("isFamilyDefault") for m in options):
                options[0]["isFamilyDefault"] = True
    models.sort(key=lambda model: model["name"] != group["default_model"])
    result = {**config, "models": models}
    if client == "codex":
        names = {m["name"] for m in models}
        result["default_model"] = (
            group["default_model"]
            if group["default_model"] in names
            else next(
                (m["name"] for m in models if m["name"] == config.get("default_model")),
                models[0]["name"] if models else "",
            )
        )
    return result
