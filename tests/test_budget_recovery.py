"""Data-loss and replay tests: never manufacture a new spending allowance."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_budget import admit
from test_budget import ledger as ledger
from test_budget import policy as policy

from claude_foundry.portal.budget_ledger import BudgetDenied, Ledger, MemoryRows
from claude_foundry.portal.budget_proxy import BudgetProxy
from claude_foundry.portal.budget_recovery import (
    RecoveryError,
    Vault,
    accounting,
    checkpoint,
    complete_restore,
    digest,
    migrate,
    restore,
    unseal,
)


class Policies:
    def __init__(self, policy):
        self.values = {digest(policy): copy.deepcopy(policy)}

    def get(self, key):
        return copy.deepcopy(self.values[key])

    def put(self, policy):
        self.values[digest(policy)] = copy.deepcopy(policy)
        return digest(policy)


def target_for(source):
    rows = MemoryRows()
    return Ledger(rows, Vault(source.vault.archive, rows.binding))


@pytest.mark.parametrize("scope", ["org", "group", "user"])
def test_deleted_used_counter_blocks_read_admission_and_republication(ledger, policy, scope):
    reservation = admit(ledger, policy)
    ledger.settle(reservation["request_id"], 12345)
    key = next(k for k in reservation["counters"] if k.split("-")[2] == scope)
    del ledger.rows.rows[key]
    with pytest.raises(RecoveryError, match="missing"):
        ledger.balances(policy, policy["members"][0]["id"])
    with pytest.raises(RecoveryError, match="missing|fenced"):
        admit(ledger, policy)
    with pytest.raises(RecoveryError, match="deleted|fenced"):
        ledger.activate(policy)
    assert key not in ledger.rows.rows


def test_deleted_control_cannot_be_bootstrapped_again(ledger, policy):
    del ledger.rows.rows["policy"]
    with pytest.raises(RecoveryError):
        ledger.active()
    with pytest.raises(RecoveryError, match="reinitialization"):
        ledger.activate(policy, expected="")


def test_deleted_request_cannot_reuse_protected_identifier(ledger, policy):
    held = admit(ledger, policy)
    del ledger.rows.rows["request-" + held["request_id"]]
    with pytest.raises(BudgetDenied, match="already"):
        admit(ledger, policy, request=held["request_id"])
    with pytest.raises(RecoveryError):
        checkpoint(ledger, Policies(policy))


def test_new_month_requires_guarded_provisioning_and_preserves_old_hold(ledger, policy):
    held = admit(ledger, policy)
    next_month = (datetime.now(UTC).replace(day=28) + timedelta(days=4)).replace(day=1)
    with pytest.raises(RecoveryError, match="missing"):
        admit(ledger, policy, at=next_month)
    ledger.prepare_month(policy, at=next_month)
    fresh = admit(ledger, policy, at=next_month)
    ledger.settle(held["request_id"], 1234)
    assert fresh["month"] != held["month"]
    assert ledger.rows.read(held["counters"][0])[0]["spent"] == 1234
    assert ledger.rows.read(fresh["counters"][0])[0]["spent"] == 0


@pytest.mark.parametrize(
    "value", [{}, {"spent": -1, "reserved": 0}, {"spent": True, "reserved": 0}, {"spent": 0}]
)
def test_invalid_counter_never_becomes_zero(ledger, policy, value):
    key = datetime.now(UTC).strftime("%Y-%m") + "-org"
    old, tag = ledger.rows.read(key)
    ledger.rows.commit([(key, {**value, "genesis": old["genesis"]}, tag)])
    with pytest.raises(RecoveryError):
        admit(ledger, policy)


def test_missing_journal_prevents_dispatch_but_keeps_committed_hold(ledger, policy):
    put = ledger.vault.archive.put
    calls = []

    def failing(name, raw):
        if "/reservations/" in name:
            raise TimeoutError("Independent backup store unavailable")
        return put(name, raw)

    ledger.vault.archive.put = failing

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200))
        ) as client:
            proxy = BudgetProxy(
                ledger,
                Policies(policy),
                SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test")),
                "https://test.services.ai.azure.com",
                {
                    "claude-sonnet-4-6": {
                        "provider": "anthropic",
                        "version": "1",
                        "max_input_tokens": 1000,
                        "max_output_tokens": 1000,
                    }
                },
                client,
            )
            with pytest.raises(TimeoutError):
                await proxy.infer(
                    "anthropic",
                    policy["members"][0]["id"],
                    str(uuid4()),
                    {"model": "claude-sonnet-4-6", "max_tokens": 100},
                )

    asyncio.run(run())
    assert calls == []
    assert (
        ledger.balances(policy, policy["members"][0]["id"])["scopes"][0]["reserved_micro_usd"]
        == 4500
    )


def test_settlement_evidence_precedes_release_and_survives_ambiguous_table_write(ledger, policy):
    policies = Policies(policy)
    held = admit(ledger, policy)
    backup = checkpoint(ledger, policies)
    commit = ledger.rows.commit

    def lost(rows):
        raise TimeoutError("Settlement could not commit")

    ledger.rows.commit = lost
    with pytest.raises(TimeoutError):
        ledger.settle(held["request_id"], 12345)
    ledger.rows.commit = commit
    assert ledger.rows.read(held["counters"][0])[0]["reserved"] == held["reserved"]
    ledger.freeze("Operator recovery test")
    target = target_for(ledger)
    result = restore(ledger, target, policies, backup["checkpoint"], "Restore terminal usage")
    values = target.balances(policy, policy["members"][0]["id"])["scopes"]
    assert all(v["spent_micro_usd"] == 12345 and v["reserved_micro_usd"] == 0 for v in values)
    assert result["paused"]


def test_restore_replays_new_requests_and_keeps_unknown_outcomes_reserved(ledger, policy):
    policies = Policies(policy)
    first = admit(ledger, policy)
    ledger.settle(first["request_id"], 25000)
    backup = checkpoint(ledger, policies)
    unknown = admit(ledger, policy, amount=200000)
    later = admit(ledger, policy, amount=300000)
    ledger.settle(later["request_id"], 40000)
    del ledger.rows.rows[unknown["counters"][2]]
    ledger.freeze("Counter lost in isolated test")
    target = target_for(ledger)
    result = restore(ledger, target, policies, backup["checkpoint"], "Replay protected tail")
    assert all(
        v["spent_micro_usd"] == 65000 and v["reserved_micro_usd"] == 200000
        for v in target.balances(policy, policy["members"][0]["id"])["scopes"]
    )
    with pytest.raises(BudgetDenied):
        admit(target, policy)
    complete_restore(
        target,
        ledger,
        policies,
        result["plan_hash"],
        result["policy_hash"],
        "Reviewed exact policy and held liabilities",
    )
    with pytest.raises(BudgetDenied, match="insufficient"):
        admit(target, policy, amount=800000)
    with pytest.raises(RecoveryError, match="fenced"):
        admit(ledger, policy)
    # A fresh worker has no cached markers and still sees the same liabilities.
    fresh = Ledger(target.rows, Vault(target.vault.archive, target.rows.binding))
    assert fresh.balances(policy, policy["members"][0]["id"]) == target.balances(
        policy, policy["members"][0]["id"]
    )


@pytest.mark.parametrize("kind", ["missing_part", "corrupt_part", "wrong_binding", "bad_manifest"])
def test_corrupt_or_cross_ledger_backup_cannot_write_target(ledger, policy, kind):
    policies = Policies(policy)
    backup = checkpoint(ledger, policies)
    identifier = backup["checkpoint"]
    prefix = ledger.vault.prefix + "checkpoints/" + identifier + "/"
    part = prefix + "part-00000.json"
    if kind == "missing_part":
        del ledger.vault.archive.objects[part]
    elif kind == "corrupt_part":
        ledger.vault.archive.objects[part] = b"corrupt"
    elif kind == "wrong_binding":
        identifier = "20260101T000000Z-" + uuid4().hex
    else:
        ledger.vault.archive.objects[prefix + "manifest.json"] = b"{}"
    ledger.freeze("Bad archive recovery test")
    target = target_for(ledger)
    with pytest.raises((RecoveryError, KeyError)):
        restore(ledger, target, policies, identifier, "Do not trust this backup")
    assert target.rows.scan() == {}


def test_restore_requires_fence_empty_target_and_exact_review(ledger, policy):
    policies = Policies(policy)
    backup = checkpoint(ledger, policies)
    target = target_for(ledger)
    with pytest.raises(RecoveryError, match="Fence"):
        restore(ledger, target, policies, backup["checkpoint"], "Operator restore test")
    ledger.freeze("Operator generation cutover")
    result = restore(ledger, target, policies, backup["checkpoint"], "Paused staging test")
    with pytest.raises(RecoveryError, match="empty"):
        restore(ledger, target, policies, backup["checkpoint"], "Do not overwrite staged data")
    with pytest.raises(RecoveryError):
        complete_restore(
            target, ledger, policies, "wrong", result["policy_hash"], "Wrong state review"
        )
    with pytest.raises(RecoveryError):
        complete_restore(
            target, ledger, policies, result["plan_hash"], "wrong", "Wrong policy review"
        )
    assert target.rows.read("policy")[0]["paused"]


def test_checkpoint_detects_mutation_during_export(ledger, policy):
    scan = ledger.rows.scan
    seen = []

    def racing():
        rows = scan()
        if not seen:
            seen.append(admit(ledger, policy))
        return rows

    ledger.rows.scan = racing
    result = checkpoint(ledger, Policies(policy))
    recovered = unseal(ledger.vault, "checkpoints", result["checkpoint"])
    assert "request-" + seen[0]["request_id"] in recovered["rows"]
    accounting(recovered["rows"])


def test_legacy_migration_preserves_liabilities_and_seals_before_enable(policy):
    modern = Ledger(MemoryRows())
    modern.activate(policy)
    held = admit(modern, policy)
    modern.settle(held["request_id"], 12345)
    unknown = admit(modern, policy)
    legacy = MemoryRows()
    for key, value in modern.rows.scan().items():
        value.pop("genesis", None)
        if key == "policy":
            value = {"digest": digest(policy), "paused": False}
        legacy.commit([(key, value, None)])
    migrated = Ledger(legacy)
    before = legacy.scan()
    result = migrate(migrated, Policies(policy), digest(before))
    assert result["checkpoint"]
    assert migrated.active() == digest(policy)
    values = migrated.balances(policy, policy["members"][0]["id"])["scopes"]
    assert all(
        v["spent_micro_usd"] == 12345 and v["reserved_micro_usd"] == unknown["reserved"]
        for v in values
    )


def test_overrun_incident_cannot_be_cleared_by_restore(ledger, policy):
    policies = Policies(policy)
    backup = checkpoint(ledger, policies)
    held = admit(ledger, policy)
    with pytest.raises(BudgetDenied):
        ledger.settle(held["request_id"], held["reserved"] + 1)
    ledger.freeze("Review exceeded pricing bound")
    target = target_for(ledger)
    result = restore(ledger, target, policies, backup["checkpoint"], "Preserve incident")
    assert result["incident"]
    with pytest.raises(RecoveryError, match="incidents"):
        complete_restore(
            target,
            ledger,
            policies,
            result["plan_hash"],
            result["policy_hash"],
            "Cannot waive an incident",
        )


def test_immutable_record_conflict_is_not_overwritten(ledger):
    ledger.vault.record("test", {"value": 1})
    with pytest.raises(RecoveryError, match="conflicts"):
        ledger.vault.record("test", {"value": 2})
    assert ledger.vault.read("test") == {"value": 1}
