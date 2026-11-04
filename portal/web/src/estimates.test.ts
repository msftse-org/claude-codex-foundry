// @vitest-environment jsdom
import { createElement } from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { standardRateEstimate, tokenCount } from "./estimates";
import { Spend } from "./Spend";
import type { State, Telemetry } from "./types";

afterEach(cleanup);

const row = {
    model: "sonnet",
    group_id: "g",
    principal_id: "p",
    input_tokens: 1_000_000,
    output_tokens: 500_000,
    cached_tokens: 100_000,
};
it("keeps missing prices visible instead of silently valuing usage at zero", () => {
    expect(standardRateEstimate([row], [])).toEqual({
        usd: 0,
        complete: false,
        missingModels: ["sonnet"],
        cacheRateMissingModels: [],
        providerMissingModels: ["sonnet"],
    });
});
it("uses the input rate for missing cache rates and discloses the fallback", () => {
    expect(
        standardRateEstimate(
            [row],
            [
                {
                    model: "sonnet",
                    provider: "anthropic",
                    input_per_million: 3,
                    output_per_million: 15,
                },
            ],
        ),
    ).toEqual({
        usd: 10.8,
        complete: true,
        missingModels: [],
        cacheRateMissingModels: ["sonnet"],
        providerMissingModels: [],
    });
});

// price_catalog.json: GPT Data Zone Standard (1.1×); Claude Global Standard.
const prices: State["prices"] = [
    {
        model: "gpt-6-astra",
        provider: "openai",
        input_per_million: 11,
        cache_read_per_million: 1.1,
        output_per_million: 55,
    },
    {
        model: "claude-opus-5-5-fast",
        provider: "anthropic",
        input_per_million: 8,
        cache_read_per_million: 0.4,
        output_per_million: 40,
    },
];
const fixtures = [
    {
        ...row,
        model: "gpt-6-astra",
        input_tokens: 53_868_980,
        cached_tokens: 52_249_118,
        output_tokens: 203_935,
    },
    {
        ...row,
        model: "claude-opus-5-5-fast",
        input_tokens: 89_065,
        cached_tokens: 65_576_660,
        output_tokens: 158_085,
    },
];
it.each([
    [0, 86.5089368, 603.775205],
    [1, 33.266584, 7.03592],
])(
    "corrects reviewed fixture %i rather than using uncached-only arithmetic",
    (index, expected, old) => {
        const value = fixtures[index];
        expect(standardRateEstimate([value], prices).usd).toBeCloseTo(
            expected,
            9,
        );
        const price = prices[index];
        expect(
            (value.input_tokens * price.input_per_million +
                value.output_tokens * price.output_per_million) /
                1e6,
        ).toBeCloseTo(old, 9);
    },
);

it.each([
    ["openai", 100, 80, 10, 0.3, 0.000234],
    ["anthropic", 100, 80, 10, 0.3, 0.000474],
    ["openai", 100, 80, 10, null, 0.00045],
    ["anthropic", 100, 80, 10, null, 0.00069],
    ["openai", 100, 80, 10, 0, 0.00021],
    ["anthropic", 100, 80, 10, 0, 0.00045],
    ["openai", 100, 200, 10, 0.3, 0.00018],
    ["anthropic", 100, 200, 10, 0.3, 0.00051],
    ["openai", 0, 200, 0, null, 0],
    ["anthropic", 0, 200, 0, null, 0.0006],
    ["openai", -100, 20, -10, 0.3, 0],
    ["anthropic", -100, 20, -10, 0.3, 0.000006],
    ["anthropic", NaN, 20, Infinity, 0.3, 0.000006],
    [undefined, 100, 20, 10, 0.3, 0],
    [undefined, 100, 0, 10, null, 0.00045],
] as const)(
    "matches KQL for %s / input %s / cached %s / output %s / cache rate %s",
    (provider, input, cached, output, cache, expected) => {
        const estimate = standardRateEstimate(
            [
                {
                    ...row,
                    input_tokens: input,
                    cached_tokens: cached,
                    output_tokens: output,
                },
            ],
            [
                {
                    model: "sonnet",
                    provider,
                    input_per_million: 3,
                    output_per_million: 15,
                    cache_read_per_million: cache,
                },
            ],
        );
        expect(estimate.usd).toBeCloseTo(expected, 12);
        expect(estimate.complete).toBe(!!provider || cached === 0);
        expect(estimate.cacheRateMissingModels).toEqual(
            cache == null && cached > 0 ? ["sonnet"] : [],
        );
    },
);

it("uses explicit provider metadata even when the deployment name suggests another provider", () => {
    const usage = { ...row, model: "gpt-looking-name" };
    const price = [
        {
            model: usage.model,
            input_per_million: 3,
            output_per_million: 15,
            cache_read_per_million: 0.3,
        },
    ];
    const models: State["models"] = [
        {
            name: usage.model,
            provider: "claude",
            labelOverride: "Synthetic",
            anthropicFamilyTier: "",
        },
    ];
    expect(standardRateEstimate([usage], price, models).usd).toBe(10.53);
    expect(standardRateEstimate([usage], price).complete).toBe(false);
    expect(
        standardRateEstimate(
            [usage],
            [{ ...price[0], provider: "openai" }],
            models,
        ).usd,
    ).toBe(10.23);
});

it("does not lose unpriced Claude cache-only consumption", () => {
    const usage = { ...row, input_tokens: 0, output_tokens: 0 };
    expect(standardRateEstimate([usage], []).complete).toBe(false);
    expect(standardRateEstimate([usage], []).missingModels).toEqual(["sonnet"]);
    expect(tokenCount(fixtures[0], prices)).toBe(54_072_915);
    expect(tokenCount(fixtures[1], prices)).toBe(65_823_810);
});

it("keeps known provider token coverage even when planning prices are missing", () => {
    const models: State["models"] = [
        {
            name: "gpt-6-astra",
            provider: "codex",
            labelOverride: "Synthetic GPT",
            anthropicFamilyTier: "",
        },
    ];
    const estimate = standardRateEstimate([fixtures[0]], [], models);
    expect(estimate.missingModels).toEqual(["gpt-6-astra"]);
    expect(estimate.providerMissingModels).toEqual([]);
    expect(tokenCount(fixtures[0], [], models)).toBe(54_072_915);
});

const member: State["viewer"] = {
    id: "p",
    name: "Synthetic member",
    email: "member@example.test",
    role: "owner",
    status: "active",
    group_id: "g",
    monthly_tokens: 0,
    extra_models: [],
};
const state: State = {
    revision: 1,
    viewer: member,
    members: [member],
    groups: [
        {
            id: "g",
            name: "Synthetic group",
            description: "",
            model_ids: prices.map((p) => p.model),
            default_model: prices[0].model,
            monthly_tokens: 0,
            budget_usd: 0,
        },
    ],
    models: prices.map((p) => ({
        name: p.model,
        labelOverride: p.model,
        anthropicFamilyTier: "",
        provider: p.provider,
    })),
    settings: {
        name: "Synthetic organization",
        monthly_budget_usd: 0,
        monthly_tokens: 0,
        member_tokens_per_minute: 0,
        member_requests_per_minute: 0,
    },
    prices,
    audit: [],
    requests: [],
    published_revision: 1,
    policy_hash: "synthetic",
    publish_status: "published",
    published_at: null,
    gateway_url: "",
    tenant_id: "",
};
const usage: Telemetry = {
    status: "connected",
    daily: [],
    models: [],
    people: [],
    requests: [],
    month_tokens: fixtures,
    planning_prices: prices,
    reporting_start: "2026-10-05",
    reporting_end: "2026-11-04",
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
            ],
        })),
    },
};
const showSpend = (data = usage, portalState = state) =>
    render(
        createElement(Spend, {
            state: portalState,
            usage: data,
            finance: false,
            busy: false,
            save: vi.fn().mockResolvedValue(true),
        }),
    );

it("shows cache-correct Spend amounts separately from full-month charges and held funds", () => {
    showSpend(usage, {
        ...state,
        prices: prices.map((p) => ({ ...p, cache_read_per_million: 999 })),
    });
    const estimate = screen
        .getByText("Estimated from token metrics")
        .closest(".stat")!;
    expect(within(estimate as HTMLElement).getByText("$119.78")).toBeTruthy();
    expect(
        within(screen.getByRole("row", { name: /gpt-6-astra/ })).getByText(
            "$86.51",
        ),
    ).toBeTruthy();
    expect(
        within(
            screen.getByRole("row", { name: /claude-opus-5-5-fast/ }),
        ).getByText("$33.27"),
    ).toBeTruthy();
    const ledger = screen.getByRole("table", {
        name: "Calendar-month budget charges",
    });
    expect(
        within(ledger).getByRole("columnheader", { name: "Held, not charged" }),
    ).toBeTruthy();
    expect(within(ledger).getAllByText("$3.199344")).toHaveLength(2);
    expect(within(ledger).getAllByText("$4.72877")).toHaveLength(2);
    expect(within(ledger).getByText("2026-10")).toBeTruthy();
    expect(within(ledger).getByText("2026-11")).toBeTruthy();
    expect(
        screen.getByText(
            /OpenAI includes cache reads; Claude cache reads are additional/,
        ),
    ).toBeTruthy();
    expect(
        screen.queryByText(/Group attribution begins after publishing/),
    ).toBeNull();
    expect(
        screen.getByText(
            /Usage recorded before this upgrade stays unattributed/,
        ),
    ).toBeTruthy();
});

it("shows the missing cache-rate warning and still counts cached tokens", () => {
    showSpend({
        ...usage,
        planning_prices: prices.map((p) => ({
            ...p,
            cache_read_per_million: null,
        })),
    });
    expect(
        screen.getByText(/Rate coverage · Cache rate missing/).textContent,
    ).toContain("gpt-6-astra");
    expect(
        screen.getByText(/Rate coverage · Cache rate missing/).textContent,
    ).toContain("claude-opus-5-5-fast");
});

it("keeps Spend estimate-only when no ledger is configured", () => {
    showSpend({
        ...usage,
        ledger_charges: {
            configured: false,
            status: "not_configured",
            currency: "USD",
            months: [],
        },
    });
    expect(
        screen.queryByText("Charged by the budget service (calendar month)"),
    ).toBeNull();
    expect(screen.getByText("Estimated from token metrics")).toBeTruthy();
});

it("shows authoritative charges even when token monitoring is not configured", () => {
    showSpend({ ...usage, status: "not_configured", month_tokens: [] });
    expect(screen.getByText("Usage is not connected")).toBeTruthy();
    expect(
        screen.getByRole("table", { name: "Calendar-month budget charges" }),
    ).toBeTruthy();
    expect(
        screen.getByText(/gateway does not report Claude cache-write tokens/),
    ).toBeTruthy();
    expect(screen.getByText(/per-request long-context tier/)).toBeTruthy();
});

it("surfaces reporting errors without stale or zero ledger charges", () => {
    showSpend({ ...usage, status: "error" });
    expect(
        screen.getByText(/calendar-month charges are unavailable, not zero/),
    ).toBeTruthy();
    expect(
        screen.queryByRole("table", { name: "Calendar-month budget charges" }),
    ).toBeNull();
    expect(screen.queryByText("Usage is not connected")).toBeNull();
});
