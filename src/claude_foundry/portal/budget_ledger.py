"""Atomic organization/group/user reservations in one Azure Table partition.

Unknown dispatch outcomes retain reservations. There is intentionally no TTL refund.
"""

import copy
import hashlib
import json
import threading
from datetime import UTC, datetime
from uuid import UUID

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

from .budget_pricing import micros


class Contended(Exception):
    pass


class BudgetDenied(Exception):
    pass


class MemoryRows:
    """Atomic adapter for deterministic concurrency and failure tests only."""

    def __init__(self):
        self.rows, self.lock, self.version = {}, threading.RLock(), 0

    def read(self, key):
        with self.lock:
            return copy.deepcopy(self.rows.get(key, ({}, None)))

    def pending(self, group_id=None, principal=None):
        with self.lock:
            values = [
                copy.deepcopy(v)
                for v, _ in self.rows.values()
                if v.get("state") == "reserved"
                and (not group_id or v.get("group_id") == group_id)
                and (not principal or v.get("principal_id") == principal)
            ]
        return sorted(values, key=lambda v: v["at"])[:51]

    def commit(self, rows):
        with self.lock:
            if any(self.rows.get(k, ({}, None))[1] != etag for k, _, etag in rows):
                raise Contended()
            for key, value, _ in rows:
                self.version += 1
                self.rows[key] = (copy.deepcopy(value), self.version)


class AzureRows:
    def __init__(self, account, organization_id, credential):
        if not account.isalnum() or not 3 <= len(account) <= 24:
            raise ValueError("Invalid budget storage account")
        self.partition = str(UUID(organization_id))
        self.table = TableClient(
            f"https://{account}.table.core.windows.net",
            "BudgetLedger",
            credential=credential,
            retry_total=0,
        )

    def read(self, key):
        try:
            entity = self.table.get_entity(self.partition, key)
            return json.loads(entity["data"]), entity.metadata["etag"]
        except ResourceNotFoundError:
            return {}, None

    def commit(self, rows):
        operations = []
        for key, value, etag in rows:
            entity = {
                "PartitionKey": self.partition,
                "RowKey": key,
                "data": json.dumps(value, separators=(",", ":")),
            }
            if "state" in value:
                entity.update(
                    state=value["state"],
                    group_id=value["group_id"],
                    principal_id=value["principal_id"],
                )
            if etag is None:
                operations.append(("create", entity))
            else:
                operations.append(
                    (
                        "update",
                        entity,
                        {
                            "mode": UpdateMode.REPLACE,
                            "etag": etag,
                            "match_condition": MatchConditions.IfNotModified,
                        },
                    )
                )
        try:
            self.table.submit_transaction(operations)
        except HttpResponseError as error:
            if error.status_code in {409, 412}:
                raise Contended() from error
            raise

    def pending(self, group_id=None, principal=None):
        from itertools import islice

        query = "PartitionKey eq @organization and state eq 'reserved'"
        params = {"organization": self.partition}
        if group_id:
            query += " and group_id eq @group"
            params["group"] = str(UUID(group_id))
        if principal:
            query += " and principal_id eq @principal"
            params["principal"] = str(UUID(principal))
        return [
            json.loads(e["data"])
            for e in islice(
                self.table.query_entities(query, parameters=params, results_per_page=51), 51
            )
        ]


def policy_digest(policy):
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def configured_limit(value, unlimited=False):
    """Only an explicit owner-reviewed flag removes a cap; missing values fail closed."""
    if unlimited is True and value is None:
        return None
    if unlimited or value is None:
        raise BudgetDenied(
            "Hard budget limits have not been configured for this organization/group"
        )
    return micros(value)


def organization_limit(policy):
    settings = policy["settings"]
    if not settings.get("budget_enforcement"):
        raise BudgetDenied("Hard budget enforcement is not enabled")
    return configured_limit(
        settings.get("spend_limit_usd"), settings.get("spend_limit_unlimited", False)
    )


def group_limit(group):
    ceiling = configured_limit(
        group.get("spend_ceiling_usd"), group.get("spend_ceiling_unlimited", False)
    )
    operating = group.get("spend_limit_usd")
    if operating is None:
        return ceiling
    return micros(operating) if ceiling is None else min(ceiling, micros(operating))


def limits_for(policy, principal):
    member = next(
        (m for m in policy["members"] if m["id"] == principal and m["status"] == "active"), None
    )
    if not member:
        raise BudgetDenied("Your organization has not granted active gateway access")
    group = next(g for g in policy["groups"] if g["id"] == member["group_id"])
    org_limit = organization_limit(policy)
    effective_group_limit = group_limit(group)
    user_limit = member.get("spend_limit_usd")
    # No role bypass, including the organization owner.
    limits = {
        "org": org_limit,
        "group-" + group["id"]: effective_group_limit,
        "user-" + principal: micros(user_limit)
        if user_limit is not None
        else effective_group_limit,
    }
    return member, group, limits


class Ledger:
    def __init__(self, rows):
        self.rows = rows

    def activate(self, policy, expected=None, at=None):
        """Never lower a cap below committed liability or unpause an accounting incident.

        Reservations update the policy ETag. Checking the counters and replacing that
        ETag makes a concurrent admission abort publication, even for 2,000 users.
        Settlement only reduces liability, so a concurrent settlement is safe.
        """
        meta, etag = self.rows.read("policy")
        if meta.get("paused"):
            raise BudgetDenied("Resolve the accounting incident before publishing")
        if expected is not None and (meta.get("digest") or "") != expected:
            raise Contended("The published budget policy changed")
        month = (at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
        limits = {"org": organization_limit(policy)}
        for g in policy["groups"]:
            limits["group-" + g["id"]] = group_limit(g)
        for m in policy["members"]:
            limits["user-" + m["id"]] = (
                micros(m["spend_limit_usd"])
                if m.get("spend_limit_usd") is not None
                else limits["group-" + m["group_id"]]
            )
        for scope, limit in limits.items():
            counter, _ = self.rows.read(month + "-" + scope)
            if limit is not None and counter.get("spent", 0) + counter.get("reserved", 0) > limit:
                raise BudgetDenied(
                    "A new limit cannot be below this month's spent plus reserved amount"
                )
        digest = policy_digest(policy)
        self.rows.commit([("policy", {"digest": digest, "paused": False}, etag)])
        return digest

    def active(self):
        meta, _ = self.rows.read("policy")
        if not meta.get("digest") or meta.get("paused"):
            raise BudgetDenied("The budget service is unavailable or paused")
        return meta["digest"]

    def reserve(self, request_id, principal, model, amount, policy, fingerprint, at=None):
        request_id, principal = str(UUID(request_id)), str(UUID(principal))
        if type(amount) is not int or amount <= 0:
            raise ValueError("A positive maximum reservation is required")
        at = at or datetime.now(UTC)
        if at.tzinfo is None:
            raise ValueError("An aware admission time is required")
        month = at.astimezone(UTC).strftime("%Y-%m")
        digest = policy_digest(policy)
        member, group, limits = limits_for(policy, principal)
        if model not in set(group["model_ids"] + member.get("extra_models", [])):
            raise BudgetDenied("This model is not allowed for your group or account")
        key = "request-" + request_id
        for _ in range(12):
            meta, meta_etag = self.rows.read("policy")
            if meta.get("digest") != digest or meta.get("paused"):
                raise BudgetDenied(
                    "Budget policy changed or is paused; retry with the current policy"
                )
            existing, _ = self.rows.read(key)
            if existing:
                # Never dispatch an inference twice under a previously charged identifier.
                raise BudgetDenied("This gateway request identifier has already been admitted")
            writes = [("policy", meta, meta_etag)]
            counters = []
            for scope, limit in limits.items():
                counter_key = month + "-" + scope
                counter, etag = self.rows.read(counter_key)
                counter = counter or {"spent": 0, "reserved": 0}
                if limit is not None and counter["spent"] + counter["reserved"] + amount > limit:
                    raise BudgetDenied(
                        "Monthly "
                        + scope.split("-")[0]
                        + " budget has insufficient remaining allowance for this request"
                    )
                counter["reserved"] += amount
                writes.append((counter_key, counter, etag))
                counters.append(counter_key)
            reservation = {
                "request_id": request_id,
                "principal_id": principal,
                "group_id": group["id"],
                "model": model,
                "month": month,
                "reserved": amount,
                "charged": None,
                "state": "reserved",
                "policy": digest,
                "fingerprint": fingerprint,
                "counters": counters,
                "at": at.isoformat(),
            }
            writes.append((key, reservation, None))
            try:
                self.rows.commit(writes)
                return reservation
            except Contended:
                continue
        raise BudgetDenied("The budget ledger is busy; no inference was admitted")

    def settle(self, request_id, amount):
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid settlement amount")
        key = "request-" + str(UUID(request_id))
        for _ in range(12):
            reservation, etag = self.rows.read(key)
            if not reservation:
                raise ValueError("Unknown reservation")
            if reservation["state"] == "settled":
                if reservation["charged"] != amount:
                    raise ValueError("Conflicting settlement; manual reconciliation required")
                return reservation
            if amount > reservation["reserved"]:
                # An invalid price/capacity bound must halt admission, not silently overdraw.
                meta, meta_etag = self.rows.read("policy")
                try:
                    self.rows.commit(
                        [
                            (
                                "policy",
                                {
                                    **meta,
                                    "paused": True,
                                    "incident_request_id": str(request_id),
                                    "incident_cost_micro_usd": amount,
                                },
                                meta_etag,
                            )
                        ]
                    )
                except Contended:
                    continue
                raise BudgetDenied("Usage exceeded its verified reservation; enforcement is paused")
            writes = []
            for counter_key in reservation["counters"]:
                counter, counter_etag = self.rows.read(counter_key)
                if not counter or counter["reserved"] < reservation["reserved"]:
                    raise ValueError("Ledger inconsistency; manual reconciliation required")
                counter["reserved"] -= reservation["reserved"]
                counter["spent"] += amount
                writes.append((counter_key, counter, counter_etag))
            reservation.update(
                state="settled", charged=amount, settled_at=datetime.now(UTC).isoformat()
            )
            writes.append((key, reservation, etag))
            try:
                self.rows.commit(writes)
                return reservation
            except Contended:
                continue
        raise Contended("Settlement remains reserved; retry reconciliation")

    def incident(self):
        meta, _ = self.rows.read("policy")
        if not meta.get("paused") or not meta.get("incident_request_id"):
            return None
        request_id = str(UUID(meta["incident_request_id"]))
        reservation, _ = self.rows.read("request-" + request_id)
        return {
            "request_id": request_id,
            "observed_cost_micro_usd": meta.get("incident_cost_micro_usd"),
            "reservation": reservation or None,
        }

    def resolve_incident(self, request_id):
        """Charge the recorded overrun and unpause only its exact reserved request."""
        request_id = str(UUID(request_id))
        key = "request-" + request_id
        for _ in range(12):
            meta, meta_etag = self.rows.read("policy")
            if not meta.get("paused") or meta.get("incident_request_id") != request_id:
                raise BudgetDenied("The accounting incident changed or is no longer active")
            amount = meta.get("incident_cost_micro_usd")
            if type(amount) is not int or amount < 0:
                raise ValueError("The accounting incident has no valid observed cost")
            reservation, reservation_etag = self.rows.read(key)
            if not reservation or reservation.get("state") != "reserved":
                raise ValueError("The incident reservation is not open")
            writes = []
            for counter_key in reservation["counters"]:
                counter, counter_etag = self.rows.read(counter_key)
                if not counter or counter["reserved"] < reservation["reserved"]:
                    raise ValueError("Ledger inconsistency; manual reconciliation required")
                counter["reserved"] -= reservation["reserved"]
                counter["spent"] += amount
                writes.append((counter_key, counter, counter_etag))
            reservation.update(
                state="settled",
                charged=amount,
                settled_at=datetime.now(UTC).isoformat(),
                reconciliation="observed_overrun",
            )
            clean_meta = {
                k: v
                for k, v in meta.items()
                if k not in {"incident_request_id", "incident_cost_micro_usd"}
            }
            clean_meta["paused"] = False
            writes.extend(
                [
                    (key, reservation, reservation_etag),
                    ("policy", clean_meta, meta_etag),
                ]
            )
            try:
                self.rows.commit(writes)
                return reservation
            except Contended:
                continue
        raise Contended("Incident reconciliation was contended; retry after reloading")

    def balances(self, policy, principal, at=None):
        _, _, limits = limits_for(policy, principal)
        month = (at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
        result = []
        for scope, limit in limits.items():
            value, _ = self.rows.read(month + "-" + scope)
            spent, reserved = value.get("spent", 0), value.get("reserved", 0)
            result.append(
                {
                    "scope": scope,
                    "limit_micro_usd": limit,
                    "spent_micro_usd": spent,
                    "reserved_micro_usd": reserved,
                    "remaining_micro_usd": None
                    if limit is None
                    else max(0, limit - spent - reserved),
                }
            )
        return {"month": month, "currency": "USD", "scopes": result}
