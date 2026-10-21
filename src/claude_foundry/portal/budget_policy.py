"""Immutable reviewed snapshots; publication and admission share a ledger barrier."""

import json
import threading
from collections import OrderedDict
from concurrent.futures import Future

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient

from .budget_ledger import policy_digest
from .budget_pricing import BudgetPrice
from .domain import Group, Member, Settings


def portal_seed(initial, ledger, snapshots, owner_id):
    """Initialize a new portal from its existing ledger, without changing that ledger."""
    meta, _ = ledger.rows.read("policy")
    if not meta.get("digest"):
        return initial
    policy = snapshots.get(meta["digest"])
    if not any(
        m["id"] == owner_id and m["role"] == "owner" and m["status"] == "active"
        for m in policy["members"]
    ):
        raise ValueError("The configured portal owner is not an active owner of this ledger")
    # Preserve identities, ceilings and reviewed rates even if the ledger is
    # paused or rates expired. Owners need the portal to review those incidents.
    return {
        **initial,
        **{key: policy[key] for key in ("settings", "groups", "members", "budget_prices")},
    }


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
    """Cache verified immutable documents, never the ledger's active reference.

    Each instance owns its cache, so storage accounts cannot share entries. Serialized
    bytes prevent callers from modifying cached grants. Coalesce concurrent downloads
    of one digest without holding the cache lock during network I/O.
    """

    def __init__(self, account, credential, *, max_entries=8, max_bytes=8 * 1024 * 1024):
        if any(type(v) is not int or v < 0 for v in (max_entries, max_bytes)):
            raise ValueError("Snapshot cache limits must be nonnegative integers")
        self.blobs = ContainerClient(
            f"https://{account}.blob.core.windows.net", "budget-policies", credential=credential
        )
        self._max_entries, self._max_bytes = max_entries, max_bytes
        self._cache, self._loading = OrderedDict(), {}
        self._lock = threading.Lock()
        self._bytes = self._hits = self._misses = self._waits = self._evictions = 0

    def cache_info(self):
        """Process-local diagnostics only; no identities, policy hashes or documents."""
        with self._lock:
            return {
                "entries": len(self._cache),
                "bytes": self._bytes,
                "hits": self._hits,
                "misses": self._misses,
                "waits": self._waits,
                "evictions": self._evictions,
            }

    def put(self, policy):
        digest = policy_digest(policy)
        try:
            self.blobs.upload_blob(
                digest + ".json", json.dumps(policy, sort_keys=True), overwrite=False
            )
        except ResourceExistsError:
            # Publication must verify the durable object, even if a prior read is warm.
            if json.loads(self._download(digest)) != policy:
                raise ValueError("Immutable budget policy does not match its digest") from None
        return digest

    def get(self, digest):
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("Invalid policy digest")
        with self._lock:
            raw = self._cache.get(digest)
            if raw is not None:
                self._cache.move_to_end(digest)
                self._hits += 1
            else:
                pending = self._loading.get(digest)
                leader = pending is None
                if leader:
                    pending = self._loading[digest] = Future()
                    self._misses += 1
                else:
                    self._waits += 1
        if raw is not None:
            return json.loads(raw)
        if leader:
            try:
                raw = self._download(digest)
                with self._lock:
                    # Oversized documents still work, but are never retained in cache.
                    if self._max_entries and len(raw) <= self._max_bytes:
                        self._cache[digest] = raw
                        self._bytes += len(raw)
                        while len(self._cache) > self._max_entries or self._bytes > self._max_bytes:
                            _, evicted = self._cache.popitem(last=False)
                            self._bytes -= len(evicted)
                            self._evictions += 1
                    del self._loading[digest]
                pending.set_result(raw)
            except BaseException as error:
                with self._lock:
                    self._loading.pop(digest, None)
                pending.set_exception(error)
                raise
        # A fresh parse isolates each caller, including concurrent miss waiters.
        return json.loads(pending.result())

    def _download(self, digest):
        raw = self.blobs.download_blob(digest + ".json").readall()
        policy = json.loads(raw)
        if policy_digest(policy) != digest:
            raise ValueError("Published budget policy integrity check failed")
        return raw


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
