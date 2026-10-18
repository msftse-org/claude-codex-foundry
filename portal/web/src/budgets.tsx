import { useEffect, useState } from "react";
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
        limit_micro_usd: number;
        spent_micro_usd: number;
        reserved_micro_usd: number;
        remaining_micro_usd: number;
    }[];
};
const dollars = (v: number) =>
    new Intl.NumberFormat("en-US", {
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
    const [balance, setBalance] = useState<Balance>();
    const [loading, setLoading] = useState(true);
    const [message, setMessage] = useState("");
    const [editing, setEditing] = useState<
        "organization" | "price" | "publish" | null
    >(null);
    const [price, setPrice] = useState<BudgetPrice>();
    const [working, setWorking] = useState(false);
    const [refreshCount, setRefreshCount] = useState(0);
    const owner = state.viewer.role === "owner";
    const publisher = owner || state.viewer.role === "group_admin";
    const refresh = () => {
        setLoading(true);
        setBalance(undefined);
        setMessage("");
        return api<Balance>("/api/budgets/balance")
            .then((value) => {
                setBalance(value);
                setRefreshCount((count) => count + 1);
            })
            .catch((e) => setMessage(String(e.message || e)))
            .finally(() => setLoading(false));
    };
    useEffect(() => {
        void refresh();
    }, [state.revision]);
    return (
        <section className="section" style={{ marginBottom: 32 }}>
            <h2>Enforced spending limits</h2>
            <Notice>
                {loading
                    ? "Loading the active budget policy and balances…"
                    : !balance
                      ? "Budget status could not be verified. Refresh to retry."
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
            {message && <Notice tone="error">{message}</Notice>}
            {!!balance?.scopes.length && (
                <div className="sheet tablewrap">
                    <table>
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
                            {balance.scopes.map((s) => (
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
                        </tbody>
                    </table>
                </div>
            )}
            <div className="actions">
                <button className="button" onClick={() => void refresh()}>
                    Refresh balance
                </button>
                {owner && (
                    <button
                        className="button"
                        onClick={() => setEditing("organization")}
                    >
                        Organization limit
                    </button>
                )}
                {owner && (
                    <button
                        className="button"
                        onClick={() => {
                            setPrice(undefined);
                            setEditing("price");
                        }}
                    >
                        Add verified model rate
                    </button>
                )}
                {publisher && (
                    <button
                        className="button primary"
                        disabled={!balance?.configured || busy}
                        onClick={() => setEditing("publish")}
                    >
                        {owner
                            ? "Publish budget policy"
                            : "Publish team budget & access"}
                    </button>
                )}
            </div>
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
                <Reservations
                    owner={owner}
                    revision={`${state.revision}:${refreshCount}`}
                />
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
                                    spend_limit_usd: String(f.get("limit")),
                                    budget_enforcement:
                                        f.get("enabled") === "on",
                                })
                            )
                                setEditing(null);
                        }}
                    >
                        <div className="drawer-scroll drawer-fields">
                            <Field
                                label="Monthly USD limit"
                                help="Zero blocks every new request. Lowering below spent plus reserved is rejected at publication."
                            >
                                <input
                                    name="limit"
                                    type="number"
                                    min="0"
                                    step="0.000001"
                                    required
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
                                        min="0.000001"
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
