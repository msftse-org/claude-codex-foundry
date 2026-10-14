import { useEffect, useMemo, useState } from "react";
import {
    ArrowClockwise,
    ArrowDown,
    ArrowRight,
    Check,
    DownloadSimple,
    SlidersHorizontal,
    X,
} from "@phosphor-icons/react";
import { api, exportCsv } from "./api";
import {
    compact,
    date,
    Drawer,
    DrawerClose,
    Empty,
    Field,
    fmt,
    Notice,
    PageHead,
    Pager,
    Status,
} from "./components";
import type { State } from "./types";
import {
    breakdownRows,
    change,
    dailySeries,
    defaultFilters,
    filterSearch,
    isoDate,
    money,
    presetDates,
    readFilters,
    tokens,
    UNATTRIBUTED,
    validRange,
    zero,
    type CostFilters,
    type CostResult,
    type Grouping,
} from "./cost-analysis";
import "./cost-analysis.css";

type Option = { id: string; name: string; detail?: string };
const groupNames = { model: "Models", group: "Groups", member: "Developers" };
const dimensions = {
    model: "model_ids",
    group: "group_ids",
    member: "member_ids",
} as const;
const shortDate = (value: string) =>
    new Date(value + "T00:00:00Z").toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        timeZone: "UTC",
    });

function MultiSelect({
    label,
    options,
    selected,
    onChange,
}: {
    label: string;
    options: Option[];
    selected: string[];
    onChange: (values: string[]) => void;
}) {
    const [search, setSearch] = useState("");
    const matches = options.filter((item) =>
        `${item.name} ${item.detail || ""} ${item.id}`
            .toLowerCase()
            .includes(search.toLowerCase()),
    );
    return (
        <fieldset className="filter-select">
            <legend>
                {label}{" "}
                <span className="muted">
                    {selected.length ? `${selected.length} selected` : "All"}
                </span>
            </legend>
            <input
                aria-label={`Search ${label.toLowerCase()}`}
                placeholder={`Find ${label.toLowerCase()}…`}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
            />
            <div className="filter-options">
                {matches.slice(0, 100).map((item) => (
                    <label className="checkrow" key={item.id}>
                        <input
                            type="checkbox"
                            checked={selected.includes(item.id)}
                            onChange={(e) =>
                                onChange(
                                    e.target.checked
                                        ? [...selected, item.id]
                                        : selected.filter(
                                              (id) => id !== item.id,
                                          ),
                                )
                            }
                        />
                        <span>
                            {item.name}
                            {item.detail && <small>{item.detail}</small>}
                        </span>
                    </label>
                ))}
                {!matches.length && (
                    <p className="muted">
                        No matches. Try a name or object ID.
                    </p>
                )}
                {matches.length > 100 && (
                    <p className="footnote">
                        Showing 100 matches. Search to narrow the list.
                    </p>
                )}
            </div>
            {selected.length > 0 && (
                <button
                    className="button text"
                    type="button"
                    onClick={() => onChange([])}
                >
                    Clear selection
                </button>
            )}
        </fieldset>
    );
}

function FiltersDrawer({
    filters,
    options,
    close,
    apply,
}: {
    filters: CostFilters;
    options: Record<Grouping, Option[]>;
    close: () => void;
    apply: (f: CostFilters) => void;
}) {
    const [draft, setDraft] = useState(filters);
    const valid = validRange(draft.start, draft.end);
    return (
        <Drawer
            title="Explore your costs"
            description="Combine dimensions to isolate a team, model, or developer. Selections within a dimension are combined; dimensions narrow each other."
            onClose={close}
            protect={false}
        >
            <form
                className="drawer-form"
                onSubmit={(e) => {
                    e.preventDefault();
                    if (valid) apply(draft);
                }}
            >
                <div className="drawer-scroll">
                    <div className="section-label">Reporting period · UTC</div>
                    <div className="twocol">
                        <Field label="Start date">
                            <input
                                type="date"
                                required
                                value={draft.start}
                                max={draft.end}
                                onChange={(e) =>
                                    setDraft({
                                        ...draft,
                                        start: e.target.value,
                                    })
                                }
                            />
                        </Field>
                        <Field label="End date">
                            <input
                                type="date"
                                required
                                value={draft.end}
                                min={draft.start}
                                max={isoDate(new Date())}
                                onChange={(e) =>
                                    setDraft({ ...draft, end: e.target.value })
                                }
                            />
                        </Field>
                    </div>
                    {!valid && (
                        <Notice tone="error">
                            Choose a period of 1–93 days ending today or
                            earlier.
                        </Notice>
                    )}
                    <p className="footnote">
                        Compared with the immediately preceding period of the
                        same length. Today may still be incomplete.
                    </p>
                    {(["model", "group", "member"] as const).map((key) => (
                        <MultiSelect
                            key={key}
                            label={groupNames[key]}
                            options={options[key]}
                            selected={draft[dimensions[key]]}
                            onChange={(values) =>
                                setDraft({
                                    ...draft,
                                    [dimensions[key]]: values,
                                })
                            }
                        />
                    ))}
                </div>
                <div className="dialogfooter">
                    <button
                        type="button"
                        className="button text"
                        onClick={() => setDraft(defaultFilters())}
                    >
                        Reset all
                    </button>
                    <DrawerClose />
                    <button className="button primary" disabled={!valid}>
                        Apply filters
                    </button>
                </div>
            </form>
        </Drawer>
    );
}

export function RatesDrawer({
    state,
    busy,
    save,
    close,
    initialModel,
}: {
    initialModel?: string;
    state: State;
    busy: boolean;
    save: (value: unknown) => Promise<boolean>;
    close: () => void;
}) {
    const [model, setModel] = useState(
        initialModel ||
            state.models.find(
                (m) => !state.prices.some((p) => p.model === m.name),
            )?.name ||
            state.models[0]?.name ||
            "",
    );
    const price = state.prices.find((p) => p.model === model);
    return (
        <Drawer
            title="Model planning rates"
            description="Use the rates in your Azure agreement. Rates recalculate every selected period and are recorded in the audit log."
            onClose={close}
            busy={busy}
        >
            <form
                className="drawer-form"
                onSubmit={async (e) => {
                    e.preventDefault();
                    const values = new FormData(e.currentTarget);
                    if (
                        await save({
                            model,
                            input_per_million: Number(values.get("input")),
                            output_per_million: Number(values.get("output")),
                            source_url:
                                String(values.get("source") || "") || null,
                            verified_on:
                                String(values.get("verified") || "") || null,
                            note: String(values.get("note") || "") || null,
                        })
                    )
                        close();
                }}
            >
                <div className="drawer-scroll">
                    <Notice>
                        Standard input and output rates produce a planning
                        estimate. Cache discounts, cache writes, long-context
                        premiums, taxes, infrastructure, and negotiated
                        adjustments are not calculated.
                    </Notice>
                    <Field label="Model">
                        <select
                            value={model}
                            onChange={(e) => setModel(e.target.value)}
                        >
                            {state.models.map((m) => (
                                <option key={m.name} value={m.name}>
                                    {m.labelOverride}
                                </option>
                            ))}
                        </select>
                    </Field>
                    <div className="twocol" key={model}>
                        <Field label="Input USD / million tokens">
                            <input
                                name="input"
                                type="number"
                                required
                                min="0"
                                max="10000"
                                step="0.000001"
                                defaultValue={price?.input_per_million}
                            />
                        </Field>
                        <Field label="Output USD / million tokens">
                            <input
                                name="output"
                                type="number"
                                required
                                min="0"
                                max="10000"
                                step="0.000001"
                                defaultValue={price?.output_per_million}
                            />
                        </Field>
                    </div>
                    <div key={`${model}-provenance`}>
                        <Field
                            label="Source URL"
                            help="Optional: link to the public rate card or agreement reference."
                        >
                            <input
                                name="source"
                                type="url"
                                defaultValue={price?.source_url}
                            />
                        </Field>
                        <Field label="Verified on">
                            <input
                                name="verified"
                                type="date"
                                max={isoDate(new Date())}
                                defaultValue={price?.verified_on}
                            />
                        </Field>
                        <Field
                            label="Rate notes"
                            help="Record deployment geography, pricing tiers, or negotiated adjustments."
                        >
                            <textarea
                                name="note"
                                maxLength={500}
                                rows={3}
                                defaultValue={price?.note}
                            />
                        </Field>
                    </div>
                    <div className="rate-coverage">
                        <span>
                            {state.prices.length} of {state.models.length}{" "}
                            models have rates
                        </span>
                        {state.models.map((m) => (
                            <div key={m.name}>
                                <span>{m.labelOverride}</span>
                                <Status
                                    tone={
                                        state.prices.some(
                                            (p) => p.model === m.name,
                                        )
                                            ? "good"
                                            : "warn"
                                    }
                                >
                                    {state.prices.some(
                                        (p) => p.model === m.name,
                                    )
                                        ? "Configured"
                                        : "Needs rates"}
                                </Status>
                            </div>
                        ))}
                    </div>
                </div>
                <div className="dialogfooter">
                    <DrawerClose disabled={busy} />
                    <button
                        className="button primary"
                        disabled={busy || !model}
                    >
                        {busy ? "Saving…" : "Save planning rates"}
                    </button>
                </div>
            </form>
        </Drawer>
    );
}

function CostChart({
    data,
    metric,
    compare,
}: {
    data: CostResult;
    metric: "tokens" | "usd";
    compare: boolean;
}) {
    const series = useMemo(() => dailySeries(data), [data]);
    const [active, setActive] = useState<number>();
    const [table, setTable] = useState(false);
    const selected =
        series[Math.min(active ?? series.length - 1, series.length - 1)];
    const value = (row: (typeof series)[number]["current"]) =>
        metric === "tokens" ? tokens(row) : row.usd;
    const format = metric === "tokens" ? compact : money;
    const peak = Math.max(
        ...series.flatMap((d) => [
            value(d.current),
            compare ? value(d.previous) : 0,
        ]),
        0,
    );
    const max = peak > 0 ? peak : 1;
    const x = (i: number) =>
        series.length === 1 ? 500 : 16 + (i / (series.length - 1)) * 968;
    const y = (v: number) => 184 - (v / max) * 164;
    const path = (period: "current" | "previous") =>
        series
            .map((d, i) => `${i ? "L" : "M"}${x(i)},${y(value(d[period]))}`)
            .join(" ");
    return (
        <div className="cost-chart">
            <div className="chart-caption">
                <div className="chart-legend">
                    <span>
                        <i />
                        Selected period
                    </span>
                    {compare && (
                        <span>
                            <i className="previous" />
                            Previous period
                        </span>
                    )}
                </div>
                <button
                    className="button text"
                    onClick={() => setTable(!table)}
                >
                    {table ? "View chart" : "View daily table"}
                </button>
            </div>
            {table ? (
                <div className="daily-table tablewrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Date (UTC)</th>
                                <th>
                                    {metric === "tokens"
                                        ? "Tokens"
                                        : "Estimated USD"}
                                </th>
                                {compare && <th>Previous period</th>}
                            </tr>
                        </thead>
                        <tbody>
                            {series.map((d) => (
                                <tr key={d.date}>
                                    <td>{shortDate(d.date)}</td>
                                    <td>{format(value(d.current))}</td>
                                    {compare && (
                                        <td>
                                            {shortDate(d.previousDate)} ·{" "}
                                            {format(value(d.previous))}
                                        </td>
                                    )}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            ) : (
                <>
                    <div className="chart-axis">
                        <span>{format(max)}</span>
                        <span>0</span>
                    </div>
                    <div className="chart-plot">
                        <svg
                            viewBox="0 0 1000 200"
                            preserveAspectRatio="none"
                            aria-hidden="true"
                        >
                            {[20, 61, 102, 143, 184].map((yy) => (
                                <line
                                    key={yy}
                                    x1="0"
                                    x2="1000"
                                    y1={yy}
                                    y2={yy}
                                    className="chart-grid"
                                />
                            ))}
                            <path
                                d={`${path("current")} L${x(series.length - 1)},184 L${x(0)},184 Z`}
                                className="chart-area"
                            />
                            {compare && (
                                <path
                                    d={path("previous")}
                                    className="chart-line previous"
                                />
                            )}
                            <path d={path("current")} className="chart-line" />
                            {selected && (
                                <>
                                    <line
                                        x1={x(
                                            Math.min(
                                                active ?? series.length - 1,
                                                series.length - 1,
                                            ),
                                        )}
                                        x2={x(
                                            Math.min(
                                                active ?? series.length - 1,
                                                series.length - 1,
                                            ),
                                        )}
                                        y1="14"
                                        y2="184"
                                        className="chart-cursor"
                                    />
                                    <circle
                                        cx={x(
                                            Math.min(
                                                active ?? series.length - 1,
                                                series.length - 1,
                                            ),
                                        )}
                                        cy={y(value(selected.current))}
                                        r="4"
                                        className="chart-dot"
                                    />
                                </>
                            )}
                        </svg>
                        <div
                            className="chart-hitareas"
                            style={{
                                gridTemplateColumns: `repeat(${series.length}, 1fr)`,
                            }}
                        >
                            {series.map((d, i) => (
                                <button
                                    key={d.date}
                                    aria-label={`${d.date}: ${format(value(d.current))}${metric === "tokens" ? " tokens" : " estimated USD"}`}
                                    tabIndex={
                                        i ===
                                        Math.min(
                                            active ?? series.length - 1,
                                            series.length - 1,
                                        )
                                            ? 0
                                            : -1
                                    }
                                    onKeyDown={(event) => {
                                        if (
                                            ![
                                                "ArrowLeft",
                                                "ArrowRight",
                                                "Home",
                                                "End",
                                            ].includes(event.key)
                                        )
                                            return;
                                        event.preventDefault();
                                        const next =
                                            event.key === "Home"
                                                ? 0
                                                : event.key === "End"
                                                  ? series.length - 1
                                                  : Math.max(
                                                        0,
                                                        Math.min(
                                                            series.length - 1,
                                                            i +
                                                                (event.key ===
                                                                "ArrowRight"
                                                                    ? 1
                                                                    : -1),
                                                        ),
                                                    );
                                        setActive(next);
                                        (
                                            event.currentTarget.parentElement
                                                ?.children[next] as HTMLElement
                                        )?.focus();
                                    }}
                                    onFocus={() => setActive(i)}
                                    onMouseEnter={() => setActive(i)}
                                    onClick={() => setActive(i)}
                                />
                            ))}
                        </div>
                    </div>
                    <div className="chart-dates">
                        <span>{shortDate(data.start)}</span>
                        <span>{shortDate(data.end)}</span>
                    </div>
                </>
            )}
            <div className="chart-readout" aria-live="polite">
                {selected && (
                    <>
                        <strong>{shortDate(selected.date)}</strong>
                        <span>
                            {format(value(selected.current))}{" "}
                            {metric === "tokens" ? "tokens" : "estimated"}
                        </span>
                        {compare && (
                            <span className="muted">
                                {format(value(selected.previous))} on{" "}
                                {shortDate(selected.previousDate)}
                            </span>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}

export function CostAnalysis({
    state,
    busy,
    save,
}: {
    state: State;
    busy: boolean;
    save: (kind: string, value: unknown) => Promise<boolean>;
}) {
    const [filters, setFilters] = useState<CostFilters>(readFilters);
    const [drawer, setDrawer] = useState<"filters" | "rates">();
    const [data, setData] = useState<CostResult>();
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [refresh, setRefresh] = useState(0);
    const [compare, setCompare] = useState(true);
    const [metric, setMetric] = useState<"tokens" | "usd">(
        state.prices.length === state.models.length ? "usd" : "tokens",
    );
    const [search, setSearch] = useState("");
    const [sort, setSort] = useState("tokens");
    const [page, setPage] = useState(0);
    const finance = ["owner", "admin", "billing"].includes(state.viewer.role);
    const pricesKey = JSON.stringify(state.prices);
    useEffect(() => {
        const pop = () => setFilters(readFilters());
        window.addEventListener("popstate", pop);
        return () => window.removeEventListener("popstate", pop);
    }, []);
    useEffect(() => {
        const controller = new AbortController();
        setLoading(true);
        setError("");
        api<CostResult>("/api/cost-analysis", {
            method: "POST",
            body: JSON.stringify(filters),
            signal: controller.signal,
        })
            .then((next) => {
                if (!controller.signal.aborted) setData(next);
            })
            .catch((e) => {
                if (!controller.signal.aborted) setError((e as Error).message);
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });
        return () => controller.abort();
    }, [filters, refresh, pricesKey]);
    useEffect(() => setPage(0), [filters, search, sort]);
    const apply = (next: CostFilters) => {
        setFilters(next);
        history.pushState({}, "", filterSearch(next));
        setDrawer(undefined);
    };
    const options: Record<Grouping, Option[]> = {
        model: state.models.map((m) => ({ id: m.name, name: m.labelOverride })),
        group: state.groups.map((g) => ({ id: g.id, name: g.name })),
        member: state.members.map((m) => ({
            id: m.id,
            name: m.name,
            detail: m.email,
        })),
    };
    for (const key of ["model", "group", "member"] as const) {
        options[key].push({
            id: UNATTRIBUTED,
            name: "Unattributed",
            detail: "Metrics without this dimension",
        });
        for (const id of filters[dimensions[key]])
            if (!options[key].some((o) => o.id === id))
                options[key].push({
                    id,
                    name: id,
                    detail: "Historical identifier",
                });
    }
    const label = (key: string, dimension = filters.grouping) =>
        options[dimension].find((o) => o.id === key)?.name || key;
    const current = data?.totals.find((r) => r.period === "current") || zero();
    const previous =
        data?.totals.find((r) => r.period === "previous") || zero("previous");
    const complete = current.unpriced_tokens === 0;
    const total = tokens(current),
        priorTotal = tokens(previous);
    const coverage = total
        ? ((total - current.unpriced_tokens) / total) * 100
        : 100;
    const ready = !!data && data.status === "connected" && !error;
    const filterCount =
        filters.model_ids.length +
        filters.group_ids.length +
        filters.member_ids.length;
    const pairs = data ? breakdownRows(data) : [];
    const rows = pairs
        .filter((r) =>
            `${label(r.key)} ${r.key}`
                .toLowerCase()
                .includes(search.toLowerCase()),
        )
        .sort((a, b) =>
            sort === "name"
                ? label(a.key).localeCompare(label(b.key))
                : sort === "cost"
                  ? b.current.usd - a.current.usd
                  : tokens(b.current) - tokens(a.current),
        );
    const leading = [...pairs].sort(
        (a, b) => tokens(b.current) - tokens(a.current),
    )[0];
    const exportRows = () =>
        exportCsv(
            `claude-cost-analysis-${filters.start}-${filters.end}.csv`,
            rows.map((r) => ({
                dimension: data?.grouping,
                name: label(r.key),
                id: r.key,
                start_utc: data?.start,
                end_utc: data?.end,
                model_filter: filters.model_ids.join(";"),
                group_filter: filters.group_ids.join(";"),
                developer_filter: filters.member_ids.join(";"),
                input_tokens: r.current.input_tokens,
                output_tokens: r.current.output_tokens,
                cached_tokens: r.current.cached_tokens,
                estimated_usd: r.current.usd,
                estimate_complete: r.current.unpriced_tokens === 0,
                unpriced_tokens: r.current.unpriced_tokens,
                previous_tokens: tokens(r.previous),
                previous_estimated_usd: r.previous.usd,
                previous_estimate_complete: r.previous.unpriced_tokens === 0,
                breakdown_truncated: data?.truncated,
                basis: "Current standard input/output planning rates; not billed Azure charges",
            })),
        );
    return (
        <div className="cost-page">
            <PageHead
                title="Cost analysis"
                description="Explore model spending and developer consumption across your organization."
            >
                <button
                    className="button"
                    onClick={() => setRefresh((n) => n + 1)}
                    disabled={loading}
                >
                    <ArrowClockwise
                        size={16}
                        className={loading ? "rotating" : ""}
                    />
                    Refresh
                </button>
                <button
                    className="button"
                    onClick={exportRows}
                    disabled={!ready || loading || !rows.length}
                >
                    <DownloadSimple size={16} />
                    Export CSV
                </button>
            </PageHead>
            <div className="cost-filterbar">
                <div className="period-presets" aria-label="Reporting period">
                    {[
                        ["month", "This month"],
                        ["7", "7 days"],
                        ["30", "30 days"],
                        ["90", "90 days"],
                    ].map(([key, name]) => (
                        <button
                            key={key}
                            aria-pressed={
                                JSON.stringify(presetDates(key)) ===
                                JSON.stringify({
                                    start: filters.start,
                                    end: filters.end,
                                })
                            }
                            onClick={() =>
                                apply({ ...filters, ...presetDates(key) })
                            }
                        >
                            {name}
                        </button>
                    ))}
                </div>
                <button className="button" onClick={() => setDrawer("filters")}>
                    <SlidersHorizontal size={17} />
                    Filters{" "}
                    {filterCount > 0 && (
                        <span className="filter-count">{filterCount}</span>
                    )}
                </button>
                <button
                    className="button text date-range"
                    onClick={() => setDrawer("filters")}
                >
                    {shortDate(filters.start)} – {shortDate(filters.end)}{" "}
                    <small>UTC</small>
                </button>
            </div>
            {filterCount > 0 && (
                <div className="filter-chips">
                    {(["model", "group", "member"] as const).flatMap(
                        (dimension) =>
                            filters[dimensions[dimension]].map((id) => (
                                <button
                                    key={`${dimension}:${id}`}
                                    onClick={() =>
                                        apply({
                                            ...filters,
                                            [dimensions[dimension]]: filters[
                                                dimensions[dimension]
                                            ].filter((v) => v !== id),
                                        })
                                    }
                                    aria-label={`Remove ${groupNames[dimension]} filter ${label(id, dimension)}`}
                                >
                                    <small>{groupNames[dimension]}</small>
                                    {label(id, dimension)}
                                    <X size={12} />
                                </button>
                            )),
                    )}
                    <button
                        className="clear-filters"
                        onClick={() =>
                            apply({
                                ...filters,
                                model_ids: [],
                                group_ids: [],
                                member_ids: [],
                            })
                        }
                    >
                        Clear filters
                    </button>
                </div>
            )}
            {error && (
                <Notice tone="error">
                    {error}{" "}
                    <button
                        className="button text"
                        onClick={() => setRefresh((n) => n + 1)}
                    >
                        Try again
                    </button>
                </Notice>
            )}
            {!loading && data?.status === "not_configured" && (
                <Notice>
                    Connect Application Insights to explore cost and token
                    metrics.
                </Notice>
            )}
            {state.prices.some((p) => p.model === "claude-haiku-5-5") &&
                (!filters.model_ids.length ||
                    filters.model_ids.includes("claude-haiku-5-5")) && (
                    <Notice>
                        Haiku 5.5 uses base planning rates here. Prompts above
                        100,000 tokens have a higher public rate; aggregated
                        metrics cannot identify that tier per request.
                    </Notice>
                )}
            <div className="cost-workspace" aria-busy={loading}>
                {loading && (
                    <div className="cost-progress" role="status">
                        <span className="spinner" />
                        Updating analysis…
                    </div>
                )}
                <section className="cost-summary" aria-label="Period summary">
                    <div className="cost-primary">
                        <span className="eyebrow">
                            Estimated model spend · USD
                        </span>
                        <div className="cost-number num">
                            {!ready
                                ? "—"
                                : complete
                                  ? money(current.usd)
                                  : "Rates needed"}
                        </div>
                        <p>
                            {ready &&
                            complete &&
                            compare &&
                            previous.unpriced_tokens === 0 ? (
                                <>
                                    <span className="comparison">
                                        {change(current.usd, previous.usd)}
                                    </span>{" "}
                                    vs previous period
                                </>
                            ) : ready && !complete ? (
                                `${fmt(current.unpriced_tokens)} tokens need planning rates`
                            ) : (
                                "Based on your configured planning rates"
                            )}
                        </p>
                        <button
                            className="button text"
                            onClick={() => setDrawer("rates")}
                            disabled={!finance}
                        >
                            Manage rates <ArrowRight size={15} />
                        </button>
                    </div>
                    <div>
                        <span className="eyebrow">Total tokens</span>
                        <strong className="summary-value num">
                            {ready ? compact(total) : "—"}
                        </strong>
                        <p>
                            {ready && compare
                                ? `${change(total, priorTotal)} vs previous period`
                                : "Input + output consumption"}
                        </p>
                    </div>
                    <div>
                        <span className="eyebrow">Output share</span>
                        <strong className="summary-value num">
                            {ready && total
                                ? `${((current.output_tokens / total) * 100).toFixed(1)}%`
                                : "—"}
                        </strong>
                        <p>
                            {ready
                                ? `${compact(current.output_tokens)} output · ${compact(current.input_tokens)} input`
                                : "Waiting for token metrics"}
                        </p>
                    </div>
                    <div>
                        <span className="eyebrow">Rate coverage</span>
                        <strong className="summary-value num">
                            {ready && total ? `${coverage.toFixed(0)}%` : "—"}
                        </strong>
                        <p>
                            {ready && total
                                ? "of recorded tokens have rates"
                                : "No consumption in this period"}
                        </p>
                    </div>
                </section>
                <section className="cost-trend sheet">
                    <div className="cost-section-head">
                        <div>
                            <h2>Consumption over time</h2>
                            <p>
                                {data && ready
                                    ? `${shortDate(data.start)} – ${shortDate(data.end)}${compare ? ` compared with ${shortDate(data.previous_start)} – ${shortDate(data.previous_end)}` : ""}`
                                    : "Daily trends for your selected period"}
                            </p>
                        </div>
                        <div className="actions">
                            <div className="segmented">
                                {(["tokens", "usd"] as const).map((m) => (
                                    <button
                                        key={m}
                                        aria-pressed={
                                            (!complete ||
                                            (compare &&
                                                previous.unpriced_tokens > 0)
                                                ? "tokens"
                                                : metric) === m
                                        }
                                        disabled={
                                            m === "usd" &&
                                            (!complete ||
                                                (compare &&
                                                    previous.unpriced_tokens >
                                                        0))
                                        }
                                        onClick={() => setMetric(m)}
                                        title={
                                            m === "usd" && !complete
                                                ? "Set rates for all models with usage to chart costs"
                                                : undefined
                                        }
                                    >
                                        {m === "tokens"
                                            ? "Tokens"
                                            : "Estimated USD"}
                                    </button>
                                ))}
                            </div>
                            <label className="compare-toggle">
                                <input
                                    type="checkbox"
                                    checked={compare}
                                    onChange={(e) =>
                                        setCompare(e.target.checked)
                                    }
                                />
                                Compare
                            </label>
                        </div>
                    </div>
                    {ready && data ? (
                        <CostChart
                            data={data}
                            metric={
                                !complete ||
                                (compare && previous.unpriced_tokens > 0)
                                    ? "tokens"
                                    : metric
                            }
                            compare={compare}
                        />
                    ) : (
                        <div className="chart-placeholder">
                            {loading
                                ? "Loading token metrics…"
                                : "No analysis available"}
                        </div>
                    )}
                </section>
                <section className="cost-breakdown sheet">
                    <div className="cost-section-head">
                        <div>
                            <h2>Where usage comes from</h2>
                            <p>
                                Choose a dimension, then select a row to explore
                                it.
                            </p>
                        </div>
                        <div className="segmented">
                            {(["model", "group", "member"] as const).map(
                                (key) => (
                                    <button
                                        key={key}
                                        aria-pressed={filters.grouping === key}
                                        onClick={() =>
                                            apply({ ...filters, grouping: key })
                                        }
                                    >
                                        {groupNames[key]}
                                    </button>
                                ),
                            )}
                        </div>
                    </div>
                    <div className="breakdown-tools">
                        <input
                            aria-label="Search cost breakdown"
                            placeholder={`Search ${groupNames[filters.grouping].toLowerCase()}…`}
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                        />
                        <label>
                            Sort by{" "}
                            <select
                                aria-label="Sort cost breakdown"
                                value={sort}
                                onChange={(e) => setSort(e.target.value)}
                            >
                                <option value="tokens">Token volume</option>
                                <option value="cost" disabled={!complete}>
                                    Estimated cost
                                </option>
                                <option value="name">Name</option>
                            </select>
                        </label>
                        <span className="muted">{rows.length} results</span>
                    </div>
                    {ready && rows.length > 0 ? (
                        <div className="tablewrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>{groupNames[filters.grouping]}</th>
                                        <th>Input</th>
                                        <th>Output</th>
                                        <th>Token share</th>
                                        <th>Estimated USD</th>
                                        {compare && <th>Tokens vs prior</th>}
                                        <th>
                                            <span className="sr-only">
                                                Explore
                                            </span>
                                        </th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {rows
                                        .slice(page * 50, (page + 1) * 50)
                                        .map((row) => (
                                            <tr key={row.key}>
                                                <td>
                                                    <button
                                                        className="cost-drill"
                                                        disabled={loading}
                                                        onClick={() =>
                                                            apply({
                                                                ...filters,
                                                                [dimensions[
                                                                    filters
                                                                        .grouping
                                                                ]]: [row.key],
                                                            })
                                                        }
                                                    >
                                                        {label(row.key)}
                                                        <small>
                                                            {row.key ===
                                                            UNATTRIBUTED
                                                                ? "No recorded attribution"
                                                                : filters.grouping ===
                                                                    "member"
                                                                  ? options.member.find(
                                                                        (m) =>
                                                                            m.id ===
                                                                            row.key,
                                                                    )?.detail
                                                                  : undefined}
                                                        </small>
                                                    </button>
                                                </td>
                                                <td className="num">
                                                    {compact(
                                                        row.current
                                                            .input_tokens,
                                                    )}
                                                </td>
                                                <td className="num">
                                                    {compact(
                                                        row.current
                                                            .output_tokens,
                                                    )}
                                                </td>
                                                <td>
                                                    <div className="share-track">
                                                        <i
                                                            style={{
                                                                width: `${total ? (tokens(row.current) / total) * 100 : 0}%`,
                                                            }}
                                                        />
                                                    </div>
                                                    <small className="num">
                                                        {total
                                                            ? (
                                                                  (tokens(
                                                                      row.current,
                                                                  ) /
                                                                      total) *
                                                                  100
                                                              ).toFixed(1)
                                                            : "0"}
                                                        %
                                                    </small>
                                                </td>
                                                <td className="num">
                                                    {row.current
                                                        .unpriced_tokens > 0 ? (
                                                        <span className="muted">
                                                            Needs rates
                                                        </span>
                                                    ) : (
                                                        money(row.current.usd)
                                                    )}
                                                </td>
                                                {compare && (
                                                    <td className="num muted">
                                                        {change(
                                                            tokens(row.current),
                                                            tokens(
                                                                row.previous,
                                                            ),
                                                        )}
                                                    </td>
                                                )}
                                                <td>
                                                    <button
                                                        className="iconbutton"
                                                        aria-label={`Explore ${label(row.key)}`}
                                                        disabled={loading}
                                                        onClick={() =>
                                                            apply({
                                                                ...filters,
                                                                [dimensions[
                                                                    filters
                                                                        .grouping
                                                                ]]: [row.key],
                                                            })
                                                        }
                                                    >
                                                        <ArrowRight size={16} />
                                                    </button>
                                                </td>
                                            </tr>
                                        ))}
                                </tbody>
                            </table>
                        </div>
                    ) : (
                        <Empty
                            title={
                                loading
                                    ? "Loading breakdown…"
                                    : search
                                      ? "No matching rows"
                                      : "No recorded consumption"
                            }
                        >
                            {search
                                ? "Try a different search."
                                : "Adjust the date range or filters. Metrics appear after gateway traffic is ingested."}
                        </Empty>
                    )}
                    <Pager count={rows.length} page={page} onChange={setPage} />
                    {data?.truncated && (
                        <Notice>
                            Showing the top {fmt(data.breakdown_limit)} entries
                            per period. Summary totals include every matching
                            metric. Narrow your filters for a complete breakdown
                            and export.
                        </Notice>
                    )}
                </section>
                <div className="cost-insights">
                    <section>
                        <span className="eyebrow">Largest contributor</span>
                        <h3>
                            {ready && leading && tokens(leading.current)
                                ? label(leading.key)
                                : "Waiting for usage"}
                        </h3>
                        <p>
                            {ready && leading && total
                                ? `${((tokens(leading.current) / total) * 100).toFixed(1)}% of the selected token volume`
                                : "Your highest-volume model, group, or developer appears here."}
                        </p>
                    </section>
                    <section>
                        <span className="eyebrow">Attribution quality</span>
                        <h3>
                            {ready && total
                                ? `${(((total - current.unattributed_tokens) / total) * 100).toFixed(0)}% attributed`
                                : "No metrics yet"}
                        </h3>
                        <p>
                            Both group and developer recorded. Earlier metrics
                            may be unattributed.
                        </p>
                    </section>
                    <section>
                        <span className="eyebrow">Data freshness</span>
                        <h3>
                            {ready && data
                                ? date(data.generated_at)
                                : "Awaiting connection"}
                        </h3>
                        <p>
                            Application Insights ingestion is delayed. Query
                            results are cached for 60 seconds.
                        </p>
                    </section>
                </div>
            </div>
            <details className="cost-method">
                <summary>
                    How these estimates are calculated <ArrowDown size={14} />
                </summary>
                <p>
                    Input tokens × input rate + output tokens × output rate, per
                    million. Current planning rates apply across the selected
                    dates. Streaming tokens may be estimated. Cached tokens are
                    reported where available, but cache discounts, cache writes,
                    long-context premiums, taxes, infrastructure, and billing
                    adjustments are not calculated. These are consumption
                    estimates, not Azure billed charges.
                </p>
                <p>
                    All periods use UTC. Only retained metrics emitted by this
                    gateway API are included. An empty day means no recorded
                    metrics; it does not prove no usage occurred. No prompts or
                    responses are collected.
                </p>
            </details>
            {drawer === "filters" && (
                <FiltersDrawer
                    filters={filters}
                    options={options}
                    close={() => setDrawer(undefined)}
                    apply={apply}
                />
            )}
            {drawer === "rates" && finance && (
                <RatesDrawer
                    state={state}
                    busy={busy}
                    save={(value) => save("pricing", value)}
                    close={() => setDrawer(undefined)}
                />
            )}
            <div className="cost-footnote">
                <Check size={13} />
                Read-only analysis · gateway policies are unchanged
            </div>
        </div>
    );
}
