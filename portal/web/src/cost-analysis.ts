export const UNATTRIBUTED = "__unattributed__";
export type Grouping = "model" | "group" | "member";
export type CostFilters = {
    start: string;
    end: string;
    model_ids: string[];
    group_ids: string[];
    member_ids: string[];
    grouping: Grouping;
};
export type CostRow = {
    key?: string;
    period: "current" | "previous";
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    usd: number;
    unpriced_tokens: number;
    unattributed_tokens: number;
    missing_models: string[];
};
export type CostResult = {
    grouping: Grouping;
    status: string;
    start: string;
    end: string;
    previous_start: string;
    previous_end: string;
    generated_at: string;
    totals: CostRow[];
    daily: CostRow[];
    breakdown: CostRow[];
    truncated: boolean;
    breakdown_limit: number;
};
export const isoDate = (d: Date) => d.toISOString().slice(0, 10);
export const shiftDate = (d: string, days: number) =>
    isoDate(new Date(new Date(d + "T00:00:00Z").getTime() + days * 86400000));
export function presetDates(preset: string, today = isoDate(new Date())) {
    return {
        start:
            preset === "month"
                ? today.slice(0, 8) + "01"
                : shiftDate(today, 1 - Number(preset)),
        end: today,
    };
}
export const defaultFilters = (): CostFilters => ({
    ...presetDates("month"),
    model_ids: [],
    group_ids: [],
    member_ids: [],
    grouping: "model",
});
export function validRange(start: string, end: string): boolean {
    const valid = (s: string) =>
        /^\d{4}-\d{2}-\d{2}$/.test(s) &&
        !Number.isNaN(Date.parse(s)) &&
        isoDate(new Date(s)) === s;
    const days = (Date.parse(end) - Date.parse(start)) / 86400000;
    return (
        valid(start) &&
        valid(end) &&
        start >= "2000-01-01" &&
        end <= isoDate(new Date()) &&
        days >= 0 &&
        days < 93
    );
}
export function readFilters(search = location.search): CostFilters {
    const defaults = defaultFilters(),
        params = new URLSearchParams(search);
    const start = params.get("c_start") || defaults.start,
        end = params.get("c_end") || defaults.end;
    const list = (key: string) =>
        params
            .getAll(key)
            .filter((v) => v.length > 0 && v.length <= 160)
            .slice(0, 2500);
    const grouping = params.get("c_group");
    return {
        ...(validRange(start, end) ? { start, end } : defaults),
        model_ids: list("c_model"),
        group_ids: list("c_team"),
        member_ids: list("c_member"),
        grouping:
            grouping === "group" || grouping === "member" ? grouping : "model",
    };
}
export function filterSearch(filters: CostFilters, search = location.search) {
    const params = new URLSearchParams(search);
    for (const key of [...params.keys()])
        if (key.startsWith("c_")) params.delete(key);
    params.set("page", "costs");
    params.set("c_start", filters.start);
    params.set("c_end", filters.end);
    params.set("c_group", filters.grouping);
    for (const [key, values] of [
        ["c_model", filters.model_ids],
        ["c_team", filters.group_ids],
        ["c_member", filters.member_ids],
    ] as const)
        for (const value of values) params.append(key, value);
    return "?" + params.toString();
}
export const zero = (period: CostRow["period"] = "current"): CostRow => ({
    period,
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
    usd: 0,
    unpriced_tokens: 0,
    unattributed_tokens: 0,
    missing_models: [],
});
export const tokens = (row: CostRow) => row.input_tokens + row.output_tokens;
export const money = (value: number) =>
    new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: value > 0 && value < 0.01 ? 6 : 2,
    }).format(value);
export function change(current: number, previous: number): string {
    if (!previous) return current ? "New usage" : "No change";
    const delta = ((current - previous) / previous) * 100;
    return `${delta > 0 ? "+" : ""}${delta.toFixed(1)}%`;
}
export function dailySeries(data: CostResult) {
    const lookup = new Map(data.daily.map((r) => [`${r.period}:${r.key}`, r]));
    const length =
        Math.round((Date.parse(data.end) - Date.parse(data.start)) / 86400000) +
        1;
    return Array.from({ length }, (_, i) => ({
        date: shiftDate(data.start, i),
        previousDate: shiftDate(data.previous_start, i),
        current: lookup.get(`current:${shiftDate(data.start, i)}`) || zero(),
        previous:
            lookup.get(`previous:${shiftDate(data.previous_start, i)}`) ||
            zero("previous"),
    }));
}
export function breakdownRows(data: CostResult) {
    const pairs = new Map<
        string,
        { key: string; current: CostRow; previous: CostRow }
    >();
    for (const row of data.breakdown) {
        const key = row.key || UNATTRIBUTED;
        const pair = pairs.get(key) || {
            key,
            current: zero(),
            previous: zero("previous"),
        };
        pair[row.period] = row;
        pairs.set(key, pair);
    }
    return [...pairs.values()];
}
