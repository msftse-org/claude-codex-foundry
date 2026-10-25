"""Validated policy drafts. No Azure mutations happen in this module."""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import islice
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


def now():
    return datetime.now(UTC).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Member(StrictModel):
    id: UUID
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    role: Literal["owner", "admin", "group_admin", "billing", "auditor", "developer"] = "developer"
    status: Literal["active", "suspended"] = "active"
    group_id: UUID
    monthly_tokens: int = Field(default=0, ge=0, le=1_000_000_000_000)
    spend_limit_usd: Decimal | None = Field(default=None, ge=0, le=1_000_000_000, decimal_places=6)
    extra_models: list[str] = Field(default_factory=list, max_length=50)


class Group(StrictModel):
    id: UUID
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    model_ids: list[str] = Field(default_factory=list, max_length=50)
    default_model: str = ""
    monthly_tokens: int = Field(default=0, ge=0, le=1_000_000_000_000)
    budget_usd: float = Field(default=0, ge=0, le=1_000_000_000)
    spend_ceiling_usd: Decimal | None = Field(
        default=None, ge=0, le=1_000_000_000, decimal_places=6
    )
    spend_limit_usd: Decimal | None = Field(default=None, ge=0, le=1_000_000_000, decimal_places=6)
    spend_ceiling_unlimited: bool = False
    delegated_models: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def default_allowed(self):
        if self.spend_ceiling_unlimited and self.spend_ceiling_usd is not None:
            raise ValueError("An unlimited group ceiling cannot also have a dollar value")
        if self.default_model and self.default_model not in self.model_ids:
            raise ValueError("The default model must be allowed for the group")
        if (
            self.spend_ceiling_usd is not None
            and self.spend_limit_usd is not None
            and self.spend_limit_usd > self.spend_ceiling_usd
        ):
            raise ValueError("The group budget cannot exceed its owner-assigned ceiling")
        return self


class Settings(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    budget_enforcement: bool = False
    spend_limit_unlimited: bool = False
    spend_limit_usd: Decimal | None = Field(default=None, ge=0, le=1_000_000_000, decimal_places=6)
    monthly_budget_usd: float = Field(default=0, ge=0, le=1_000_000_000)
    monthly_tokens: int = Field(default=0, ge=0, le=1_000_000_000_000)
    member_tokens_per_minute: int = Field(default=0, ge=0, le=100_000_000)
    member_requests_per_minute: int = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def consistent_limit(self):
        if self.spend_limit_unlimited and self.spend_limit_usd is not None:
            raise ValueError("An unlimited organization limit cannot also have a dollar value")
        return self


class Change(StrictModel):
    revision: int = Field(ge=0)
    kind: Literal[
        "member",
        "member_import",
        "group",
        "settings",
        "pricing",
        "budget_pricing",
        "request",
        "decision",
    ]
    value: dict


class Price(StrictModel):
    model: str
    input_per_million: float = Field(ge=0, le=10000)
    output_per_million: float = Field(ge=0, le=10000)
    source_url: HttpUrl | None = None
    verified_on: date | None = None
    note: str | None = Field(default=None, max_length=500)


def seed(config, owner_id, owner_name="Organization owner", owner_email="owner@example.com"):
    group_id = str(uuid4())
    models = [m["name"] for m in config["models"]]
    return {
        "revision": 0,
        "settings": Settings(name=config.get("profile_name", "Claude on Azure")).model_dump(),
        "members": [
            Member(
                id=owner_id, name=owner_name, email=owner_email, role="owner", group_id=group_id
            ).model_dump(mode="json")
        ],
        "groups": [
            Group(
                id=group_id,
                name="Platform team",
                description="Initial group for the organization owner.",
                model_ids=models,
                default_model=next(
                    (
                        m["name"]
                        for m in config["models"]
                        if m.get("anthropicFamilyTier") == "sonnet" and m.get("isFamilyDefault")
                    ),
                    models[0],
                ),
            ).model_dump(mode="json")
        ],
        "prices": [],
        "budget_prices": [],
        "requests": [],
        "audit": [],
        "published_revision": None,
        "publish_status": "not_published",
        "published_at": None,
    }


def effective_models(state, member):
    if member["status"] != "active":
        return []
    group = next(g for g in state["groups"] if g["id"] == member["group_id"])
    return sorted(set(group["model_ids"] + member.get("extra_models", [])))


def audit(state, actor, action, target, before, after, group_id=None):
    state["audit"].append(
        {
            "id": str(uuid4()),
            "at": now(),
            "actor_id": actor["id"],
            "actor": actor["name"],
            "action": action,
            "target": target,
            "before": before,
            "after": after,
            "group_id": group_id,
        }
    )


def import_members(state, value, actor=None):
    """Validate an entire CSV before accepting any members. Imports cannot elevate roles."""
    if set(value) != {"csv", "group_id"} or not isinstance(value["csv"], str):
        raise ValueError("Provide CSV content and the default group")
    groups = {group["id"] for group in state["groups"]}
    if not isinstance(value["group_id"], str) or value["group_id"] not in groups:
        raise ValueError("Select an existing default group")
    reader = csv.DictReader(io.StringIO(value["csv"].lstrip("\ufeff")), strict=True)
    fields = reader.fieldnames or []
    if (
        not {"object_id", "name", "email"}.issubset(fields)
        or set(fields) - {"object_id", "name", "email", "group_id"}
        or len(fields) != len(set(fields))
    ):
        raise ValueError(
            "CSV requires object_id,name,email; group_id is optional. Other columns are not accepted."
        )
    seen = {member["id"] for member in state["members"]}
    members = []
    try:
        rows = list(islice(reader, 2501))
    except csv.Error as error:
        raise ValueError(f"Invalid CSV: {error}") from error
    for line, row in enumerate(rows, 2):
        if len(members) >= 2500:
            raise ValueError("Import at most 2,500 people per file")
        try:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("Each row must have the same columns as the header")
            member = Member(
                id=row["object_id"].strip(),
                name=row["name"].strip(),
                email=row["email"].strip(),
                group_id=row.get("group_id", "").strip() or value["group_id"],
            ).model_dump(mode="json")
            if member["id"] in seen:
                raise ValueError("This object ID already exists in the organization or CSV")
            if member["group_id"] not in groups:
                raise ValueError("Group not found")
            if actor and actor["role"] == "group_admin" and member["group_id"] != actor["group_id"]:
                raise PermissionError("Group administrators can import only into their own group")
            seen.add(member["id"])
            members.append(member)
        except ValueError as error:
            raise ValueError(f"CSV row {line}: {error}") from error
    if not members:
        raise ValueError("The CSV contains no people")
    return members


def apply_change(state, change, actor, model_ids):
    """Mutate a caller-owned copy, after all authorization and validation."""
    kind, value = change.kind, change.value
    role = actor["role"]
    scoped = role == "group_admin"
    scope_id = actor["group_id"] if scoped else None
    if (
        role not in {"owner", "admin", "group_admin"}
        and not (role == "billing" and kind in {"settings", "pricing"})
        and kind != "request"
    ):
        raise PermissionError("Your role cannot make this change")
    if scoped and kind not in {"member", "member_import", "group", "request", "decision"}:
        raise PermissionError("This change requires an organization administrator")
    before = None
    event_group = None
    target = kind
    if kind == "member_import":
        members = import_members(state, value, actor)
        state["members"].extend(members)
        target, value = f"{len(members)} developers", {"members": members}
        event_group = scope_id
    elif kind in {"member", "group"}:
        item = (Member if kind == "member" else Group).model_validate(value).model_dump(mode="json")
        collection = state[kind + "s"]
        old = next((x for x in collection if x["id"] == item["id"]), None)
        if old:
            old = (
                (Member if kind == "member" else Group).model_validate(old).model_dump(mode="json")
            )
        if scoped:
            if kind == "group":
                if not old or item["id"] != scope_id:
                    raise PermissionError("You can manage only your assigned group")
            elif item["group_id"] != scope_id or (old and old["group_id"] != scope_id):
                raise PermissionError("You cannot access or move another group's members")
            elif item["role"] != "developer" or (old and old["role"] != "developer"):
                raise PermissionError("Group administrators can manage developers only")
        if kind == "member":
            if not any(g["id"] == item["group_id"] for g in state["groups"]):
                raise ValueError("Select an existing group")
            if item["role"] == "owner" or (old and old["role"] == "owner"):
                if role != "owner":
                    raise PermissionError("Only an owner can change ownership")
            if (
                old
                and old["id"] == actor["id"]
                and (item["role"] != old["role"] or item["status"] != "active")
            ):
                raise ValueError("You cannot suspend yourself or change your own role")
            if old and item["group_id"] != old["group_id"] and role != "owner":
                raise PermissionError("Only the owner can move existing members between groups")
            grants = item["extra_models"]
            member_group = next(g for g in state["groups"] if g["id"] == item["group_id"])
            if scoped and set(grants) - set(member_group.get("delegated_models", [])):
                raise PermissionError("Only owner-delegated models can be granted")
            parent_limit = member_group.get("spend_limit_usd")
            if parent_limit is None:
                parent_limit = member_group.get("spend_ceiling_usd")
            if (
                item["spend_limit_usd"] is not None
                and parent_limit is not None
                and Decimal(item["spend_limit_usd"]) > Decimal(str(parent_limit))
            ):
                raise ValueError("The user budget cannot exceed the group allowance")
            event_group = item["group_id"]

        else:
            grants = item["model_ids"]
            for owner_field in ("spend_ceiling_usd", "spend_ceiling_unlimited", "delegated_models"):
                default = {"delegated_models": [], "spend_ceiling_unlimited": False}.get(
                    owner_field
                )
                previous = old.get(owner_field, default) if old else default
                if role != "owner" and item[owner_field] != previous:
                    raise PermissionError(
                        "Only the organization owner can change group ceilings or delegated model allowances"
                    )
            if set(item["delegated_models"]) - set(model_ids):
                raise ValueError("Delegate only models deployed in this organization")
            if scoped and set(grants) - set(item["delegated_models"]):
                raise PermissionError("Only owner-delegated models can be granted")
            event_group = item["id"]

        if set(grants) - set(model_ids):
            raise ValueError("Select models deployed in this organization")
        before = old
        state[kind + "s"] = [x for x in collection if x["id"] != item["id"]] + [item]
        if not any(m["role"] == "owner" and m["status"] == "active" for m in state["members"]):
            raise ValueError("Keep at least one active owner")
        target, value = item["name"], item
    elif kind == "settings":
        value = Settings.model_validate(value).model_dump(mode="json")
        before = Settings.model_validate(state["settings"]).model_dump(mode="json")
        if role != "owner" and any(
            value[k] != before[k]
            for k in ("budget_enforcement", "spend_limit_usd", "spend_limit_unlimited")
        ):
            raise PermissionError(
                "Only the organization owner can change the organization spending limit or enforcement mode"
            )
        if role == "billing" and any(
            value[k] != before[k] for k in value if k != "monthly_budget_usd"
        ):
            raise PermissionError(
                "Billing administrators can change the dollar planning budget only"
            )
        state["settings"] = value
    elif kind == "pricing":
        value = Price.model_validate(value).model_dump(mode="json", exclude_none=True)
        if value["model"] not in model_ids:
            raise ValueError("Unknown model")
        before = next((p for p in state["prices"] if p["model"] == value["model"]), None)
        state["prices"] = [p for p in state["prices"] if p["model"] != value["model"]] + [value]
        target = value["model"]
    elif kind == "budget_pricing":
        if role != "owner":
            raise PermissionError("Only the owner can change enforcement prices")
        from .budget_pricing import BudgetPrice

        value = BudgetPrice.model_validate(value).model_dump(mode="json")
        if value["model"] not in model_ids:
            raise ValueError("Unknown model")
        before = next(
            (p for p in state.get("budget_prices", []) if p["model"] == value["model"]), None
        )
        state["budget_prices"] = [
            p for p in state.get("budget_prices", []) if p["model"] != value["model"]
        ] + [value]
        target = value["model"]
    elif kind == "request":
        if (
            set(value) != {"model", "reason"}
            or value["model"] not in model_ids
            or not isinstance(value["reason"], str)
            or not 5 <= len(value["reason"]) <= 1000
        ):
            raise ValueError("Choose a deployed model and give a reason (5–1000 characters)")
        if any(
            r["member_id"] == actor["id"] and r["model"] == value["model"] and r["status"] == "open"
            for r in state["requests"]
        ):
            raise ValueError("You already have an open request for this model")
        value = {
            **value,
            "id": str(uuid4()),
            "member_id": actor["id"],
            "member_name": actor["name"],
            "at": now(),
            "status": "open",
        }
        state["requests"].append(value)
        event_group = actor["group_id"]
    elif kind == "decision":
        if role not in {"owner", "admin", "group_admin"}:
            raise PermissionError("Only administrators can decide requests")
        if (
            set(value) != {"id", "approve", "note"}
            or type(value["approve"]) is not bool
            or not isinstance(value["note"], str)
            or len(value["note"]) > 1000
        ):
            raise ValueError("Invalid decision")
        request = next(
            (r for r in state["requests"] if r["id"] == value["id"] and r["status"] == "open"), None
        )
        if not request:
            raise ValueError("This request has already been decided or does not exist")
        requester = next(m for m in state["members"] if m["id"] == request["member_id"])
        if scoped:
            group = next(g for g in state["groups"] if g["id"] == scope_id)
            if requester["group_id"] != scope_id or requester["role"] != "developer":
                raise PermissionError("You can decide requests only for your group's developers")
            if value["approve"] and request["model"] not in group.get("delegated_models", []):
                raise PermissionError("This model needs approval from the organization owner")
        event_group = requester["group_id"]
        before = dict(request)
        request.update(
            status="approved" if value["approve"] else "declined",
            note=value["note"],
            decided_by=actor["id"],
            decided_at=now(),
        )
        if value["approve"]:
            member = next(m for m in state["members"] if m["id"] == request["member_id"])
            member["extra_models"] = sorted(set(member["extra_models"] + [request["model"]]))
        value, target = dict(request), request["member_name"]
    audit(state, actor, kind + ".updated", target, before, value, event_group)
    state["revision"] += 1
    return state
