// @vitest-environment jsdom
import {
    act,
    cleanup,
    fireEvent,
    render,
    screen,
    within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { BudgetPanel } from "./budgets";
import type { State } from "./types";

vi.mock("./api", () => ({ api: vi.fn() }));

const state: State = {
    revision: 1,
    viewer: {
        id: "test-owner",
        name: "Test owner",
        email: "owner@example.test",
        role: "owner",
        status: "active",
        group_id: "platform",
        monthly_tokens: 0,
        extra_models: [],
    },
    members: [],
    groups: [],
    models: [],
    audit: [],
    prices: [],
    requests: [],
    settings: {
        name: "Test organization",
        monthly_budget_usd: 0,
        monthly_tokens: 0,
        member_tokens_per_minute: 0,
        member_requests_per_minute: 0,
    },
    published_revision: 1,
    policy_hash: "test",
    publish_status: "published",
    published_at: null,
    gateway_url: "",
    tenant_id: "test",
};
const balance = {
    configured: true,
    active: true,
    month: "2026-10",
    published_hash: "active-policy",
    scopes: [
        {
            scope: "org",
            limit_micro_usd: 10_000_000,
            spent_micro_usd: 938420,
            reserved_micro_usd: 0,
            remaining_micro_usd: 9061580,
        },
        {
            scope: "group-platform",
            limit_micro_usd: 5_000_000,
            spent_micro_usd: 938420,
            reserved_micro_usd: 0,
            remaining_micro_usd: 4061580,
        },
        {
            scope: "user-test-owner",
            limit_micro_usd: 1_000_000,
            spent_micro_usd: 938420,
            reserved_micro_usd: 0,
            remaining_micro_usd: 61580,
        },
    ],
};
const reads: Promise<unknown>[] = [];
const save = vi.fn();
function deferred() {
    let resolve!: (value: unknown) => void;
    let reject!: (reason: Error) => void;
    const promise = new Promise<unknown>((yes, no) => {
        resolve = yes;
        reject = no;
    });
    return { promise, resolve, reject };
}
const refreshButton = () =>
    screen.getByRole<HTMLButtonElement>("button", { name: "Refresh balance" });
const table = () =>
    screen.getByRole("table", { name: "Monthly spending limits" });
const publishButton = () =>
    screen.getByRole<HTMLButtonElement>("button", {
        name: "Publish budget policy",
    });

beforeEach(() => {
    reads.length = 0;
    vi.mocked(api).mockReset();
    vi.mocked(api).mockImplementation((path) => {
        if (path === "/api/budgets/balance") {
            const result = reads.shift();
            if (!result) throw new Error("Unexpected balance request");
            return result;
        }
        if (path === "/api/budgets/reservations")
            return Promise.resolve({ items: [], has_more: false });
        throw new Error("Unexpected API path: " + path);
    });
});
afterEach(cleanup);

describe("verified enforcement rate inputs", () => {
    it("accepts whole token capacities and fractional prices with native browser validation", async () => {
        reads.push(Promise.resolve(balance));
        render(<BudgetPanel state={state} busy={false} save={save} />);
        await screen.findByText("$0.06158");
        fireEvent.click(
            screen.getByRole("button", { name: "Add verified model rate" }),
        );
        for (const [name, value] of [
            ["Verified maximum input tokens", "1000000"],
            ["Verified maximum output tokens", "128000"],
            ["Default output limit", "4096"],
        ]) {
            const input = screen.getByRole<HTMLInputElement>("spinbutton", {
                name,
            });
            fireEvent.change(input, { target: { value } });
            expect(input.checkValidity()).toBe(true);
            fireEvent.change(input, { target: { value: "1.5" } });
            expect(input.validity.stepMismatch).toBe(true);
        }
        const price = screen.getByRole<HTMLInputElement>("spinbutton", {
            name: "Input ceiling (USD / million tokens)",
        });
        fireEvent.change(price, { target: { value: "0.000001" } });
        expect(price.checkValidity()).toBe(true);
        fireEvent.change(price, { target: { value: "1.25" } });
        expect(price.checkValidity()).toBe(true);
        fireEvent.change(price, { target: { value: "0" } });
        expect(price.validity.rangeUnderflow).toBe(true);
    });
});

describe("budget table refresh", () => {
    it("reserves the table layout during initial loading and disables refresh and publication", async () => {
        const first = deferred();
        reads.push(first.promise);
        render(<BudgetPanel state={state} busy={false} save={save} />);
        const initialTable = table();
        expect(within(initialTable).getAllByRole("row")).toHaveLength(4);
        expect(refreshButton().disabled).toBe(true);
        expect(refreshButton().getAttribute("aria-busy")).toBe("true");
        expect(publishButton().disabled).toBe(true);
        expect(screen.getByRole("status").textContent).toBe(
            "Loading balances…",
        );
        await act(async () => first.resolve(balance));
        expect(table()).toBe(initialTable);
        expect(refreshButton().disabled).toBe(false);
        expect(publishButton().disabled).toBe(false);
    });

    it("keeps the same table and values during refresh, prevents repeat clicks and updates in place", async () => {
        const next = deferred();
        reads.push(Promise.resolve(balance), next.promise);
        render(<BudgetPanel state={state} busy={false} save={save} />);
        await screen.findByText("$0.06158");
        const originalTable = table();
        fireEvent.click(refreshButton());
        fireEvent.click(refreshButton());
        expect(table()).toBe(originalTable);
        expect(within(originalTable).getByText("$0.06158")).toBeTruthy();
        expect(
            screen
                .getByRole("region", { name: "Monthly spending balances" })
                .getAttribute("aria-busy"),
        ).toBe("true");
        expect(
            refreshButton().querySelector(".budget-refresh-spinner"),
        ).toBeTruthy();
        expect(publishButton().disabled).toBe(true);
        expect(
            vi
                .mocked(api)
                .mock.calls.filter(([path]) => path === "/api/budgets/balance"),
        ).toHaveLength(2);
        await act(async () =>
            next.resolve({
                ...balance,
                scopes: balance.scopes.map((s) => ({
                    ...s,
                    reserved_micro_usd: 10000,
                })),
            }),
        );
        expect(table()).toBe(originalTable);
        expect(within(originalTable).getAllByText("$0.01")).toHaveLength(3);
        expect(refreshButton().disabled).toBe(false);
        expect(
            refreshButton().querySelector(".budget-refresh-spinner"),
        ).toBeNull();
    });

    it("retains last loaded balances after failure, marks them stale and recovers on retry", async () => {
        const next = deferred();
        reads.push(
            Promise.resolve(balance),
            next.promise,
            Promise.resolve(balance),
        );
        render(<BudgetPanel state={state} busy={false} save={save} />);
        await screen.findByText("$0.06158");
        const originalTable = table();
        fireEvent.click(refreshButton());
        await act(async () =>
            next.reject(new Error("Ledger temporarily unavailable")),
        );
        expect(table()).toBe(originalTable);
        expect(within(originalTable).getByText("$0.06158")).toBeTruthy();
        expect(screen.getByRole("alert").textContent).toContain(
            "Showing the last loaded balances",
        );
        expect(screen.getByRole("status").textContent).toBe(
            "Balances may be out of date.",
        );
        expect(refreshButton().disabled).toBe(false);
        expect(publishButton().disabled).toBe(true);
        fireEvent.click(refreshButton());
        await screen.findByText(/Last refreshed at/);
        expect(screen.queryByRole("alert")).toBeNull();
        expect(publishButton().disabled).toBe(false);
    });

    it("ignores an older response after a policy revision starts a newer refresh", async () => {
        const old = deferred(),
            latest = deferred();
        reads.push(old.promise, latest.promise);
        const view = render(
            <BudgetPanel state={state} busy={false} save={save} />,
        );
        view.rerender(
            <BudgetPanel
                state={{ ...state, revision: 2 }}
                busy={false}
                save={save}
            />,
        );
        await act(async () => latest.resolve({ ...balance, month: "2026-11" }));
        await act(async () => old.resolve(balance));
        expect(screen.getByText(/Budget policy active/).textContent).toContain(
            "2026-11",
        );
        expect(refreshButton().disabled).toBe(false);
    });

    it("removes old balances when a successful refresh reports no active policy", async () => {
        reads.push(
            Promise.resolve(balance),
            Promise.resolve({
                configured: true,
                active: false,
                scopes: [],
                note: "Membership changes await budget publication",
            }),
        );
        render(<BudgetPanel state={state} busy={false} save={save} />);
        await screen.findByText("$0.06158");
        fireEvent.click(refreshButton());
        await screen.findByText("No active budget balances to display.");
        expect(screen.queryByText("$0.06158")).toBeNull();
        expect(
            screen.getByText("Membership changes await budget publication"),
        ).toBeTruthy();
    });

    it("never retains another identity or group's balances across a viewer change", async () => {
        const next = deferred();
        reads.push(Promise.resolve(balance), next.promise);
        const view = render(
            <BudgetPanel state={state} busy={false} save={save} />,
        );
        await screen.findByText("$0.06158");
        view.rerender(
            <BudgetPanel
                state={{
                    ...state,
                    viewer: {
                        ...state.viewer,
                        role: "developer",
                        group_id: "different-team",
                    },
                }}
                busy={false}
                save={save}
            />,
        );
        expect(screen.queryByText("$0.06158")).toBeNull();
        expect(within(table()).queryByText("Organization")).toBeNull();
        expect(
            screen.queryByRole("group", { name: "Budget policy actions" }),
        ).toBeNull();
        await act(async () =>
            next.resolve({ ...balance, scopes: [balance.scopes[2]] }),
        );
        expect(within(table()).queryByText("Organization")).toBeNull();
    });
});
