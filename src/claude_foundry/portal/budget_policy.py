"""Immutable reviewed snapshots; publication and admission share a ledger barrier."""

import json

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient

from .budget_ledger import policy_digest
from .budget_pricing import BudgetPrice
from .domain import Group, Member, Settings


def snapshot(state):
    result = {
        "schema": 1,
        "settings": Settings.model_validate(state["settings"]).model_dump(mode="json"),
        "groups": [Group.model_validate(g).model_dump(mode="json") for g in state["groups"]],
        "members": [Member.model_validate(m).model_dump(mode="json") for m in state["members"]],
        "budget_prices": [
            BudgetPrice.model_validate(p).model_dump(mode="json")
            for p in state.get("budget_prices", [])
        ],
    }
    settings = result["settings"]
    if not settings["budget_enforcement"] or settings["spend_limit_usd"] is None:
        raise ValueError("Set a finite organization limit and enable hard budget enforcement")
    groups = {g["id"]: g for g in result["groups"]}
    if len(groups) != len(result["groups"]) or len({m["id"] for m in result["members"]}) != len(
        result["members"]
    ):
        raise ValueError("Duplicate group or member identity")
    if not any(m["role"] == "owner" and m["status"] == "active" for m in result["members"]):
        raise ValueError("An active owner is required")
    prices = {p["model"]: p for p in result["budget_prices"]}
    if not prices or len(prices) != len(result["budget_prices"]):
        raise ValueError("Unique verified prices are required")
    for p in prices.values():
        BudgetPrice.model_validate(p).check_current()
    for group in groups.values():
        if group["spend_ceiling_usd"] is None:
            raise ValueError("Every group needs an owner-assigned dollar ceiling")
        if set(group["model_ids"]) - set(group["delegated_models"]):
            raise ValueError("Group models must be inside the owner's delegated allowance")
    for member in result["members"]:
        group = groups.get(member["group_id"])
        if not group:
            raise ValueError("Member group does not exist")
        if set(member["extra_models"]) - set(group["delegated_models"]):
            raise ValueError("Personal grants must stay inside the owner's delegated allowance")
        allowed = set(group["model_ids"] + member["extra_models"])
        if member["status"] == "active" and allowed - set(prices):
            raise ValueError("Every granted model needs a verified enforcement price")
    return result


class Snapshots:
    def __init__(self, account, credential):
        self.blobs = ContainerClient(
            f"https://{account}.blob.core.windows.net", "budget-policies", credential=credential
        )

    def put(self, policy):
        digest = policy_digest(policy)
        try:
            self.blobs.upload_blob(
                digest + ".json", json.dumps(policy, sort_keys=True), overwrite=False
            )
        except ResourceExistsError:
            if self.get(digest) != policy:
                raise ValueError("Immutable budget policy does not match its digest") from None
        return digest

    def get(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid policy digest")
        policy = json.loads(self.blobs.download_blob(digest + ".json").readall())
        if policy_digest(policy) != digest:
            raise ValueError("Published budget policy integrity check failed")
        return policy


def group_publication(active, draft, actor):
    """Publish only delegated team edits, preserving every owner-controlled field.

    The active snapshot is the authority for ceilings, delegation and existing role
    assignments. Unrelated drafts and pending privilege changes never ride along.
    """
    import copy

    if actor["role"] != "group_admin":
        raise PermissionError("A group administrator is required")
    result = copy.deepcopy(active)
    group_id = actor["group_id"]
    current = next((g for g in active["groups"] if g["id"] == group_id), None)
    proposed = next((g for g in draft["groups"] if g["id"] == group_id), None)
    if not current or not proposed:
        raise ValueError("The owner must first publish this group and its ceiling")
    updated = Group.model_validate(
        {
            **proposed,
            "spend_ceiling_usd": current["spend_ceiling_usd"],
            "delegated_models": current["delegated_models"],
        }
    ).model_dump(mode="json")
    result["groups"] = [updated if g["id"] == group_id else g for g in result["groups"]]
    existing = {m["id"]: m for m in active["members"]}
    for m in draft["members"]:
        if m["group_id"] != group_id or m["role"] != "developer":
            continue
        previous = existing.get(m["id"])
        if previous and (previous["group_id"] != group_id or previous["role"] != "developer"):
            raise PermissionError("Only the owner can publish moves or role changes")
        existing[m["id"]] = m
    result["members"] = list(existing.values())
    return snapshot(result)
