// @vitest-environment jsdom
import { createElement } from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "./api";
import { CostAnalysis } from "./CostAnalysis";
import type { State } from "./types";
import {
    breakdownRows,
    change,
    dailySeries,
    filterSearch,
    ledgerMoney,
    presetDates,
    readFilters,
    validRange,
    zero,
    type CostResult,
} from "./cost-analysis";

vi.mock("./api", () => ({ api: vi.fn(), exportCsv: vi.fn() }));
afterEach(cleanup);
beforeEach(() => {
    vi.mocked(api).mockReset();
    history.replaceState(
        {},
        "",
        "?page=costs&c_start=2026-10-31&c_end=2026-11-01",
    );
});

it("uses inclusive UTC periods across leap days and month boundaries", () => {
    expect(presetDates("7", "2024-03-02")).toEqual({
        start: "2024-02-25",
        end: "2024-03-02",
    });
    expect(presetDates("month", "2024-03-02")).toEqual({
        start: "2024-03-01",
        end: "2024-03-02",
    });
    expect(validRange("2024-02-30", "2024-03-01")).toBe(false);
    expect(validRange("2024-01-01", "2024-04-03")).toBe(false);
    expect(validRange("2024-03-02", "2024-03-01")).toBe(false);
});
it("round-trips combined filter values without losing special characters", () => {
    const filters = {
        start: "2024-02-01",
        end: "2024-02-29",
        model_ids: ['a"; &x'],
        group_ids: ["__unattributed__"],
        member_ids: ["member1", "member2"],
        grouping: "member" as const,
    };
    expect(readFilters(filterSearch(filters, "?page=costs"))).toEqual(filters);
    expect(readFilters("?c_group=inject&c_start=broken").grouping).toBe(
        "model",
    );
});
it("zero-fills days and aligns comparison by day offset", () => {
    const data = {
        start: "2024-03-01",
        end: "2024-03-02",
        previous_start: "2024-02-28",
        daily: [
            { ...zero(), key: "2024-03-02", input_tokens: 5 },
            { ...zero("previous"), key: "2024-02-29", input_tokens: 3 },
        ],
        breakdown: [
            { ...zero(), key: "a", input_tokens: 5 },
            { ...zero("previous"), key: "b", input_tokens: 8 },
        ],
    } as CostResult;
    const series = dailySeries(data);
    expect(series[0].current.input_tokens).toBe(0);
    expect(series[1].previousDate).toBe("2024-02-29");
    expect(series[1].previous.input_tokens).toBe(3);
    expect(
        breakdownRows(data).find((r) => r.key === "b")?.current.input_tokens,
    ).toBe(0);
});
it("does not report infinite percentage growth from no previous usage", () => {
    expect(change(20, 0)).toBe("New usage");
    expect(change(0, 0)).toBe("No change");
    expect(change(75, 100)).toBe("-25.0%");
});

it("keeps exact micro-USD ledger amounts separate from rounded planning estimates", () => {
    expect(ledgerMoney(3_199_344)).toBe("$3.199344");
    expect(ledgerMoney(4_728_770)).toBe("$4.72877");
    expect(ledgerMoney(1)).toBe("$0.000001");
});

it("selects inclusive UTC reporting dates across a year boundary", () => {
    expect(presetDates("7", "2025-01-02")).toEqual({
        start: "2024-12-27",
        end: "2025-01-02",
    });
});

const member: State["viewer"] = {
    id: "synthetic-member",
    name: "Synthetic member",
    email: "member@example.test",
    role: "owner",
    status: "active",
    group_id: "synthetic-group",
    monthly_tokens: 0,
    extra_models: [],
};
const state: State = {
    revision: 1,
    viewer: member,
    members: [member],
    groups: [
        {
            id: "synthetic-group",
            name: "Synthetic group",
            description: "",
            model_ids: ["gpt-6-astra"],
            default_model: "gpt-6-astra",
            monthly_tokens: 0,
            budget_usd: 0,
        },
    ],
    models: [
        {
            name: "gpt-6-astra",
            labelOverride: "Synthetic GPT",
            anthropicFamilyTier: "",
            provider: "codex",
        },
    ],
    settings: {
        name: "Synthetic organization",
        monthly_budget_usd: 0,
        monthly_tokens: 0,
        member_tokens_per_minute: 0,
        member_requests_per_minute: 0,
    },
    prices: [
        {
            model: "gpt-6-astra",
            provider: "openai",
            input_per_million: 11,
            cache_read_per_million: 1.1,
            output_per_million: 55,
        },
    ],
    audit: [],
    requests: [],
    published_revision: 1,
    policy_hash: "synthetic",
    publish_status: "published",
    published_at: null,
    gateway_url: "",
    tenant_id: "",
};
const result: CostResult = {
    grouping: "model",
    status: "connected",
    start: "2026-10-31",
    end: "2026-11-01",
    previous_start: "2026-10-29",
    previous_end: "2026-10-30",
    generated_at: "2026-11-04T00:00:00Z",
    totals: [
        {
            ...zero(),
            input_tokens: 53_868_980,
            cached_tokens: 52_249_118,
            output_tokens: 203_935,
            total_tokens: 54_072_915,
            usd: 86.5089368,
        },
    ],
    daily: [],
    breakdown: [],
    truncated: false,
    breakdown_limit: 2500,
    ledger_charges: {
        configured: true,
        status: "connected",
        currency: "USD",
        months: ["2026-10", "2026-11"].map((month) => ({
            month,
            scopes: [
                {
                    scope: "org",
                    spent_micro_usd: 3_199_344,
                    reserved_micro_usd: 4_728_770,
                },
                {
                    scope: "group-synthetic-group",
                    spent_micro_usd: 2_000_000,
                    reserved_micro_usd: 1_000_000,
                },
                {
                    scope: "user-synthetic-member",
                    spent_micro_usd: 500_000,
                    reserved_micro_usd: 250_000,
                },
            ],
        })),
    },
};
function showCosts(data = result, portalState = state) {
    vi.mocked(api).mockResolvedValue(data);
    return render(
        createElement(CostAnalysis, {
            state: portalState,
            busy: false,
            save: vi.fn().mockResolvedValue(true),
        }),
    );
}

it("renders cache-correct cost and authoritative monthly charges without adding holds to either", async () => {
    showCosts();
    expect(await screen.findByText("$86.51")).toBeTruthy();
    expect(screen.getByText("Estimated from token metrics")).toBeTruthy();
    expect(
        screen.getByText("Charged by the budget service (calendar month)"),
    ).toBeTruthy();
    const table = screen.getByRole("table", {
        name: "Calendar-month budget charges",
    });
    expect(within(table).getAllByText("$3.199344")).toHaveLength(2);
    expect(within(table).getAllByText("$4.72877")).toHaveLength(2);
    expect(
        within(table).getByRole("columnheader", { name: "Held, not charged" }),
    ).toBeTruthy();
    expect(within(table).getAllByText("2026-10")).toHaveLength(3);
    expect(within(table).getAllByText("2026-11")).toHaveLength(3);
    expect(within(table).getAllByText("Group: Synthetic group")).toHaveLength(
        2,
    );
    expect(within(table).getAllByText("Member: Synthetic member")).toHaveLength(
        2,
    );
    expect(screen.getByText(/not prorated to selected days/)).toBeTruthy();
    expect(
        screen.getByText(/gateway does not report Claude cache-write tokens/),
    ).toBeTruthy();
    expect(screen.getByText(/per-request long-context tier/)).toBeTruthy();
    expect(
        screen.getByText(/Streaming tokens can be estimates or incomplete/),
    ).toBeTruthy();
    expect(vi.mocked(api).mock.calls[0][0]).toBe("/api/cost-analysis");
});

it("shows cache-rate coverage fallback by model name", async () => {
    showCosts({
        ...result,
        totals: [
            { ...result.totals[0], cache_rate_missing_models: ["gpt-6-astra"] },
        ],
    });
    const note = await screen.findByText(/Cache rate missing: Synthetic GPT/);
    expect(note.textContent).toContain("Cached tokens use the input rate");
    expect(screen.getByText("$86.51")).toBeTruthy();
});

it("labels the November comparison without treating its estimate or holds as charges", async () => {
    showCosts({
        ...result,
        start: "2026-11-01",
        totals: [{ ...result.totals[0], usd: 3.93 }],
        ledger_charges: {
            ...result.ledger_charges!,
            months: [result.ledger_charges!.months[1]],
        },
    });
    expect(await screen.findByText("$3.93")).toBeTruthy();
    const table = screen.getByRole("table", {
        name: "Calendar-month budget charges",
    });
    const organization = within(table).getByRole("row", {
        name: /Organization/,
    });
    expect(within(organization).getByText("$3.199344")).toBeTruthy();
    expect(within(organization).getByText("$4.72877")).toBeTruthy();
    expect(within(table).queryByText("$3.93")).toBeNull();
});

it("marks estimates incomplete rather than guessing providers", async () => {
    showCosts({
        ...result,
        totals: [
            {
                ...result.totals[0],
                usd: 0,
                unpriced_tokens: 54_072_915,
                provider_missing_models: ["gpt-6-astra"],
            },
        ],
    });
    expect(await screen.findByText("Rates needed")).toBeTruthy();
    expect(
        screen.getByText(/Provider metadata missing: Synthetic GPT/),
    ).toBeTruthy();
    expect(screen.queryByText("$86.51")).toBeNull();
});

it.each([
    "owner",
    "admin",
    "billing",
    "auditor",
    "group_admin",
    "developer",
] as const)(
    "renders only the API-authorized ledger scopes for %s",
    async (role) => {
        const scopes = result.ledger_charges!.months[0].scopes.filter((row) =>
            role === "developer"
                ? row.scope.startsWith("user-")
                : role === "group_admin"
                  ? row.scope !== "org"
                  : true,
        );
        showCosts(
            {
                ...result,
                ledger_charges: {
                    ...result.ledger_charges!,
                    months: [{ month: "2026-11", scopes }],
                },
            },
            { ...state, viewer: { ...member, role } },
        );
        const table = await screen.findByRole("table", {
            name: "Calendar-month budget charges",
        });
        expect(within(table).getAllByRole("row")).toHaveLength(
            scopes.length + 1,
        );
        if (role === "developer" || role === "group_admin") {
            expect(within(table).queryByText("Organization")).toBeNull();
            expect(within(table).queryByText("$3.199344")).toBeNull();
        }
        if (role === "developer")
            expect(
                within(table).queryByText("Group: Synthetic group"),
            ).toBeNull();
    },
);

it("keeps the estimate when ledger configuration is absent", async () => {
    showCosts({
        ...result,
        ledger_charges: {
            configured: false,
            status: "not_configured",
            currency: "USD",
            months: [],
        },
    });
    expect(await screen.findByText("$86.51")).toBeTruthy();
    expect(
        screen.queryByText("Charged by the budget service (calendar month)"),
    ).toBeNull();
});

it("does not render a zero ledger table for unpublished scope", async () => {
    showCosts({
        ...result,
        ledger_charges: {
            configured: true,
            status: "pending_publication",
            currency: "USD",
            months: [],
            note: "Membership changes await budget publication.",
        },
    });
    expect(
        await screen.findByText("Membership changes await budget publication."),
    ).toBeTruthy();
    expect(
        screen.queryByRole("table", { name: "Calendar-month budget charges" }),
    ).toBeNull();
});

it("surfaces ledger read failures instead of displaying invented zero charges", async () => {
    vi.mocked(api).mockRejectedValue(
        new Error(
            "Calendar-month charges could not be loaded from the budget ledger.",
        ),
    );
    render(createElement(CostAnalysis, { state, busy: false, save: vi.fn() }));
    expect(
        await screen.findByText(/Calendar-month charges could not be loaded/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(
        screen.queryByRole("table", { name: "Calendar-month budget charges" }),
    ).toBeNull();
});
