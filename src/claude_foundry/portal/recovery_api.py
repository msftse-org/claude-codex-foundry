"""Recovery operations require both a current portal owner and an explicit operator ID."""

import logging
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .budget_ledger import AzureRows, Contended, Ledger, MemoryRows
from .budget_recovery import (
    RecoveryError,
    Vault,
    checkpoint,
    complete_restore,
    digest,
    migrate,
    restore,
    run_drill,
    stamp,
    unseal,
)
from .domain import audit

LOG = logging.getLogger(__name__)


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=300)
    confirmation: str
    checkpoint: str = ""
    target_partition: UUID | None = None
    state_hash: str = ""
    policy_hash: str = ""


def install_recovery(app, budget, storage, actor_dependency, credential):
    import json

    operators = {
        str(UUID(value)) for value in json.loads(os.getenv("BUDGET_RECOVERY_OPERATOR_IDS", "[]"))
    }
    enabled = bool(budget and operators)
    drill_enabled = os.getenv("BUDGET_RECOVERY_DRILL_ENABLED") == "1"
    app.state.recovery_checkpoint = None
    if budget is None:
        return None
    ledger, snapshots = budget
    app.state.budget = budget
    memory_targets = {}

    def operator(access):
        data, etag, member = access
        if not enabled:
            raise HTTPException(404, "Recovery operations are not configured")
        if member["role"] != "owner" or member["id"] not in operators:
            raise HTTPException(
                403, "An explicitly designated recovery operator with owner access is required"
            )
        return data, etag, member

    def reviewed(body, access, action):
        data, etag, member = operator(access)
        if body.confirmation != data["settings"]["name"]:
            raise HTTPException(409, "Confirm the exact organization name")
        audit(
            data,
            member,
            "budget.recovery." + action,
            "protected-ledger",
            None,
            {
                "reason": body.reason.strip(),
                "checkpoint": body.checkpoint,
                "target_partition": str(body.target_partition) if body.target_partition else None,
            },
        )
        data["revision"] += 1
        storage().save(data, etag)

    def rows_for(partition):
        partition = str(UUID(partition))
        if isinstance(ledger.rows, AzureRows):
            return AzureRows(ledger.rows.account, partition, credential)
        return memory_targets.setdefault(partition, MemoryRows())

    def target_for(partition):
        rows = rows_for(partition)
        return Ledger(rows, Vault(ledger.vault.archive, rows.binding, ledger.vault.protection))

    def execute(operation):
        try:
            return operation()
        except HTTPException:
            raise
        except (RecoveryError, Contended, ValueError) as error:
            raise HTTPException(409, str(error)) from None
        except Exception:
            LOG.exception("Budget recovery operation failed; inspect retained operator evidence")
            raise HTTPException(
                503,
                "Recovery operation could not be confirmed. Inspect status before retrying; no automatic cutover occurred.",
            ) from None

    def scheduled():
        meta, _ = ledger.rows.read("policy")
        if meta.get("format") != 2 or meta.get("initializing"):
            LOG.warning("BUDGET_RECOVERY_MIGRATION_REQUIRED: no checkpoint attempted")
            return {"migration_required": True}
        if not meta.get("paused") and not ledger.vault.read("fence"):
            policy = snapshots.get(ledger.active())
            ledger.prepare_month(policy)
            next_month = (datetime.now(UTC).replace(day=28) + timedelta(days=4)).replace(day=1)
            ledger.prepare_month(policy, at=next_month)
        try:
            result = checkpoint(ledger, snapshots, portal_state=storage().read()[0])
        except RecoveryError:
            LOG.exception("BUDGET_RECOVERY_CHECKPOINT_FAILED: no incomplete backup was published")
            raise
        LOG.warning(
            "BUDGET_RECOVERY_CHECKPOINT_OK revision=%s rows=%s", result["revision"], result["rows"]
        )
        return result

    app.state.recovery_checkpoint = scheduled

    @app.get("/api/budgets/recovery/status")
    def status(access=actor_dependency):
        operator(access)

        def read():
            meta, _ = ledger.rows.read("policy")
            reports = ledger.vault.latest("checkpoint-reports")
            reports.sort(key=lambda value: value["checkpoint"], reverse=True)
            latest = reports[0]["at"] if reports else None
            stale = not latest or datetime.now(UTC) - datetime.fromisoformat(latest) > timedelta(
                minutes=75
            )
            targets = []
            for entry in ledger.vault.latest("restore-targets"):
                target = target_for(entry["target_partition"])
                control, _ = target.rows.read("policy")
                plan = target.vault.read("restore-plan") or {}
                verified = target.vault.read("restore-verified")
                targets.append(
                    {
                        **entry,
                        "verified": bool(verified),
                        "ready": bool(
                            control and not control.get("paused") and not control.get("recovering")
                        ),
                        "plan_hash": plan.get("state_hash", ""),
                        "policy_hash": plan.get("policy_hash", ""),
                    }
                )
            protection_error = ""
            try:
                if ledger.vault.read("anchor"):
                    ledger.vault.verify_protection()
                elif ledger.vault.protection:
                    ledger.vault.protection()
            except RecoveryError as error:
                protection_error = str(error)
            return {
                "configured": True,
                "format": meta.get("format", 1),
                "migration_required": meta.get("format") != 2,
                "fenced": ledger.vault.read("fence") is not None,
                "drill_enabled": drill_enabled,
                "migration_enabled": os.getenv("BUDGET_RECOVERY_MIGRATION_ENABLED") == "1",
                "checkpoints": reports[:20],
                "checkpoint_stale": stale,
                "schedule_minutes": 30,
                "retention_minimum_days": 35,
                "protection_verified": not protection_error,
                "protection_error": protection_error,
                "targets": targets,
                "last_drill": next(iter(ledger.vault.latest("drill-reports", 1)), None),
            }

        return execute(read)

    @app.post("/api/budgets/recovery/checkpoint")
    def backup(body: RecoveryAction, access=actor_dependency):
        reviewed(body, access, "checkpoint_requested")
        return execute(scheduled)

    @app.post("/api/budgets/recovery/migration-plan")
    def migration_plan(body: RecoveryAction, access=actor_dependency):
        reviewed(body, access, "migration_review")
        return execute(
            lambda: {
                "state_hash": digest(ledger.rows.scan()),
                "migration_required": ledger.rows.read("policy")[0].get("format") != 2,
            }
        )

    @app.post("/api/budgets/recovery/migrate")
    def migrate_existing(body: RecoveryAction, access=actor_dependency):
        operator(access)
        if (
            os.getenv("BUDGET_RECOVERY_MIGRATION_ENABLED") != "1"
            or os.getenv("BUDGET_LEGACY_WRITERS_STOPPED") != "1"
        ):
            raise HTTPException(
                409,
                "An Azure operator must stop legacy writers and explicitly enable migration first",
            )
        reviewed(body, access, "migration_requested")
        return execute(lambda: migrate(ledger, snapshots, body.state_hash))

    @app.post("/api/budgets/recovery/restore")
    def stage_restore(body: RecoveryAction, access=actor_dependency):
        reviewed(body, access, "restore_requested")

        def stage():
            # Verify the selected archive before permanently fencing a healthy source.
            unseal(ledger.vault, "checkpoints", body.checkpoint)
            partition = str(uuid4())
            ledger.vault.record(
                "restore-targets/" + partition,
                {"target_partition": partition, "checkpoint": body.checkpoint, "at": stamp()},
            )
            # This permanently fences this source generation. It never changes routing.
            try:
                ledger.freeze(body.reason)
            except Exception:
                if not ledger.vault.read("fence"):
                    raise
            target = target_for(partition)
            return {
                **restore(ledger, target, snapshots, body.checkpoint, body.reason),
                "target_partition": partition,
            }

        return execute(stage)

    @app.post("/api/budgets/recovery/complete")
    def finish_restore(body: RecoveryAction, access=actor_dependency):
        reviewed(body, access, "restore_reviewed")
        if not body.target_partition:
            raise HTTPException(422, "A staged target partition is required")
        return execute(
            lambda: complete_restore(
                target_for(body.target_partition),
                ledger,
                snapshots,
                body.state_hash,
                body.policy_hash,
                body.reason,
            )
        )

    @app.post("/api/budgets/recovery/drill")
    def drill(body: RecoveryAction, access=actor_dependency):
        operator(access)
        if not drill_enabled:
            raise HTTPException(404, "Synthetic recovery drills are disabled")
        reviewed(body, access, "isolated_drill_requested")

        def run():
            result = run_drill(rows_for, ledger.vault.archive, snapshots, ledger.vault.protection)
            ledger.vault.record(
                "drill-reports/" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex,
                result,
            )
            return result

        return execute(run)

    return {"operators": operators}
