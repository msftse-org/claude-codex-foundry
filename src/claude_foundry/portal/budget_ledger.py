"""Atomic organization/group/user reservations in one Azure Table partition.

Unknown dispatch outcomes retain reservations. There is intentionally no TTL refund.
"""

import copy
import hashlib
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

from .budget_pricing import micros
from .budget_recovery import (
    MAX_ROWS,
    MemoryArchive,
    RecoveryError,
    Vault,
    digest,
    expected_counters,
    validate_counter,
)


class Contended(Exception):
    pass


class BudgetDenied(Exception):
    pass


class MemoryRows:
    """Atomic adapter for deterministic concurrency and failure tests only."""

    def __init__(self):
        self.rows, self.lock, self.version = {}, threading.RLock(), 0
        self.binding = "memory/BudgetLedger/" + str(uuid4())
        self.archive = MemoryArchive()

    def scan(self):
        with self.lock:
            return {k: copy.deepcopy(v) for k, (v, _) in self.rows.items()}

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
        self.account = account
        self.binding = account + "/BudgetLedger/" + self.partition
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

    def scan(self):
        result = {}
        for entity in self.table.query_entities(
            "PartitionKey eq @organization", parameters={"organization": self.partition}
        ):
            result[entity["RowKey"]] = json.loads(entity["data"])
            if len(result) > MAX_ROWS:
                raise RecoveryError("Ledger exceeds the reviewed recovery export bound")
        return result

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


def limits_for(policy, principal):
    member = next(
        (m for m in policy["members"] if m["id"] == principal and m["status"] == "active"), None
    )
    if not member:
        raise BudgetDenied("Your organization has not granted active gateway access")
    group = next(g for g in policy["groups"] if g["id"] == member["group_id"])
    org_limit = policy["settings"].get("spend_limit_usd")
    ceiling = group.get("spend_ceiling_usd")
    if not policy["settings"].get("budget_enforcement") or org_limit is None or ceiling is None:
        raise BudgetDenied(
            "Hard budget limits have not been configured for this organization/group"
        )
    operating = group.get("spend_limit_usd")
    group_limit = min(
        micros(ceiling), micros(operating) if operating is not None else micros(ceiling)
    )
    user_limit = member.get("spend_limit_usd")
    # No role bypass, including the organization owner.
    limits = {
        "org": micros(org_limit),
        "group-" + group["id"]: group_limit,
        "user-" + principal: micros(user_limit) if user_limit is not None else group_limit,
    }
    return member, group, limits


class Ledger:
    def __init__(self, rows, vault=None):
        self.rows = rows
        if vault is None and isinstance(rows, MemoryRows):
            vault = Vault(rows.archive, rows.binding)
        if vault is None:
            raise RecoveryError("Protected recovery storage is required for a budget ledger")
        if vault.binding != rows.binding:
            raise RecoveryError("Recovery storage is bound to another ledger")
        self.vault = vault

    def validate_meta(self, meta):
        self.vault.anchor()
        if (
            meta.get("format") != 2
            or type(meta.get("revision")) is not int
            or not meta.get("digest")
        ):
            raise RecoveryError("Ledger control data is missing or requires explicit migration")

    def _write(self, writes, initializing=False):
        self.vault.check_open()
        meta, tag = self.rows.read("policy")
        self.validate_meta(meta)
        if (
            meta.get("frozen")
            or meta.get("recovering")
            or (meta.get("initializing") and not initializing)
        ):
            raise RecoveryError("Ledger is frozen or awaiting recovery verification")
        self.rows.commit([("policy", {**meta, "revision": meta["revision"] + 1}, tag), *writes])

    def _counter(self, key):
        value, etag = self.rows.read(key)
        if not value:
            if self.vault.has_genesis(key):
                self.freeze("A previously initialized spending counter is missing")
            raise RecoveryError(
                "An expected monthly spending counter is missing; recovery required"
            )
        marker = self.vault.genesis(key)
        try:
            validate_counter(value, marker)
        except RecoveryError:
            self.freeze("An initialized spending counter failed integrity validation")
            raise
        return value, etag

    def prepare_month(self, policy, at=None, initializing=False):
        """Explicit, guarded provisioning; admission never initializes a missing row."""
        self.vault.check_open()
        month = (at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
        for key in expected_counters(policy, month):
            for attempt in range(12):
                meta, tag = self.rows.read("policy")
                self.validate_meta(meta)
                if (
                    meta.get("frozen")
                    or meta.get("recovering")
                    or (meta.get("paused") and not initializing)
                ):
                    raise RecoveryError("Cannot provision a frozen or paused ledger")
                if meta["digest"] != policy_digest(policy):
                    # During publication, new scopes can be provisioned without activating grants.
                    if initializing:
                        raise RecoveryError("Initialization policy does not match")
                value, _ = self.rows.read(key)
                marker = digest({"binding": self.rows.binding, "counter": key})
                if value:
                    validate_counter(value, marker)
                    if not self.vault.has_genesis(key) and (value["spent"] or value["reserved"]):
                        raise RecoveryError("A used counter has lost its initialization evidence")
                    self.vault.genesis(key, create=True)
                    break
                if self.vault.has_genesis(key):
                    self.freeze("Counter provisioning detected deletion of an initialized row")
                    raise RecoveryError(
                        "A previously initialized counter was deleted; refusing zero reset"
                    )
                try:
                    self.rows.commit(
                        [
                            ("policy", {**meta, "revision": meta["revision"] + 1}, tag),
                            (key, {"spent": 0, "reserved": 0, "genesis": marker}, None),
                        ]
                    )
                    self.vault.genesis(key, create=True)
                    break
                except Contended:
                    if attempt == 11:
                        raise BudgetDenied(
                            "Counter provisioning is busy; no allowance was reset"
                        ) from None

    def freeze(self, reason):
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 300:
            raise ValueError("Provide a recovery reason of 5–300 characters")
        if self.vault.read("fence") is None:
            self.vault.record("fence", {"reason": reason.strip()})
        for _ in range(12):
            meta, tag = self.rows.read("policy")
            self.validate_meta(meta)
            try:
                if not meta.get("frozen"):
                    meta = {
                        **meta,
                        "frozen": True,
                        "paused": True,
                        "revision": meta["revision"] + 1,
                    }
                    self.rows.commit([("policy", meta, tag)])
                self.vault.record(
                    "frozen-state", {"digest": meta["digest"], "revision": meta["revision"]}
                )
                return {"fenced": True, "policy_hash": meta["digest"]}
            except Contended:
                continue
        raise Contended("Generation is fenced; Table freeze could not be confirmed")

    def activate(self, policy, expected=None, at=None):
        """Never lower a cap below committed liability or unpause an accounting incident.

        Reservations update the policy ETag. Checking the counters and replacing that
        ETag makes a concurrent admission abort publication, even for 2,000 users.
        Settlement only reduces liability, so a concurrent settlement is safe.
        """
        self.vault.record("policies/" + policy_digest(policy), policy)
        meta, etag = self.rows.read("policy")
        if not meta:
            if self.vault.read("anchor") is not None:
                raise RecoveryError(
                    "The active policy row was deleted; refusing ledger reinitialization"
                )
            self.vault.anchor(create=True)
            meta = {
                "digest": policy_digest(policy),
                "format": 2,
                "revision": 0,
                "paused": True,
                "initializing": True,
            }
            self.rows.commit([("policy", meta, etag)])
        self.validate_meta(meta)
        self.vault.check_open()
        if meta.get("legacy_baseline") and meta.get("initializing"):
            raise RecoveryError("Operator migration is incomplete")
        if meta.get("recovering") or meta.get("frozen"):
            raise RecoveryError("Recovery requires explicit verification before publication")
        initial = meta.get("initializing", False)
        if meta.get("paused") and not initial:
            raise BudgetDenied("Resolve the accounting incident before publishing")
        if expected is not None and ("" if initial else meta["digest"]) != expected:
            raise Contended("The published budget policy changed")
        self.prepare_month(policy, at=at, initializing=initial)
        meta, etag = self.rows.read("policy")
        if not initial and expected is not None and meta["digest"] != expected:
            raise Contended("The published budget policy changed")
        month = (at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
        limits = {"org": micros(policy["settings"]["spend_limit_usd"])}
        groups = {g["id"]: g for g in policy["groups"]}
        for g in groups.values():
            limits["group-" + g["id"]] = min(
                micros(g["spend_ceiling_usd"]),
                micros(g["spend_limit_usd"])
                if g.get("spend_limit_usd") is not None
                else micros(g["spend_ceiling_usd"]),
            )
        for m in policy["members"]:
            limits["user-" + m["id"]] = (
                micros(m["spend_limit_usd"])
                if m.get("spend_limit_usd") is not None
                else limits["group-" + m["group_id"]]
            )
        for scope, limit in limits.items():
            counter, _ = self._counter(month + "-" + scope)
            if counter["spent"] + counter["reserved"] > limit:
                raise BudgetDenied(
                    "A new limit cannot be below this month's spent plus reserved amount"
                )
        digest = policy_digest(policy)
        if initial:
            from .budget_recovery import checkpoint

            checkpoint(self, SimpleNamespace(get=lambda h: self.vault.read("policies/" + h)))
        self.rows.commit(
            [
                (
                    "policy",
                    {
                        **meta,
                        "digest": digest,
                        "paused": False,
                        "initializing": False,
                        "revision": meta["revision"] + 1,
                        "policy_revision": meta["revision"] + 1,
                    },
                    etag,
                )
            ]
        )
        published, _ = self.rows.read("policy")
        self.vault.publication(published)
        return digest

    def active(self):
        meta, _ = self.rows.read("policy")
        self.validate_meta(meta)
        self.vault.check_open()
        if (
            meta.get("paused")
            or meta.get("initializing")
            or meta.get("recovering")
            or meta.get("frozen")
        ):
            raise BudgetDenied("The budget service is unavailable or paused")
        self.vault.publication(meta)
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
        if self.vault.read("reservations/" + request_id) is not None:
            raise BudgetDenied("This gateway request identifier has already been admitted")
        for _ in range(12):
            meta, meta_etag = self.rows.read("policy")
            self.validate_meta(meta)
            self.vault.check_open()
            if (
                meta.get("digest") != digest
                or meta.get("paused")
                or meta.get("initializing")
                or meta.get("recovering")
                or meta.get("frozen")
            ):
                raise BudgetDenied(
                    "Budget policy changed or is paused; retry with the current policy"
                )
            self.vault.publication(meta)
            existing, _ = self.rows.read(key)
            if existing:
                # Never dispatch an inference twice under a previously charged identifier.
                raise BudgetDenied("This gateway request identifier has already been admitted")
            writes = [("policy", {**meta, "revision": meta["revision"] + 1}, meta_etag)]
            counters = []
            for scope, limit in limits.items():
                counter_key = month + "-" + scope
                counter, etag = self._counter(counter_key)
                if counter["spent"] + counter["reserved"] + amount > limit:
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
                # A possibly billable dispatch cannot precede this independent receipt.
                self.vault.record("reservations/" + request_id, reservation)
                self.vault.check_open()
                latest, _ = self.rows.read("policy")
                self.validate_meta(latest)
                if latest.get("frozen") or latest.get("recovering"):
                    raise RecoveryError(
                        "Generation was fenced before dispatch; reservation remains held"
                    )
                return reservation
            except Contended:
                continue
        raise BudgetDenied("The budget ledger is busy; no inference was admitted")

    def settle(self, request_id, amount):
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid settlement amount")
        key = "request-" + str(UUID(request_id))
        for _ in range(12):
            self.vault.check_open()
            meta, meta_etag = self.rows.read("policy")
            self.validate_meta(meta)
            if meta.get("frozen") or meta.get("initializing") or meta.get("recovering"):
                raise RecoveryError("Settlement is blocked during recovery; allowance remains held")
            reservation, etag = self.rows.read(key)
            if not reservation:
                raise ValueError("Unknown reservation")
            if reservation["state"] == "settled":
                if reservation["charged"] != amount:
                    raise ValueError("Conflicting settlement; manual reconciliation required")
                return reservation
            if amount > reservation["reserved"]:
                # An invalid price/capacity bound must halt admission, not silently overdraw.
                self.vault.record(
                    "incidents/" + str(request_id),
                    {"request_id": str(request_id), "observed_micro_usd": amount},
                )
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
                                    "revision": meta["revision"] + 1,
                                },
                                meta_etag,
                            )
                        ]
                    )
                except Contended:
                    continue
                raise BudgetDenied("Usage exceeded its verified reservation; enforcement is paused")
            writes = [("policy", {**meta, "revision": meta["revision"] + 1}, meta_etag)]
            for counter_key in reservation["counters"]:
                counter, counter_etag = self._counter(counter_key)
                if counter["reserved"] < reservation["reserved"]:
                    raise ValueError("Ledger inconsistency; manual reconciliation required")
                counter["reserved"] -= reservation["reserved"]
                counter["spent"] += amount
                writes.append((counter_key, counter, counter_etag))
            # Also covers a previously ambiguous reservation write that never dispatched.
            self.vault.record(
                "reservations/" + str(request_id),
                {k: v for k, v in reservation.items() if k != "settled_at"},
            )
            self.vault.record(
                "settlements/" + str(request_id), {"request_id": str(request_id), "amount": amount}
            )
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

    def balances(self, policy, principal, at=None):
        _, _, limits = limits_for(policy, principal)
        month = (at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
        result = []
        for scope, limit in limits.items():
            value, _ = self._counter(month + "-" + scope)
            spent, reserved = value["spent"], value["reserved"]
            result.append(
                {
                    "scope": scope,
                    "limit_micro_usd": limit,
                    "spent_micro_usd": spent,
                    "reserved_micro_usd": reserved,
                    "remaining_micro_usd": max(0, limit - spent - reserved),
                }
            )
        return {"month": month, "currency": "USD", "scopes": result}
