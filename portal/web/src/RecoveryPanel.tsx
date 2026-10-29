import { useCallback, useEffect, useState } from "react";
import { ArrowClockwise, ShieldCheck } from "@phosphor-icons/react";
import { api } from "./api";
import { Drawer, DrawerClose, Field, Notice } from "./components";

type Target = {
    target_partition: string;
    checkpoint: string;
    at: string;
    verified: boolean;
    ready: boolean;
    plan_hash: string;
    policy_hash: string;
};
type Report = {
    passed: boolean;
    checks: Record<string, boolean>;
    model_requests: number;
    spent_micro_usd: number;
    reserved_micro_usd: number;
};
type Status = {
    format: number;
    migration_required: boolean;
    migration_enabled: boolean;
    fenced: boolean;
    drill_enabled: boolean;
    checkpoint_stale: boolean;
    protection_verified: boolean;
    protection_error: string;
    checkpoints: { checkpoint: string; at: string; rows: number }[];
    targets: Target[];
    last_drill: Report | null;
};
type Action =
    | "checkpoint"
    | "drill"
    | "migration-plan"
    | "migrate"
    | "restore"
    | "complete";
const titles: Record<Action, string> = {
    checkpoint: "Create protected backup",
    drill: "Run isolated recovery drill",
    "migration-plan": "Review ledger migration",
    migrate: "Apply reviewed migration",
    restore: "Stage a paused restore",
    complete: "Approve staged target",
};

export function RecoveryPanel({
    organization,
    onChange,
}: {
    organization: string;
    onChange: () => Promise<unknown>;
}) {
    const [status, setStatus] = useState<Status>();
    const [loading, setLoading] = useState(true);
    const [working, setWorking] = useState(false);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [action, setAction] = useState<Action>();
    const [checkpoint, setCheckpoint] = useState("");
    const [migrationHash, setMigrationHash] = useState("");
    const [target, setTarget] = useState<Target>();
    const refresh = useCallback(async () => {
        setLoading(true);
        try {
            const value = await api<Status>("/api/budgets/recovery/status");
            setStatus(value);
            setError("");
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setLoading(false);
        }
    }, []);
    useEffect(() => {
        void refresh();
    }, [refresh]);
    const disabled = loading || working || !status;
    return (
        <section className="section" aria-labelledby="recovery-title">
            <div className="sectionhead budget-panel-heading">
                <h2 id="recovery-title">Budget recovery</h2>
                <button
                    className="button"
                    disabled={loading || working}
                    onClick={() => void refresh()}
                >
                    <ArrowClockwise
                        size={16}
                        aria-hidden="true"
                        className={
                            loading ? "budget-refresh-spinner" : undefined
                        }
                    />
                    Refresh recovery status
                </button>
            </div>
            <p>
                Protected checkpoints and dispatch receipts use a separate
                private storage account. Restores go into a new, paused ledger;
                switching the applications is a separate operator action.
            </p>
            {loading && <p role="status">Checking recovery protection…</p>}
            {error && <Notice tone="error">{error}</Notice>}
            {notice && <Notice>{notice}</Notice>}
            {status && (
                <>
                    <Notice>
                        {status.protection_verified ? (
                            <>
                                <ShieldCheck size={18} aria-hidden="true" />
                                Storage protection verified · locked retention
                                of at least 35 days
                            </>
                        ) : (
                            status.protection_error
                        )}
                    </Notice>
                    {status.migration_required && (
                        <Notice>
                            Legacy ledger detected. Budget admission stays
                            blocked until an operator migrates and verifies the
                            existing balances. Stop legacy writers before
                            applying migration.
                        </Notice>
                    )}
                    {status.fenced && (
                        <Notice>
                            This source generation is permanently fenced.
                            Restore verification and operator cutover are
                            required.
                        </Notice>
                    )}
                    <p className="footnote">
                        Checkpoints run every 30 minutes.{" "}
                        {status.checkpoints[0]
                            ? `Last completed: ${new Date(status.checkpoints[0].at).toLocaleString()}.`
                            : "No complete checkpoint recorded yet."}{" "}
                        {status.checkpoint_stale
                            ? "A current checkpoint needs attention."
                            : "Checkpoint is current."}{" "}
                        Receipts preserve possible model charges between
                        checkpoints.
                    </p>
                    <div
                        className="budget-policy-actions"
                        role="group"
                        aria-label="Recovery actions"
                    >
                        <div className="actions">
                            <button
                                className="button"
                                disabled={disabled || status.migration_required}
                                onClick={() => setAction("checkpoint")}
                            >
                                Create protected backup
                            </button>
                            {status.drill_enabled && (
                                <button
                                    className="button"
                                    disabled={disabled}
                                    onClick={() => setAction("drill")}
                                >
                                    Run isolated recovery drill
                                </button>
                            )}
                            {status.migration_required && (
                                <button
                                    className="button"
                                    disabled={disabled}
                                    onClick={() => setAction("migration-plan")}
                                >
                                    Review ledger migration
                                </button>
                            )}
                            {status.migration_required && migrationHash && (
                                <button
                                    className="button primary"
                                    disabled={
                                        disabled || !status.migration_enabled
                                    }
                                    onClick={() => setAction("migrate")}
                                >
                                    Apply reviewed migration
                                </button>
                            )}
                        </div>
                    </div>
                    {migrationHash && status.migration_required && (
                        <p className="footnote">
                            Reviewed migration state:{" "}
                            <code>{migrationHash}</code>
                        </p>
                    )}
                    {status.last_drill && (
                        <div className="notice">
                            <div>
                                <strong>
                                    {status.last_drill.passed
                                        ? "Isolated recovery drill passed"
                                        : "Recovery drill requires attention"}
                                </strong>
                                <p>
                                    Model requests:{" "}
                                    {status.last_drill.model_requests}.
                                    Synthetic records verify missing-counter
                                    refusal, journal replay, retained holds and
                                    restored budget enforcement.
                                </p>
                                <details>
                                    <summary>
                                        Technical verification record
                                    </summary>
                                    <pre className="codewell">
                                        {JSON.stringify(
                                            status.last_drill,
                                            null,
                                            2,
                                        )}
                                    </pre>
                                </details>
                            </div>
                        </div>
                    )}
                    {!!status.checkpoints.length && (
                        <div className="section">
                            <Field label="Checkpoint to restore">
                                <select
                                    value={
                                        checkpoint ||
                                        status.checkpoints[0].checkpoint
                                    }
                                    onChange={(e) =>
                                        setCheckpoint(e.target.value)
                                    }
                                >
                                    {status.checkpoints.map((item) => (
                                        <option
                                            key={item.checkpoint}
                                            value={item.checkpoint}
                                        >
                                            {new Date(item.at).toLocaleString()}{" "}
                                            · {item.rows} rows
                                        </option>
                                    ))}
                                </select>
                            </Field>
                            <div className="actions" style={{ marginTop: 16 }}>
                                <button
                                    className="button"
                                    disabled={disabled}
                                    onClick={() => setAction("restore")}
                                >
                                    Stage a paused restore
                                </button>
                            </div>
                        </div>
                    )}
                    {status.targets.map((item) => (
                        <div className="notice" key={item.target_partition}>
                            <div>
                                <strong>
                                    {item.ready
                                        ? "Target approved for operator cutover"
                                        : item.verified
                                          ? "Paused restore verified"
                                          : "Staging incomplete — admission remains blocked"}
                                </strong>
                                <p>
                                    Target partition:{" "}
                                    <code>{item.target_partition}</code>
                                </p>
                                {item.verified && !item.ready && (
                                    <button
                                        className="button"
                                        disabled={disabled}
                                        onClick={() => {
                                            setTarget(item);
                                            setAction("complete");
                                        }}
                                    >
                                        Approve staged target
                                    </button>
                                )}
                            </div>
                        </div>
                    ))}
                </>
            )}
            {action && (
                <Drawer
                    title={titles[action]}
                    description={
                        action === "restore"
                            ? "This permanently fences the current source generation. The selected archive is verified before fencing. The restored target remains paused until review; no application settings change automatically."
                            : action === "drill"
                              ? "Creates disposable synthetic ledger partitions in Azure. It deliberately deletes a synthetic counter, restores into a new partition and tests protected blobs. Your active ledger is not modified and no model is invoked."
                              : action === "migrate"
                                ? "Preserves all existing spend and held allowance. This requires an Azure operator to stop old writers and enable migration first."
                                : "Every operation is restricted to designated recovery operators and recorded in the administrative audit."
                    }
                    onClose={() => setAction(undefined)}
                    busy={working}
                >
                    <form
                        className="drawer-form"
                        onSubmit={async (e) => {
                            e.preventDefault();
                            const f = new FormData(e.currentTarget);
                            setWorking(true);
                            setError("");
                            try {
                                const result = await api<
                                    Record<string, unknown>
                                >(`/api/budgets/recovery/${action}`, {
                                    method: "POST",
                                    body: JSON.stringify({
                                        reason: f.get("reason"),
                                        confirmation: f.get("confirmation"),
                                        checkpoint:
                                            checkpoint ||
                                            status?.checkpoints[0]
                                                ?.checkpoint ||
                                            "",
                                        state_hash:
                                            action === "migrate"
                                                ? migrationHash
                                                : target?.plan_hash || "",
                                        policy_hash: target?.policy_hash || "",
                                        target_partition:
                                            action === "complete"
                                                ? target?.target_partition
                                                : null,
                                    }),
                                });
                                if (action === "migration-plan")
                                    setMigrationHash(String(result.state_hash));
                                setNotice(
                                    action === "migration-plan"
                                        ? "Migration state reviewed. Apply only after the old writers have stopped."
                                        : action === "drill"
                                          ? "Isolated recovery drill completed. Review the verification record below."
                                          : action === "restore"
                                            ? "Restore staged in a new paused partition. Review its verification result before approval."
                                            : action === "complete"
                                              ? "Target approved. An Azure operator must switch both applications to the target partition; routing has not changed."
                                              : "Recovery operation completed.",
                                );
                                setAction(undefined);
                                await refresh();
                                await onChange();
                            } catch (e) {
                                setError(
                                    e instanceof Error ? e.message : String(e),
                                );
                            } finally {
                                setWorking(false);
                            }
                        }}
                    >
                        <div className="drawer-scroll drawer-fields">
                            {action === "restore" && (
                                <Notice tone="error">
                                    Governed inference on this source stops
                                    after fencing. This action cannot be
                                    reversed by ordinary publication.
                                </Notice>
                            )}
                            {action === "complete" && (
                                <Notice>
                                    Review the policy and all recovered
                                    liabilities. Backup-account failover or
                                    changed protection blocks completion.
                                    Uncertain requests stay held.
                                </Notice>
                            )}
                            <Field
                                label={`Confirm organization: ${organization}`}
                            >
                                <input
                                    name="confirmation"
                                    autoComplete="off"
                                    required
                                />
                            </Field>
                            <Field label="Operator reason">
                                <textarea
                                    name="reason"
                                    minLength={5}
                                    maxLength={300}
                                    required
                                />
                            </Field>
                            {error && <Notice tone="error">{error}</Notice>}
                        </div>
                        <div className="dialogfooter">
                            <DrawerClose disabled={working} />
                            <button
                                className="button primary"
                                disabled={working}
                            >
                                {working ? (
                                    <>
                                        <span
                                            className="spinner"
                                            aria-hidden="true"
                                        />
                                        Working…
                                    </>
                                ) : (
                                    titles[action]
                                )}
                            </button>
                        </div>
                    </form>
                </Drawer>
            )}
        </section>
    );
}
