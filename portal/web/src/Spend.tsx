import { useState } from "react";
import type { State, Telemetry } from "./types";
import { Empty, fmt, Notice, Status } from "./components";
import { metricCount, standardRateEstimate, tokenCount } from "./estimates";
import { EstimateMethod, LedgerCharges, RatesDrawer } from "./CostAnalysis";

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
    const prices = usage.planning_prices ?? state.prices;
    const estimate = standardRateEstimate(rows, prices, state.models);
    const budget = state.settings.monthly_budget_usd;
    return (
        <section className="section" style={{ marginTop: 36 }}>
            <div className="sectionhead">
                <h2>Month-to-date usage</h2>
                <span className="muted">
                    UTC calendar month · ingestion delayed
                </span>
            </div>
            <Notice>
                Estimates use planning input/output rates and cache-read rates
                from the published budget price unless explicitly overridden.
                Streaming tokens are estimates. Claude cache writes,
                long-context premiums, tax and gateway costs are not reconciled
                here. Azure Cost Management remains the source for invoiced
                spend.
            </Notice>
            {usage.status === "error" ? (
                <Notice tone="error">
                    Spend reporting could not be loaded. Token estimates and
                    calendar-month charges are unavailable, not zero. Refresh
                    the portal to retry.
                </Notice>
            ) : usage.status !== "connected" ? (
                <Empty title="Usage is not connected">
                    Connect Application Insights to see token consumption and
                    planning estimates.
                </Empty>
            ) : (
                <>
                    {!estimate.complete && (
                        <Notice>
                            Rates or provider metadata are missing for{" "}
                            {[
                                ...new Set([
                                    ...estimate.missingModels,
                                    ...estimate.providerMissingModels,
                                ]),
                            ].join(", ")}
                            . The estimate is incomplete.
                        </Notice>
                    )}
                    {estimate.cacheRateMissingModels.length > 0 && (
                        <Notice>
                            Rate coverage · Cache rate missing:{" "}
                            {estimate.cacheRateMissingModels.join(", ")}. Cached
                            tokens use the input rate.
                        </Notice>
                    )}
                    <div className="stats">
                        <div className="stat">
                            <span>Estimated from token metrics</span>
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
                                        (n, r) =>
                                            n + metricCount(r.input_tokens),
                                        0,
                                    ),
                                )}
                            </strong>
                            <small>
                                OpenAI includes cache reads; Claude cache reads
                                are additional. Cached tokens use the cache-read
                                rate, or the input rate when missing.
                            </small>
                        </div>
                        <div className="stat">
                            <span>Output tokens</span>
                            <strong>
                                {fmt(
                                    rows.reduce(
                                        (n, r) =>
                                            n + metricCount(r.output_tokens),
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
            {usage.status !== "error" && (
                <LedgerCharges charges={usage.ledger_charges} state={state} />
            )}
            {usage.status !== "error" && usage.ledger_charges?.configured && (
                <p className="footnote">
                    Ledger reporting period: {usage.reporting_start} –{" "}
                    {usage.reporting_end} UTC. The token estimate above covers
                    this UTC month; ledger rows cover every touched calendar
                    month.
                </p>
            )}
            <div className="sheet tablewrap">
                <table>
                    <thead>
                        <tr>
                            <th>Model</th>
                            <th className="numeric">Input tokens</th>
                            <th className="numeric">Output tokens</th>
                            <th className="numeric">Input / 1M</th>
                            <th className="numeric">Cache read / 1M</th>
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
                            const price = prices.find(
                                (p) => p.model === model.name,
                            );
                            const cost = standardRateEstimate(
                                tokens,
                                prices,
                                state.models,
                            );
                            return (
                                <tr key={model.name}>
                                    <td>{model.labelOverride}</td>
                                    <td className="numeric">
                                        {usage.status === "connected"
                                            ? fmt(
                                                  tokens.reduce(
                                                      (n, r) =>
                                                          n +
                                                          metricCount(
                                                              r.input_tokens,
                                                          ),
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
                                                          n +
                                                          metricCount(
                                                              r.output_tokens,
                                                          ),
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
                                        {price?.cache_read_per_million != null
                                            ? dollars(
                                                  price.cache_read_per_million,
                                              )
                                            : "Input rate fallback"}
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
                                prices,
                                state.models,
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
                                                          tokenCount(
                                                              r,
                                                              prices,
                                                              state.models,
                                                          ),
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
                Usage recorded before this upgrade stays unattributed when
                principal dimensions are missing. New principal IDs map to the
                published policy’s groups; unknown principals stay unattributed.
                Changing rates recalculates this month’s estimate, not the
                ledger’s charges or Azure prices.
            </p>
            <EstimateMethod />
            {editing && (
                <RatesDrawer
                    state={state}
                    busy={busy}
                    initialModel={editing}
                    save={save}
                    close={() => setEditing(undefined)}
                />
            )}
        </section>
    );
}
