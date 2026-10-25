import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowClockwise } from "@phosphor-icons/react";
import { api } from "./api";
import { Drawer, DrawerClose, Field, Notice } from "./components";
import type { BudgetPrice, State } from "./types";

type Balance = {
    configured: boolean;
    active: boolean;
    month?: string;
    note?: string;
    published_hash?: string;
    scopes: {
        scope: string;
        limit_micro_usd: number | null;
        spent_micro_usd: number;
        reserved_micro_usd: number;
        remaining_micro_usd: number | null;
    }[];
};
const dollars = (v: number | null) =>
    v === null
        ? "Unlimited"
        : new Intl.NumberFormat("en-US", {
              style: "currency",
              currency: "USD",
              maximumFractionDigits: 6,
          }).format(v / 1_000_000);

export function BudgetPanel({
    state,
    busy,
    save,
}: {
    state: State;
    busy: boolean;
    save: (kind: string, value: unknown) => Promise<boolean>;
}) {
    const viewerKey = `${state.viewer.id}:${state.viewer.role}:${state.viewer.group_id}`;
    const [loaded, setLoaded] = useState<{
        viewer: string;
        balance: Balance;
    }>();
    const balance = loaded?.viewer === viewerKey ? loaded.balance : undefined;
    const [loading, setLoading] = useState(true);
    const [balanceError, setBalanceError] = useState("");
    const [updatedAt, setUpdatedAt] = useState<Date>();
    const requestId = useRef(0);
    const [message, setMessage] = useState("");
    const [editing, setEditing] = useState<
        "organization" | "price" | "publish" | null
    >(null);
    const [orgUnlimited, setOrgUnlimited] = useState(false);
    const [price, setPrice] = useState<BudgetPrice>();
    const [working, setWorking] = useState(false);
    const [refreshCount, setRefreshCount] = useState(0);
    const owner = state.viewer.role === "owner";
    const publisher = owner || state.viewer.role === "group_admin";
    const refresh = useCallback(async () => {
        const request = ++requestId.current;
        setLoading(true);
        setBalanceError("");
        try {
            const value = await api<Balance>("/api/budgets/balance");
            if (request === requestId.current) {
                setLoaded({ viewer: viewerKey, balance: value });
                setUpdatedAt(new Date());
                setRefreshCount((count) => count + 1);
            }
        } catch (error) {
            if (request === requestId.current)
                setBalanceError(
                    error instanceof Error ? error.message : String(error),
                );
        } finally {
            if (request === requestId.current) setLoading(false);
        }
    }, [viewerKey]);
    useEffect(() => {
        void refresh();
        return () => {
            ++requestId.current;
        };
    }, [refresh, state.revision]);
    const placeholderScopes =
        state.viewer.role === "developer"
            ? ["Your account"]
            : state.viewer.role === "group_admin"
              ? ["Your group", "Your account"]
              : ["Organization", "Your group", "Your account"];
    return (
        <section
            className="section budget-panel"
            aria-labelledby="budget-panel-title"
        >
            <div className="sectionhead budget-panel-heading">
                <h2 id="budget-panel-title">Enforced spending limits</h2>
                <button
                    className="button"
                    onClick={() => void refresh()}
                    disabled={loading}
                    aria-busy={loading}
                >
                    <ArrowClockwise
                        size={16}
                        aria-hidden="true"
                        className={
                            loading ? "budget-refresh-spinner" : undefined
                        }
                    />
                    Refresh balance
                </button>
            </div>
            <Notice>
                {!balance
                    ? loading
                        ? "Loading the active budget policy and balances…"
                        : "Budget status could not be verified. Refresh to retry."
                    : balance.active
                      ? `Budget policy active · ${balance.month} · UTC calendar month`
                      : balance?.configured
                        ? balance.note || "No active budget policy"
                        : "Budget gateway is not configured. These draft limits do not currently stop inference."}
            </Notice>
            <p>
                Every admitted request reserves allowance against the
                organization, group and developer at once. The owner’s requests
                follow the same rules. Uncertain requests keep their reservation
                for reconciliation. Values use reviewed conservative rates;
                Azure invoices remain separate.
            </p>
            {(owner || publisher) && (
                <div
                    className="budget-policy-actions"
                    role="group"
                    aria-label="Budget policy actions"
                >
                    {owner && (
                        <div className="actions">
                            <button
                                className="button"
                                onClick={() => {
                                    setOrgUnlimited(
                                        state.settings.spend_limit_unlimited ??
                                            false,
                                    );
                                    setEditing("organization");
                                }}
                            >
                                Organization limit
                            </button>
                            <button
                                className="button"
                                onClick={() => {
                                    setPrice(undefined);
                                    setEditing("price");
                                }}
                            >
                                Add verified model rate
                            </button>
                        </div>
                    )}
                    {publisher && (
                        <button
                            className="button primary"
                            disabled={
                                !balance?.configured ||
                                busy ||
                                loading ||
                                !!balanceError
                            }
                            onClick={() => setEditing("publish")}
                        >
                            {owner
                                ? "Publish budget policy"
                                : "Publish team budget & access"}
                        </button>
                    )}
                </div>
            )}
            {balanceError && (
                <Notice tone="error">
                    {balance
                        ? "Refresh failed. Showing the last loaded balances; refresh again to verify current allowance. "
                        : "Balances could not be loaded. "}
                    {balanceError}
                </Notice>
            )}
            {message && <Notice tone="error">{message}</Notice>}
            <div
                className="sheet tablewrap"
                role="region"
                aria-label="Monthly spending balances"
                aria-busy={loading}
                tabIndex={0}
            >
                <table
                    className="budget-balance-table"
                    aria-label="Monthly spending limits"
                >
                    <thead>
                        <tr>
                            <th>Scope</th>
                            <th>Limit</th>
                            <th>Spent</th>
                            <th>Reserved</th>
                            <th>Available</th>
                        </tr>
                    </thead>
                    <tbody>
                        {balance?.scopes.map((s) => (
                            <tr key={s.scope}>
                                <td>
                                    {s.scope === "org"
                                        ? "Organization"
                                        : s.scope.startsWith("group-")
                                          ? "Your group"
                                          : "Your account"}
                                </td>
                                <td>{dollars(s.limit_micro_usd)}</td>
                                <td>{dollars(s.spent_micro_usd)}</td>
                                <td>{dollars(s.reserved_micro_usd)}</td>
                                <td>{dollars(s.remaining_micro_usd)}</td>
                            </tr>
                        ))}
                        {!balance &&
                            loading &&
                            placeholderScopes.map((scope) => (
                                <tr key={scope}>
                                    <td>{scope}</td>
                                    {[0, 1, 2, 3].map((column) => (
                                        <td key={column}>
                                            <span
                                                className="budget-placeholder"
                                                aria-hidden="true"
                                            />
                                        </td>
                                    ))}
                                </tr>
                            ))}
                        {!loading && !balance?.scopes.length && (
                            <tr>
                                <td colSpan={5} className="budget-empty-state">
                                    {balance
                                        ? "No active budget balances to display."
                                        : "Refresh balance to try loading your spending limits again."}
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
            <p
                className="budget-refresh-status"
                role="status"
                aria-live="polite"
                aria-atomic="true"
            >
                {loading
                    ? balance
                        ? "Refreshing balances…"
                        : "Loading balances…"
                    : balanceError
                      ? balance
                          ? "Balances may be out of date."
                          : "Balance refresh failed."
                      : balance && updatedAt
                        ? `Last refreshed at ${updatedAt.toLocaleTimeString()}`
                        : "No balance data available."}
            </p>
            <p className="footnote">
                A blank personal limit inherits its group allowance; zero blocks
                new requests. Group admins cannot raise the owner-assigned
                ceiling. Saving a draft does not publish it. Budget publication
                applies only to the dedicated budget routes.
            </p>
            {owner && !!state.budget_prices?.length && (
                <div className="sheet tablewrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Model</th>
                                <th>Input ceiling / 1M</th>
                                <th>Output ceiling / 1M</th>
                                <th>Valid until</th>
                                <th></th>
                            </tr>
                        </thead>
                        <tbody>
                            {state.budget_prices.map((p) => (
                                <tr key={p.model}>
                                    <td>{p.model}</td>
                                    <td>${p.input_ceiling_per_million}</td>
                                    <td>${p.output_ceiling_per_million}</td>
                                    <td>
                                        {new Date(
                                            p.valid_until,
                                        ).toLocaleDateString()}
                                    </td>
                                    <td>
                                        <button
                                            className="button text"
                                            onClick={() => {
                                                setPrice(p);
                                                setEditing("price");
                                            }}
                                        >
                                            Review rate
                                        </button>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
            {balance?.configured && (
                <>
                    {owner && (
                        <AccountingIncident
                            revision={`${state.revision}:${refreshCount}`}
                        />
                    )}
                    <Reservations
                        owner={owner}
                        revision={`${state.revision}:${refreshCount}`}
                    />
                </>
            )}
            {editing === "organization" && (
                <Drawer
                    title="Organization spending limit"
                    description="Only the owner can change this ceiling. Review rates and every group ceiling before publishing."
                    onClose={() => setEditing(null)}
                    busy={busy}
                >
                    <form
                        className="drawer-form"
                        onSubmit={async (e) => {
                            e.preventDefault();
                            const f = new FormData(e.currentTarget);
                            if (
                                await save("settings", {
                                    ...state.settings,
                                    spend_limit_usd: orgUnlimited
                                        ? null
                                        : String(f.get("limit")),
                                    spend_limit_unlimited: orgUnlimited,
                                    budget_enforcement:
                                        f.get("enabled") === "on",
                                })
                            )
                                setEditing(null);
                        }}
                    >
                        <div className="drawer-scroll drawer-fields">
                            <label className="checkrow">
                                <input
                                    type="checkbox"
                                    checked={orgUnlimited}
                                    onChange={(e) =>
                                        setOrgUnlimited(e.target.checked)
                                    }
                                />
                                No organization spending limit
                            </label>
                            <Field
                                label="Monthly USD limit"
                                help="Zero blocks every new request. Lowering below spent plus reserved is rejected at publication."
                            >
                                <input
                                    name="limit"
                                    type="number"
                                    min="0"
                                    step="0.000001"
                                    required={!orgUnlimited}
                                    disabled={orgUnlimited}
                                    defaultValue={
                                        state.settings.spend_limit_usd ?? ""
                                    }
                                />
                            </Field>
                            <label className="checkrow">
                                <input
                                    name="enabled"
                                    type="checkbox"
                                    defaultChecked={
                                        state.settings.budget_enforcement
                                    }
                                />
                                Enable hard budgets in the published budget
                                policy
                            </label>
                        </div>
                        <div className="dialogfooter">
                            <DrawerClose disabled={busy} />
                            <button className="button primary" disabled={busy}>
                                Save draft
                            </button>
                        </div>
                    </form>
                </Drawer>
            )}
            {editing === "publish" && (
                <Drawer
                    title={
                        owner
                            ? "Publish spending policy"
                            : "Publish your team’s policy"
                    }
                    description={
                        owner
                            ? "Activate the reviewed limits, model access and rates for new budget gateway requests."
                            : "Publish your team’s developers, allowed models and operating limits. Organization limits, owner-assigned ceilings and other teams remain under owner control."
                    }
                    onClose={() => setEditing(null)}
                    busy={working}
                >
                    <form
                        className="drawer-form"
                        onSubmit={async (e) => {
                            e.preventDefault();
                            setWorking(true);
                            setMessage("");
                            try {
                                await api("/api/budgets/publish", {
                                    method: "POST",
                                    body: JSON.stringify({
                                        revision: state.revision,
                                        organization: String(
                                            new FormData(e.currentTarget).get(
                                                "organization",
                                            ),
                                        ),
                                        published_hash: balance?.published_hash,
                                    }),
                                });
                                window.location.reload();
                            } catch (error) {
                                setMessage(
                                    error instanceof Error
                                        ? error.message
                                        : String(error),
                                );
                            } finally {
                                setWorking(false);
                            }
                        }}
                    >
                        <div className="drawer-scroll drawer-fields">
                            <Notice>
                                Existing non-budget endpoints and direct Foundry
                                permissions bypass these controls. Only
                                distribute validated budget gateway profiles to
                                governed users.
                            </Notice>
                            <Field
                                label={`Confirm organization: ${state.settings.name}`}
                            >
                                <input
                                    name="organization"
                                    required
                                    autoComplete="off"
                                />
                            </Field>
                            {message && <Notice tone="error">{message}</Notice>}
                        </div>
                        <div className="dialogfooter">
                            <DrawerClose disabled={working} />
                            <button
                                className="button primary"
                                disabled={working}
                            >
                                Publish reviewed policy
                            </button>
                        </div>
                    </form>
                </Drawer>
            )}
            {editing === "price" && (
                <Drawer
                    title="Verified enforcement rate"
                    description="These are maximum admitted prices and pinned model capacities, not estimated invoice rates. Include cache writes, long context and every supported billing category. Unverified models fail closed."
                    onClose={() => setEditing(null)}
                    busy={busy}
                >
                    <form
                        className="drawer-form"
                        onSubmit={async (e) => {
                            e.preventDefault();
                            const f = new FormData(e.currentTarget);
                            const value = Object.fromEntries(f);
                            for (const k of [
                                "max_input_tokens",
                                "max_output_tokens",
                                "default_output_tokens",
                            ])
                                (value as Record<string, unknown>)[k] = Number(
                                    value[k],
                                );
                            for (const k of ["verified_at", "valid_until"])
                                value[k] = new Date(
                                    String(value[k]) + "Z",
                                ).toISOString();
                            if (await save("budget_pricing", value))
                                setEditing(null);
                        }}
                    >
                        <div className="drawer-scroll drawer-fields">
                            <Field label="Model">
                                <select
                                    name="model"
                                    defaultValue={price?.model}
                                >
                                    {state.models.map((m) => (
                                        <option key={m.name}>{m.name}</option>
                                    ))}
                                </select>
                            </Field>
                            <Field label="Provider">
                                <select
                                    name="provider"
                                    defaultValue={
                                        price?.provider || "anthropic"
                                    }
                                >
                                    <option value="anthropic">
                                        Anthropic Messages
                                    </option>
                                    <option value="openai">
                                        OpenAI Responses
                                    </option>
                                </select>
                            </Field>
                            <Field label="Pinned deployment version">
                                <input
                                    name="deployment_version"
                                    defaultValue={price?.deployment_version}
                                    required
                                />
                            </Field>
                            {(
                                [
                                    [
                                        "max_input_tokens",
                                        "Verified maximum input tokens",
                                    ],
                                    [
                                        "max_output_tokens",
                                        "Verified maximum output tokens",
                                    ],
                                    [
                                        "default_output_tokens",
                                        "Default output limit",
                                    ],
                                    [
                                        "input_ceiling_per_million",
                                        "Input ceiling (USD / million tokens)",
                                    ],
                                    [
                                        "output_ceiling_per_million",
                                        "Output ceiling (USD / million tokens)",
                                    ],
                                ] as const
                            ).map(([key, label]) => (
                                <Field key={key} label={label}>
                                    <input
                                        name={key}
                                        type="number"
                                        min={
                                            key.endsWith("tokens")
                                                ? "1"
                                                : "0.000001"
                                        }
                                        step={
                                            key.endsWith("tokens")
                                                ? "1"
                                                : "0.000001"
                                        }
                                        required
                                        defaultValue={
                                            price?.[key] ??
                                            (key === "default_output_tokens"
                                                ? 4096
                                                : "")
                                        }
                                    />
                                </Field>
                            ))}
                            <Field label="Official price source">
                                <input
                                    name="source_url"
                                    type="url"
                                    required
                                    defaultValue={price?.source_url}
                                />
                            </Field>
                            <Field label="Verified at (UTC)">
                                <input
                                    name="verified_at"
                                    type="datetime-local"
                                    required
                                    defaultValue={price?.verified_at.slice(
                                        0,
                                        16,
                                    )}
                                />
                            </Field>
                            <Field label="Valid until (UTC, within 31 days)">
                                <input
                                    name="valid_until"
                                    type="datetime-local"
                                    required
                                    defaultValue={price?.valid_until.slice(
                                        0,
                                        16,
                                    )}
                                />
                            </Field>
                            <Field label="Verification notes">
                                <textarea
                                    name="note"
                                    required
                                    minLength={10}
                                    maxLength={1000}
                                    defaultValue={price?.note}
                                />
                            </Field>
                        </div>
                        <div className="dialogfooter">
                            <DrawerClose disabled={busy} />
                            <button className="button primary" disabled={busy}>
                                Save reviewed rate
                            </button>
                        </div>
                    </form>
                </Drawer>
            )}
        </section>
    );
}

function AccountingIncident({ revision }: { revision: string }) {
    type Incident = {
        request_id: string;
        observed_cost_micro_usd: number;
        reservation: {
            model: string;
            reserved: number;
        };
    };
    const [incident, setIncident] = useState<Incident | null>();
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        api<{ incident: Incident | null }>("/api/budgets/incident")
            .then((result) => setIncident(result.incident))
            .catch((cause) =>
                setError(cause instanceof Error ? cause.message : String(cause)),
            );
    }, [revision]);
    if (incident === undefined) return null;
    if (!incident)
        return error ? <Notice tone="error">{error}</Notice> : null;
    return (
        <div style={{ marginTop: 24 }}>
            <Notice tone="error">
                Budget enforcement is paused because request{" "}
                <code>{incident.request_id}</code> exceeded its verified
                reservation. Review and reconcile the recorded charge to resume
                inference.
            </Notice>
            <Drawer
                title="Reconcile accounting incident"
                description="This charges the recorded incident amount, releases the matching reservation, and resumes budget enforcement. The request ID is guarded so a changed incident cannot be reconciled accidentally."
                onClose={() => undefined}
                busy={busy}
            >
                <form
                    className="drawer-form"
                    onSubmit={async (event) => {
                        event.preventDefault();
                        const reason = String(
                            new FormData(event.currentTarget).get("reason"),
                        );
                        setBusy(true);
                        setError("");
                        try {
                            await api("/api/budgets/resolve-incident", {
                                method: "POST",
                                body: JSON.stringify({
                                    request_id: incident.request_id,
                                    reason,
                                }),
                            });
                            window.location.reload();
                        } catch (cause) {
                            setError(
                                cause instanceof Error
                                    ? cause.message
                                    : String(cause),
                            );
                        } finally {
                            setBusy(false);
                        }
                    }}
                >
                    <div className="drawer-scroll drawer-fields">
                        <p>
                            Model: <strong>{incident.reservation.model}</strong>
                        </p>
                        <p>
                            Reserved:{" "}
                            <strong>
                                {dollars(incident.reservation.reserved)}
                            </strong>
                        </p>
                        <p>
                            Recorded charge:{" "}
                            <strong>
                                {dollars(
                                    incident.observed_cost_micro_usd,
                                )}
                            </strong>
                        </p>
                        <Field label="Reason for reconciliation">
                            <textarea
                                name="reason"
                                required
                                minLength={5}
                                maxLength={300}
                            />
                        </Field>
                        {error && <Notice tone="error">{error}</Notice>}
                    </div>
                    <div className="dialogfooter">
                        <button
                            className="button primary"
                            disabled={busy}
                        >
                            Reconcile and resume inference
                        </button>
                    </div>
                </form>
            </Drawer>
        </div>
    );
}

function Reservations({
    owner,
    revision,
}: {
    owner: boolean;
    revision: string;
}) {
    type Hold = {
        request_id: string;
        model: string;
        at: string;
        reserved: number;
    };
    const [holds, setHolds] = useState<Hold[]>([]);
    const [more, setMore] = useState(false);
    const [selected, setSelected] = useState<Hold>();
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        api<{ items: Hold[]; has_more: boolean }>("/api/budgets/reservations")
            .then((r) => {
                setHolds(r.items);
                setMore(r.has_more);
            })
            .catch((e) => setError(e.message));
    }, [revision]);
    return (
        <div style={{ marginTop: 24 }}>
            <h3>Outstanding reservations</h3>
            {error && <Notice tone="error">{error}</Notice>}
            {holds.length ? (
                <div className="sheet tablewrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Request</th>
                                <th>Model</th>
                                <th>Started</th>
                                <th>Held</th>
                                <th></th>
                            </tr>
                        </thead>
                        <tbody>
                            {holds.map((h) => (
                                <tr key={h.request_id}>
                                    <td>
                                        <code>{h.request_id}</code>
                                    </td>
                                    <td>{h.model}</td>
                                    <td>{new Date(h.at).toLocaleString()}</td>
                                    <td>{dollars(h.reserved)}</td>
                                    <td>
                                        {owner && (
                                            <button
                                                className="button text"
                                                onClick={() => setSelected(h)}
                                            >
                                                Close at maximum
                                            </button>
                                        )}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            ) : !error ? (
                <p className="footnote">
                    No outstanding reservations in your scope.
                </p>
            ) : null}
            {more && (
                <Notice>
                    Showing 50 reservations. Additional records require operator
                    review.
                </Notice>
            )}
            {selected && (
                <Drawer
                    title="Close at the reserved maximum"
                    description="This records the full held amount as spent. It does not issue a refund or change Azure billing. Normally, a complete model response settles automatically."
                    onClose={() => setSelected(undefined)}
                    busy={busy}
                >
                    <form
                        className="drawer-form"
                        onSubmit={async (e) => {
                            e.preventDefault();
                            const reason = String(
                                new FormData(e.currentTarget).get("reason"),
                            );
                            setBusy(true);
                            setError("");
                            try {
                                await api("/api/budgets/close-reservation", {
                                    method: "POST",
                                    body: JSON.stringify({
                                        request_id: selected.request_id,
                                        reason,
                                    }),
                                });
                                window.location.reload();
                            } catch (e) {
                                setError(
                                    e instanceof Error ? e.message : String(e),
                                );
                            } finally {
                                setBusy(false);
                            }
                        }}
                    >
                        <div className="drawer-scroll drawer-fields">
                            <p>
                                <code>{selected.request_id}</code>
                            </p>
                            <p>
                                Charge to allowance:{" "}
                                <strong>{dollars(selected.reserved)}</strong>
                            </p>
                            <Field label="Reason for conservative closure">
                                <textarea
                                    name="reason"
                                    required
                                    minLength={5}
                                    maxLength={300}
                                />
                            </Field>
                            {error && <Notice tone="error">{error}</Notice>}
                        </div>
                        <div className="dialogfooter">
                            <DrawerClose disabled={busy} />
                            <button className="button primary" disabled={busy}>
                                Charge full reservation
                            </button>
                        </div>
                    </form>
                </Drawer>
            )}
        </div>
    );
}
