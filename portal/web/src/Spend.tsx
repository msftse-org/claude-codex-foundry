import { useState } from "react";
import type { State, Telemetry } from "./types";
import {
    Empty,
    Field,
    fmt,
    Drawer,
    DrawerClose,
    Notice,
    Status,
} from "./components";
import { standardRateEstimate } from "./estimates";

const dollars = (n: number) =>
    new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
    }).format(n);
export function Spend({
    state,
    usage,
    finance,
    busy,
    save,
}: {
    state: State;
    usage: Telemetry;
    finance: boolean;
    busy: boolean;
    save: (value: unknown) => Promise<boolean>;
}) {
    const [editing, setEditing] = useState<string>();
    const rows = usage.month_tokens || [];
    const estimate = standardRateEstimate(rows, state.prices);
    const budget = state.settings.monthly_budget_usd;
    const currentPrice = state.prices.find((p) => p.model === editing);
    return (
        <section className="section" style={{ marginTop: 36 }}>
            <div className="sectionhead">
                <h2>Month-to-date usage</h2>
                <span className="muted">
                    UTC calendar month · ingestion delayed
                </span>
            </div>
            <Notice>
                Estimates use the rates you enter below and APIM token metrics.
                Streaming tokens are estimated. Cache write/read charges,
                discounts, tax, and gateway costs are not reconciled here. Azure
                Cost Management remains the source for billed spend.
            </Notice>
            {usage.status !== "connected" ? (
                <Empty title="Usage is not connected">
                    Connect Application Insights to see token consumption and
                    planning estimates.
                </Empty>
            ) : (
                <>
                    {!estimate.complete && (
                        <Notice>
                            Rates are missing for{" "}
                            {estimate.missingModels.length} used models. The
                            estimate is incomplete.
                        </Notice>
                    )}
                    <div className="stats">
                        <div className="stat">
                            <span>Standard-rate estimate</span>
                            <strong>
                                {estimate.complete
                                    ? dollars(estimate.usd)
                                    : "Incomplete"}
                            </strong>
                            <small>
                                {estimate.complete
                                    ? budget
                                        ? `${dollars(budget)} monthly planning budget`
                                        : "No planning budget set"
                                    : `${dollars(estimate.usd)} from models with rates`}
                            </small>
                        </div>
                        <div className="stat">
                            <span>Input tokens</span>
                            <strong>
                                {fmt(
                                    rows.reduce(
                                        (n, r) => n + r.input_tokens,
                                        0,
                                    ),
                                )}
                            </strong>
                            <small>Includes reported cache hits</small>
                        </div>
                        <div className="stat">
                            <span>Output tokens</span>
                            <strong>
                                {fmt(
                                    rows.reduce(
                                        (n, r) => n + r.output_tokens,
                                        0,
                                    ),
                                )}
                            </strong>
                            <small>Reported by the gateway</small>
                        </div>
                        <div className="stat">
                            <span>Planning threshold</span>
                            <strong>
                                {budget && estimate.complete
                                    ? `${Math.round((estimate.usd / budget) * 100)}%`
                                    : "—"}
                            </strong>
                            <small>
                                {budget && estimate.usd >= budget
                                    ? "Planning budget reached"
                                    : "Dollar thresholds do not block inference"}
                            </small>
                        </div>
                    </div>
                    {budget > 0 && estimate.usd >= budget && (
                        <Notice tone="error">
                            Your standard-rate estimate has reached the monthly
                            planning budget. Review usage and token quotas.
                            Inference continues unless an enforced token quota
                            is reached.
                        </Notice>
                    )}
                </>
            )}
            <div className="sheet tablewrap">
                <table>
                    <thead>
                        <tr>
                            <th>Model</th>
                            <th className="numeric">Input tokens</th>
                            <th className="numeric">Output tokens</th>
                            <th className="numeric">Input / 1M</th>
                            <th className="numeric">Output / 1M</th>
                            <th className="numeric">Estimate</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>
                        {state.models.map((model) => {
                            const tokens = rows.filter(
                                (r) => r.model === model.name,
                            );
                            const price = state.prices.find(
                                (p) => p.model === model.name,
                            );
                            const cost = standardRateEstimate(
                                tokens,
                                state.prices,
                            );
                            return (
                                <tr key={model.name}>
                                    <td>{model.labelOverride}</td>
                                    <td className="numeric">
                                        {usage.status === "connected"
                                            ? fmt(
                                                  tokens.reduce(
                                                      (n, r) =>
                                                          n + r.input_tokens,
                                                      0,
                                                  ),
                                              )
                                            : "—"}
                                    </td>
                                    <td className="numeric">
                                        {usage.status === "connected"
                                            ? fmt(
                                                  tokens.reduce(
                                                      (n, r) =>
                                                          n + r.output_tokens,
                                                      0,
                                                  ),
                                              )
                                            : "—"}
                                    </td>
                                    <td className="numeric">
                                        {price
                                            ? dollars(price.input_per_million)
                                            : "Not set"}
                                    </td>
                                    <td className="numeric">
                                        {price
                                            ? dollars(price.output_per_million)
                                            : "Not set"}
                                    </td>
                                    <td className="numeric">
                                        {usage.status === "connected" &&
                                        cost.complete
                                            ? dollars(cost.usd)
                                            : "—"}
                                    </td>
                                    <td>
                                        {finance && (
                                            <button
                                                className="button text"
                                                onClick={() =>
                                                    setEditing(model.name)
                                                }
                                            >
                                                Set rates
                                            </button>
                                        )}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            <h3 style={{ marginTop: 28, marginBottom: 12 }}>
                Consumption by group
            </h3>
            <div className="sheet tablewrap">
                <table>
                    <thead>
                        <tr>
                            <th>Group</th>
                            <th className="numeric">Tokens</th>
                            <th className="numeric">Estimate</th>
                            <th>Planning budget</th>
                        </tr>
                    </thead>
                    <tbody>
                        {state.groups.map((group) => {
                            const tokens = rows.filter(
                                (r) => r.group_id === group.id,
                            );
                            const cost = standardRateEstimate(
                                tokens,
                                state.prices,
                            );
                            const exceeded =
                                group.budget_usd > 0 &&
                                cost.usd >= group.budget_usd;
                            return (
                                <tr key={group.id}>
                                    <td>{group.name}</td>
                                    <td className="numeric">
                                        {usage.status === "connected"
                                            ? fmt(
                                                  tokens.reduce(
                                                      (n, r) =>
                                                          n +
                                                          r.input_tokens +
                                                          r.output_tokens,
                                                      0,
                                                  ),
                                              )
                                            : "—"}
                                    </td>
                                    <td className="numeric">
                                        {usage.status === "connected" &&
                                        cost.complete
                                            ? dollars(cost.usd)
                                            : "—"}
                                    </td>
                                    <td>
                                        {group.budget_usd ? (
                                            <Status
                                                tone={
                                                    exceeded
                                                        ? "warn"
                                                        : "neutral"
                                                }
                                            >
                                                {dollars(group.budget_usd)}
                                                {exceeded ? " · Reached" : ""}
                                            </Status>
                                        ) : (
                                            "Not set"
                                        )}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            <p className="footnote">
                Group attribution begins after publishing a portal policy.
                Earlier metrics without a group are included in organization
                totals only. Changing rates recalculates this month’s estimate;
                it does not change Azure prices.
            </p>
            {editing && (
                <Drawer
                    busy={busy}
                    title="Set model planning rates"
                    description={
                        state.models.find((m) => m.name === editing)
                            ?.labelOverride || editing
                    }
                    onClose={() => setEditing(undefined)}
                >
                    <form
                        className="drawer-form"
                        onSubmit={async (e) => {
                            e.preventDefault();
                            const form = new FormData(e.currentTarget);
                            if (
                                await save({
                                    model: editing,
                                    input_per_million: Number(
                                        form.get("input"),
                                    ),
                                    output_per_million: Number(
                                        form.get("output"),
                                    ),
                                })
                            )
                                setEditing(undefined);
                        }}
                    >
                        <div className="drawer-scroll">
                            <Notice>
                                Use your contracted Azure rates. These values
                                are for estimates only and do not change the
                                price charged by Microsoft.
                            </Notice>
                            <Field label="Input USD per million tokens">
                                <input
                                    name="input"
                                    type="number"
                                    min="0"
                                    max="10000"
                                    step="0.000001"
                                    defaultValue={
                                        currentPrice?.input_per_million
                                    }
                                    required
                                />
                            </Field>
                            <Field label="Output USD per million tokens">
                                <input
                                    name="output"
                                    type="number"
                                    min="0"
                                    max="10000"
                                    step="0.000001"
                                    defaultValue={
                                        currentPrice?.output_per_million
                                    }
                                    required
                                />
                            </Field>
                        </div>
                        <div className="dialogfooter">
                            <DrawerClose disabled={busy} />
                            <button className="button primary" disabled={busy}>
                                {busy ? "Saving…" : "Save planning rates"}
                            </button>
                        </div>
                    </form>
                </Drawer>
            )}
        </section>
    );
}
