"""Independent immutable recovery records. No prompts, responses or credentials.

Reservation receipts precede model dispatch; settlement evidence precedes release.
Checkpoints are consistent Table exports. Restore always stages a different,
paused partition and replays receipts conservatively before explicit completion.
"""

import copy
import hashlib
import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContainerClient, ContentSettings

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_ROWS = 100_000
PART_SIZE = 4 * 1024 * 1024


class RecoveryError(Exception):
    """Unproven integrity or recovery state; never release allowance."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def stamp():
    return datetime.now(UTC).isoformat()


def open_ledger(account, organization_id, credential, recovery_account):
    from .budget_ledger import AzureRows, Ledger
    from .service import ManagedAzure

    if not recovery_account:
        raise RecoveryError("Configure protected recovery storage before starting a budget service")
    rows = AzureRows(account, organization_id, credential)
    resource = os.getenv("BUDGET_RECOVERY_STORAGE_RESOURCE_ID", "")
    if not re.fullmatch(
        r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/Microsoft.Storage/storageAccounts/"
        + recovery_account,
        resource,
    ):
        raise RecoveryError("Configure the exact recovery storage resource ID")
    azure = ManagedAzure(resource.split("/")[2], credential)

    def protection():
        storage = azure.request("GET", resource, version="2023-05-01")
        policy = azure.request(
            "GET",
            resource
            + "/blobServices/default/containers/budget-recovery/immutabilityPolicies/default",
            version="2023-05-01",
        )["properties"]
        p = storage["properties"]
        if (
            policy["state"] != "Locked"
            or policy["immutabilityPeriodSinceCreationInDays"] < 35
            or p.get("publicNetworkAccess") != "Disabled"
            or p.get("allowSharedKeyAccess") is not False
        ):
            raise RecoveryError("Recovery storage protections are not verified")
        return {
            "resource": resource,
            "created_at": p["creationTime"],
            "last_failover": p.get("lastGeoFailoverTime") or "",
        }

    return Ledger(rows, Vault(BlobArchive(recovery_account, credential), rows.binding, protection))


class MemoryArchive:
    """Deterministic test adapter. Shares its protected data across worker objects."""

    def __init__(self):
        self.objects, self.lock = {}, threading.RLock()

    def get(self, name):
        with self.lock:
            return self.objects.get(name)

    def put(self, name, raw):
        with self.lock:
            if name in self.objects and self.objects[name] != raw:
                raise RecoveryError("An immutable recovery record conflicts")
            self.objects[name] = raw

    def names(self, prefix):
        with self.lock:
            return sorted(k for k in self.objects if k.startswith(prefix))


class BlobArchive:
    """Managed identity only; the deployment must lock container retention."""

    def __init__(self, account, credential):
        if not re.fullmatch(r"[a-z0-9]{3,24}", account):
            raise ValueError("Invalid recovery storage account")
        self.container = ContainerClient(
            f"https://{account}.blob.core.windows.net",
            "budget-recovery",
            credential=credential,
            retry_total=2,
            connection_timeout=5,
            read_timeout=20,
        )

    def get(self, name):
        try:
            blob = self.container.get_blob_client(name)
            if blob.get_blob_properties().size > PART_SIZE + 1024:
                raise RecoveryError("Recovery object exceeds its bound")
            return blob.download_blob().readall()
        except ResourceNotFoundError:
            return None

    def put(self, name, raw):
        if len(raw) > PART_SIZE + 1024:
            raise RecoveryError("Recovery object exceeds its bound")
        try:
            self.container.upload_blob(
                name,
                raw,
                overwrite=False,
                content_settings=ContentSettings(
                    content_type="application/json", cache_control="no-store"
                ),
            )
        except ResourceExistsError:
            if self.get(name) != raw:
                raise RecoveryError("An immutable recovery record conflicts") from None

    def names(self, prefix):
        result = []
        for item in self.container.list_blobs(name_starts_with=prefix):
            result.append(item.name)
            if len(result) > MAX_ROWS * 3:
                raise RecoveryError("Recovery listing exceeds the reviewed bound")
        return result


class Vault:
    def __init__(self, archive, binding, protection=None):
        if isinstance(archive, BlobArchive) and protection is None:
            raise RecoveryError("Azure recovery storage requires a protection verifier")
        self.archive, self.binding = archive, binding
        self.protection = protection
        self._baseline, self._checked = None, 0
        self.prefix = "ledgers/" + hashlib.sha256(binding.encode()).hexdigest() + "/"
        self._genesis, self._publications, self._anchor = set(), set(), False

    def record(self, key, value):
        payload = {"schema": 1, "binding": self.binding, "value": value}
        self.archive.put(
            self.prefix + key + ".json", encoded({"digest": digest(payload), **payload})
        )

    def read(self, key):
        raw = self.archive.get(self.prefix + key + ".json")
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            expected = value.pop("digest")
            if (
                value["schema"] != 1
                or value["binding"] != self.binding
                or digest(value) != expected
            ):
                raise ValueError()
            return value["value"]
        except (ValueError, KeyError, TypeError):
            raise RecoveryError("Recovery record integrity check failed") from None

    def records(self, kind):
        prefix = self.prefix + kind + "/"
        for name in self.archive.names(prefix):
            yield name[len(self.prefix) : -5], self.read(name[len(self.prefix) : -5])

    def latest(self, kind, limit=20):
        names = sorted(self.archive.names(self.prefix + kind + "/"), reverse=True)[:limit]
        return [self.read(name[len(self.prefix) : -5]) for name in names]

    def anchor(self, create=False):
        if not self._anchor:
            value = self.read("anchor")
            if value is None and create:
                self.record(
                    "anchor",
                    {"format": 2, "protection": self.protection() if self.protection else None},
                )
                value = self.read("anchor")
            if not isinstance(value, dict) or value.get("format") != 2:
                raise RecoveryError("Ledger recovery anchor is missing; operator recovery required")
            self._baseline = value.get("protection")
            self._anchor = True
            self.verify_protection()

    def verify_protection(self, force=True):
        self.anchor()
        if self.protection and (force or time.monotonic() >= self._checked):
            if self.protection() != self._baseline:
                raise RecoveryError(
                    "Recovery account was recreated or failed over; journal reconciliation is required before reopening"
                )
            self._checked = time.monotonic() + 60

    def check_open(self):
        self.anchor()
        self.verify_protection(force=False)
        if self.read("fence") is not None:
            raise RecoveryError("Ledger generation is permanently fenced for recovery")

    def genesis(self, key, create=False):
        expected = digest({"binding": self.binding, "counter": key})
        if key not in self._genesis:
            value = self.read("genesis/" + key)
            if value is None and create:
                self.record("genesis/" + key, {"counter": key, "genesis": expected})
                value = self.read("genesis/" + key)
            if value != {"counter": key, "genesis": expected}:
                raise RecoveryError("Counter initialization evidence is missing")
            self._genesis.add(key)
        return expected

    def has_genesis(self, key):
        return key in self._genesis or self.read("genesis/" + key) is not None

    def publication(self, meta):
        revision = meta.get("policy_revision")
        if type(revision) is not int or revision < 1:
            raise RecoveryError("Published policy has no recovery revision")
        key = (revision, meta["digest"])
        if key not in self._publications:
            self.record(
                "publications/" + f"{revision:020d}",
                {"revision": revision, "policy_hash": meta["digest"]},
            )
            self._publications.add(key)


def expected_counters(policy, month):
    return (
        [month + "-org"]
        + [month + "-group-" + g["id"] for g in policy["groups"]]
        + [month + "-user-" + m["id"] for m in policy["members"]]
    )


def validate_counter(value, genesis=None):
    if (
        not isinstance(value, dict)
        or any(type(value.get(k)) is not int or value[k] < 0 for k in ("spent", "reserved"))
        or (genesis is not None and value.get("genesis") != genesis)
    ):
        raise RecoveryError("A spending counter is missing, malformed or belongs to another ledger")


def validate_reservation(value):
    try:
        request, principal, group = (
            str(UUID(value[k])) for k in ("request_id", "principal_id", "group_id")
        )
        month = value["month"]
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError()
        if value["counters"] != [
            month + "-org",
            month + "-group-" + group,
            month + "-user-" + principal,
        ]:
            raise ValueError()
        if type(value["reserved"]) is not int or value["reserved"] <= 0:
            raise ValueError()
        if value["state"] not in {"reserved", "settled"}:
            raise ValueError()
        if value["state"] == "settled" and (
            type(value.get("charged")) is not int or not 0 <= value["charged"] <= value["reserved"]
        ):
            raise ValueError()
        return request
    except (ValueError, KeyError, TypeError):
        raise RecoveryError("A reservation has invalid recovery evidence") from None


def accounting(rows):
    """Validate all counters against retained request records, including old months."""
    totals = {}
    for key, value in rows.items():
        if not key.startswith("request-"):
            continue
        if key != "request-" + validate_reservation(value):
            raise RecoveryError("A request row has the wrong identity")
        for counter in value["counters"]:
            total = totals.setdefault(counter, {"spent": 0, "reserved": 0})
            total["spent" if value["state"] == "settled" else "reserved"] += (
                value["charged"] if value["state"] == "settled" else value["reserved"]
            )
    for key, value in rows.items():
        if re.match(r"^\d{4}-\d{2}-", key):
            validate_counter(value)
            if {k: value[k] for k in ("spent", "reserved")} != totals.pop(
                key, {"spent": 0, "reserved": 0}
            ):
                raise RecoveryError("Spending counters do not match retained request records")
        elif key != "policy" and not key.startswith("request-"):
            raise RecoveryError("Unknown ledger row; refusing an incomplete recovery")
    if totals:
        raise RecoveryError("A counter referenced by a request is missing")


def seal(vault, kind, value):
    """Write bounded chunks, then an immutable manifest; partial uploads are not backups."""
    raw = encoded(value)
    if len(raw) > MAX_ARCHIVE:
        raise RecoveryError("Recovery archive exceeds the reviewed 64 MiB bound")
    identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex
    base = kind + "/" + identifier
    parts = []
    for index, offset in enumerate(range(0, len(raw), PART_SIZE)):
        part = raw[offset : offset + PART_SIZE]
        name = vault.prefix + base + f"/part-{index:05d}.json"
        vault.archive.put(name, part)
        parts.append({"name": name, "sha256": hashlib.sha256(part).hexdigest(), "bytes": len(part)})
    vault.record(
        base + "/manifest",
        {
            "created_at": stamp(),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "parts": parts,
        },
    )
    return identifier


def unseal(vault, kind, identifier):
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{32}", identifier):
        raise RecoveryError("Invalid archive identifier")
    base = kind + "/" + identifier
    manifest = vault.read(base + "/manifest")
    if not manifest or not 0 < manifest["bytes"] <= MAX_ARCHIVE or len(manifest["parts"]) > 16:
        raise RecoveryError("Missing or oversized recovery manifest")
    raw = bytearray()
    for index, part in enumerate(manifest["parts"]):
        if part["name"] != vault.prefix + base + f"/part-{index:05d}.json":
            raise RecoveryError("Archive part escapes its ledger")
        chunk = vault.archive.get(part["name"])
        if (
            chunk is None
            or len(chunk) != part["bytes"]
            or hashlib.sha256(chunk).hexdigest() != part["sha256"]
        ):
            raise RecoveryError("Missing or corrupt backup part")
        raw.extend(chunk)
    if len(raw) != manifest["bytes"] or hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise RecoveryError("Recovery archive checksum mismatch")
    return json.loads(raw)


def checkpoint(ledger, snapshots, portal_state=None):
    ledger.vault.verify_protection()
    for _ in range(5):
        before, etag = ledger.rows.read("policy")
        ledger.validate_meta(before)
        rows = ledger.rows.scan()
        after, last = ledger.rows.read("policy")
        if etag != last or before != after:
            continue
        accounting(rows)
        for _, evidence in ledger.vault.records("genesis"):
            key = evidence["counter"]
            validate_counter(rows.get(key), evidence["genesis"])
        for _, evidence in ledger.vault.records("reservations"):
            key = "request-" + validate_reservation(evidence)
            if key not in rows:
                raise RecoveryError("An admitted request is missing from the ledger")
        # Receipt writers run after admission; a concurrent mutation invalidates this export.
        if ledger.rows.read("policy")[1] != etag:
            continue
        hashes = {before["digest"]} | {
            v["policy"] for k, v in rows.items() if k.startswith("request-")
        }
        policies = {h: snapshots.get(h) for h in hashes}
        value = {
            "schema": 2,
            "binding": ledger.rows.binding,
            "at": stamp(),
            "rows": rows,
            "policies": policies,
            "portal_state": portal_state,
        }
        identifier = seal(ledger.vault, "checkpoints", value)
        result = {
            "checkpoint": identifier,
            "rows": len(rows),
            "revision": before["revision"],
            "state_hash": digest(rows),
            "at": value["at"],
        }
        ledger.vault.record("checkpoint-reports/" + identifier, result)
        return result
    raise RecoveryError("Ledger remained busy; no consistent checkpoint was sealed")


def migrate(ledger, snapshots, expected_hash):
    """Explicit legacy cutover only, after old writers are stopped by the operator."""
    from .budget_ledger import Contended

    rows = ledger.rows.scan()
    if digest(rows) != expected_hash:
        raise Contended("Ledger changed since migration review")
    original = rows.get("policy", {})
    if not original.get("digest") or original.get("format") == 2:
        raise RecoveryError("Migration requires an existing legacy ledger")
    if ledger.vault.read("anchor") is not None:
        raise RecoveryError("Recovery anchor already exists; do not reinitialize this ledger")
    accounting(rows)
    policy = snapshots.get(original["digest"])
    baseline = seal(
        ledger.vault,
        "legacy-baselines",
        {"binding": ledger.rows.binding, "rows": rows, "policy": policy},
    )
    ledger.vault.anchor(create=True)
    meta, etag = ledger.rows.read("policy")
    if meta != original:
        raise Contended("Legacy writer changed the ledger; migration remains blocked")
    ledger.rows.commit(
        [
            (
                "policy",
                {
                    **meta,
                    "format": 2,
                    "revision": 0,
                    "initializing": True,
                    "paused": True,
                    "legacy_baseline": baseline,
                },
                etag,
            )
        ]
    )
    for key, value in rows.items():
        if re.match(r"^\d{4}-\d{2}-", key):
            current, tag = ledger.rows.read(key)
            if current != value:
                raise RecoveryError("A legacy writer is still changing counters")
            marker = digest({"binding": ledger.rows.binding, "counter": key})
            ledger._write([(key, {**current, "genesis": marker}, tag)], initializing=True)
            ledger.vault.genesis(key, create=True)
    ledger.prepare_month(policy, initializing=True)
    for h in {original["digest"]} | {
        v["policy"] for k, v in rows.items() if k.startswith("request-")
    }:
        ledger.vault.record("policies/" + h, snapshots.get(h))
    meta, etag = ledger.rows.read("policy")
    result = checkpoint(ledger, snapshots)
    ledger.rows.commit(
        [
            (
                "policy",
                {
                    **meta,
                    "initializing": False,
                    "paused": original.get("paused", False),
                    "revision": meta["revision"] + 1,
                    "policy_revision": meta["revision"] + 1,
                },
                etag,
            )
        ]
    )
    final, _ = ledger.rows.read("policy")
    ledger.vault.publication(final)
    return result


def restore(source, target, snapshots, identifier, reason):
    """Replay evidence into a different empty partition; never overwrite source."""
    if (
        source.rows.binding == target.rows.binding
        or target.rows.scan()
        or target.vault.read("anchor")
    ):
        raise RecoveryError("Restore requires a different, empty target ledger")
    if not source.vault.read("fence"):
        raise RecoveryError("Fence the source generation before restoring")
    archive = unseal(source.vault, "checkpoints", identifier)
    if archive["binding"] != source.rows.binding:
        raise RecoveryError("Backup belongs to another ledger")
    rows = copy.deepcopy(archive["rows"])
    accounting(rows)
    for _, entry in source.vault.records("reservations"):
        key = "request-" + validate_reservation(entry)
        if key in rows:
            previous = rows[key]
            if any(
                previous.get(k) != entry.get(k)
                for k in (
                    "request_id",
                    "principal_id",
                    "group_id",
                    "month",
                    "reserved",
                    "policy",
                    "fingerprint",
                    "counters",
                )
            ):
                raise RecoveryError("Conflicting reservation recovery evidence")
        else:
            rows[key] = entry
    for _, entry in source.vault.records("settlements"):
        request = rows.get("request-" + str(UUID(entry["request_id"])))
        if not request:
            raise RecoveryError("Settlement has no reservation evidence")
        amount = entry["amount"]
        if type(amount) is not int or not 0 <= amount <= request["reserved"]:
            raise RecoveryError("Invalid settlement recovery amount")
        if request["state"] == "settled" and request["charged"] != amount:
            raise RecoveryError("Conflicting settlement evidence")
        request.update(state="settled", charged=amount)
    # Rebuild from retained requests, not stale or externally supplied balances.
    for key in list(rows):
        if re.match(r"^\d{4}-\d{2}-", key):
            rows[key] = {"spent": 0, "reserved": 0}
    for key, value in list(rows.items()):
        if key.startswith("request-"):
            validate_reservation(value)
            for counter in value["counters"]:
                total = rows.setdefault(counter, {"spent": 0, "reserved": 0})
                field = "spent" if value["state"] == "settled" else "reserved"
                total[field] += value["charged"] if field == "spent" else value["reserved"]
    frozen = source.vault.read("frozen-state")
    policy_hash = frozen["digest"] if frozen else rows["policy"]["digest"]
    publications = [v for _, v in source.vault.records("publications")]
    if not frozen and publications:
        policy_hash = max(publications, key=lambda v: v["revision"])["policy_hash"]
    policies = archive["policies"]
    for key, policy in source.vault.records("policies"):
        policies[key.split("/")[-1]] = policy
    if policy_hash not in policies or any(digest(p) != h for h, p in policies.items()):
        raise RecoveryError("Recovery policy evidence is missing or corrupt")
    incident = bool(list(source.vault.records("incidents"))) or bool(
        rows["policy"].get("incident_request_id")
    )
    rows["policy"] = {
        **rows["policy"],
        "digest": policy_hash,
        "format": 2,
        "revision": 0,
        "paused": True,
        "recovering": True,
        "initializing": False,
        "frozen": False,
        "restore_id": identifier,
        "source_binding": source.rows.binding,
        "incident": incident,
    }
    for key in rows:
        if re.match(r"^\d{4}-\d{2}-", key):
            rows[key]["genesis"] = digest({"binding": target.rows.binding, "counter": key})
    accounting(rows)
    plan_hash = digest(rows)
    target.vault.record(
        "restore-plan",
        {
            "source": source.rows.binding,
            "checkpoint": identifier,
            "state_hash": plan_hash,
            "policy_hash": policy_hash,
            "reason": reason,
            "at": stamp(),
            "requires_policy_review": frozen is None,
        },
    )
    target.vault.anchor(create=True)
    # A partially written target always has a paused control row first.
    target.rows.commit([("policy", rows["policy"], None)])
    items = [(k, v, None) for k, v in rows.items() if k != "policy"]
    for offset in range(0, len(items), 90):
        target.rows.commit(items[offset : offset + 90])
    for key in rows:
        if re.match(r"^\d{4}-\d{2}-", key):
            target.vault.genesis(key, create=True)
        elif key.startswith("request-"):
            reservation = {k: v for k, v in rows[key].items() if k != "settled_at"}
            reservation.update(state="reserved", charged=None)
            target.vault.record("reservations/" + reservation["request_id"], reservation)
            if rows[key]["state"] == "settled":
                target.vault.record(
                    "settlements/" + reservation["request_id"],
                    {"request_id": reservation["request_id"], "amount": rows[key]["charged"]},
                )
    for h, policy in policies.items():
        if snapshots.put(policy) != h:
            raise RecoveryError("Restored policy hash changed")
        target.vault.record("policies/" + h, policy)
    if digest(target.rows.scan()) != plan_hash:
        raise RecoveryError("Staged target verification failed; it remains paused")
    verified = checkpoint(target, snapshots)
    target.vault.record(
        "restore-verified", {"plan_hash": plan_hash, "checkpoint": verified["checkpoint"]}
    )
    return {
        **verified,
        "staged": True,
        "paused": True,
        "plan_hash": plan_hash,
        "policy_hash": policy_hash,
        "incident": incident,
    }


def complete_restore(target, source, snapshots, expected_hash, policy_hash, reason):
    source.vault.verify_protection()
    target.vault.verify_protection()
    if not source.vault.read("fence"):
        raise RecoveryError("Source generation is not fenced")
    plan = target.vault.read("restore-plan")
    verified = target.vault.read("restore-verified")
    rows = target.rows.scan()
    if (
        not plan
        or plan["source"] != source.rows.binding
        or digest(rows) != expected_hash
        or plan["state_hash"] != expected_hash
    ):
        raise RecoveryError("Restore review no longer matches the staged target")
    if (
        not verified
        or verified["plan_hash"] != expected_hash
        or digest(unseal(target.vault, "checkpoints", verified["checkpoint"])["rows"])
        != expected_hash
    ):
        raise RecoveryError("A fully sealed, verified staging checkpoint is required")
    meta = rows["policy"]
    if not meta.get("recovering") or meta.get("incident") or meta["digest"] != policy_hash:
        raise RecoveryError("Resolve incidents and review the exact recovered policy first")
    accounting(rows)
    target.vault.record(
        "restore-approval",
        {"plan_hash": expected_hash, "policy_hash": policy_hash, "reason": reason},
    )
    _, etag = target.rows.read("policy")
    target.rows.commit(
        [
            (
                "policy",
                {
                    **meta,
                    "paused": False,
                    "recovering": False,
                    "revision": meta["revision"] + 1,
                    "policy_revision": meta["revision"] + 1,
                },
                etag,
            )
        ]
    )
    final, _ = target.rows.read("policy")
    target.vault.publication(final)
    return checkpoint(target, snapshots)


def run_drill(source_factory, archive, snapshots, protection=None):
    """Storage-only synthetic drill. No caller may choose the source or target ID."""
    from .budget_ledger import BudgetDenied, Ledger

    primary, secondary, principal, group = (str(uuid4()) for _ in range(4))
    rows, target_rows = source_factory(primary), source_factory(secondary)
    source = Ledger(rows, Vault(archive, rows.binding, protection))
    target = Ledger(target_rows, Vault(archive, target_rows.binding, protection))
    policy = {
        "schema": 1,
        "settings": {
            "name": "Synthetic recovery drill",
            "budget_enforcement": True,
            "spend_limit_usd": "1",
        },
        "groups": [
            {
                "id": group,
                "spend_ceiling_usd": "1",
                "spend_limit_usd": "1",
                "model_ids": ["recovery-drill-only"],
                "delegated_models": ["recovery-drill-only"],
            }
        ],
        "members": [
            {
                "id": principal,
                "role": "owner",
                "status": "active",
                "group_id": group,
                "spend_limit_usd": "1",
                "extra_models": [],
            }
        ],
        "budget_prices": [],
    }
    snapshots.put(policy)
    source.activate(policy)

    def reserve(ledger, amount):
        return ledger.reserve(
            str(uuid4()), principal, "recovery-drill-only", amount, policy, "synthetic-no-inference"
        )

    paid = reserve(source, 100000)
    source.settle(paid["request_id"], 25000)
    backup = checkpoint(source, snapshots)
    reserve(source, 200000)  # An uncertain outcome must remain held after restore.
    tail = reserve(source, 300000)
    source.settle(tail["request_id"], 40000)
    key = tail["counters"][2]
    if hasattr(rows, "table"):
        rows.table.delete_entity(rows.partition, key)
    else:
        del rows.rows[key]
    checks = {}
    for label, action in [
        ("missing_counter_denied", lambda: reserve(source, 1)),
        ("zero_reinitialization_denied", lambda: source.activate(policy)),
        ("incomplete_checkpoint_denied", lambda: checkpoint(source, snapshots)),
    ]:
        try:
            action()
            raise AssertionError(label)
        except RecoveryError:
            checks[label] = True
    source.freeze("Synthetic recovery drill; no inference service uses this partition")
    staged = restore(
        source, target, snapshots, backup["checkpoint"], "Synthetic receipt replay test"
    )
    try:
        reserve(target, 1)
        raise AssertionError("Target was admitted before review")
    except BudgetDenied:
        checks["paused_target_denied"] = True
    balances = target.balances(policy, principal)["scopes"]
    if not all(
        s["spent_micro_usd"] == 65000 and s["reserved_micro_usd"] == 200000 for s in balances
    ):
        raise RecoveryError("Synthetic replay did not retain all liabilities")
    complete_restore(
        target,
        source,
        snapshots,
        staged["plan_hash"],
        staged["policy_hash"],
        "Reviewed synthetic replay, retained unknown holds and exact policy",
    )
    try:
        reserve(target, 800000)
        raise AssertionError("Restored limits were bypassed")
    except BudgetDenied:
        checks["restored_budget_denied"] = True
    fresh = Ledger(target_rows, Vault(archive, target_rows.binding, protection))
    if fresh.balances(policy, principal)["scopes"] != balances:
        raise RecoveryError("Fresh worker changed recovered balances")
    checks["fresh_worker_preserves_liability"] = True
    checks["tail_replayed_without_refund"] = True
    # Probe only a newly created synthetic blob, never a customer checkpoint.
    probe = source.vault.prefix + "immutability-probe.json"
    archive.put(probe, b'{"synthetic":true}')
    if isinstance(archive, BlobArchive):
        from azure.core.exceptions import HttpResponseError

        blob = archive.container.get_blob_client(probe)
        for label, action in [
            ("overwrite_denied", lambda: blob.upload_blob(b'{"synthetic":false}', overwrite=True)),
            ("delete_denied", blob.delete_blob),
        ]:
            try:
                action()
                raise RecoveryError("Backup protection probe unexpectedly succeeded")
            except HttpResponseError as error:
                if error.status_code not in {403, 409}:
                    raise
                checks[label] = True
    report = {
        "passed": True,
        "checks": checks,
        "source_partition": primary,
        "target_partition": secondary,
        "spent_micro_usd": 65000,
        "reserved_micro_usd": 200000,
        "model_requests": 0,
        "at": stamp(),
    }
    source.vault.record("drill-result", report)
    return report
