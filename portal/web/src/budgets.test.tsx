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
const holdReads: Promise<unknown>[] = [];
const writes: Promise<unknown>[] = [];
const held = {
    request_id: "00000000-0000-0000-0000-000000000101",
    principal_id: "synthetic-member",
    member_name: "Synthetic member",
    model: "synthetic-model",
    at: "2026-10-01T10:00:00Z" as string | null,
    month: "2026-10",
    reserved: 1_234_567,
    hold_reason: null as string | null,
    held_at: null as string | null,
    gateway_outcome: {
        result_code: "429" as string | null,
        timestamp: "2026-10-01T10:01:00Z" as string | null,
        cause: "Throttled, not billed",
        releasable: true,
    },
};
const emptyHolds = {
    items: [] as (typeof held)[],
    totals: [] as {
        month: string;
        count: number;
        held_micro_usd: number;
    }[],
    has_more: false,
    next_cursor: null as string | null,
    read_limit: 10_000,
    read_limit_reached: false,
    outcomes_status: "connected",
};
const heldPage = {
    ...emptyHolds,
    items: [held],
    totals: [
        { month: "2026-11", count: 2, held_micro_usd: 2_500_000 },
        { month: "2026-10", count: 3, held_micro_usd: 7_000_000 },
    ],
};
const save = vi.fn();
const reload = vi.fn();
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
    holdReads.length = 0;
    writes.length = 0;
    reload.mockReset();
    vi.stubGlobal("location", { reload });
    vi.mocked(api).mockReset();
    vi.mocked(api).mockImplementation((path) => {
        if (path === "/api/budgets/balance") {
            const result = reads.shift();
            if (!result) throw new Error("Unexpected balance request");
            return result;
        }
        if (path.startsWith("/api/budgets/reservations"))
            return holdReads.shift() || Promise.resolve(emptyHolds);
        if (
            path === "/api/budgets/release-unbilled" ||
            path === "/api/budgets/close-reservation"
        ) {
            const result = writes.shift();
            if (!result) throw new Error("Unexpected reservation mutation");
            return result;
        }
        if (path === "/api/budgets/incident")
            return Promise.resolve({ incident: null });
        throw new Error("Unexpected API path: " + path);
    });
});
afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
});

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

describe("explicit unlimited policy", () => {
    it("renders unlimited scope allowances while preserving recorded spend", async () => {
        reads.push(
            Promise.resolve({
                ...balance,
                scopes: balance.scopes.map((s) => ({
                    ...s,
                    limit_micro_usd: null,
                    remaining_micro_usd: null,
                })),
            }),
        );
        render(<BudgetPanel state={state} busy={false} save={save} />);
        await screen.findAllByText("Unlimited");
        expect(within(table()).getAllByText("Unlimited")).toHaveLength(6);
        expect(within(table()).getAllByText("$0.93842")).toHaveLength(3);
    });

    describe("held reservations", () => {
        const holdsTable = () =>
            screen.getByRole("table", { name: "Held reservations" });
        const monthTable = () =>
            screen.getByRole("table", { name: "Held reservations by month" });
        const renderHolds = (
            page = heldPage,
            viewer: State["viewer"] = state.viewer,
        ) => {
            reads.push(Promise.resolve(balance));
            holdReads.push(Promise.resolve(page));
            return render(
                <BudgetPanel
                    state={{ ...state, viewer }}
                    busy={false}
                    save={save}
                />,
            );
        };

        it("separates all-month held totals from charges and shows started time, age, member, model and old metadata", async () => {
            renderHolds();
            await screen.findByText(held.request_id);
            expect(
                screen.getByText(/worst-case cap, not a charge/),
            ).toBeTruthy();
            expect(within(monthTable()).getByText("$7.00")).toBeTruthy();
            expect(within(monthTable()).getByText("$2.50")).toBeTruthy();
            expect(within(monthTable()).getByText("3")).toBeTruthy();
            expect(within(monthTable()).getAllByRole("row")).toHaveLength(3);
            const row = within(holdsTable()).getAllByRole("row")[1];
            for (const text of [
                "2026-10",
                "synthetic-model",
                "Synthetic member",
                "$1.234567",
                "Throttled, not billed",
                "Hold reason: Not recorded",
                "Held since: Not recorded",
                new Date(held.at!).toLocaleString(),
            ])
                expect(within(row).getByText(text)).toBeTruthy();
            expect(
                within(row).getByText(/\d+ (min|hr|days?) old/),
            ).toBeTruthy();
            expect(screen.queryByText(/Additional records require/)).toBeNull();
        });

        it.each([
            ["dispatch_unknown", "Dispatch outcome unknown"],
            ["client_disconnected", "Client disconnected"],
            ["stream_incomplete", "Incomplete stream"],
            ["rejected_possibly_billed", "Rejected, possibly billed"],
            ["usage_unreadable", "Usage unreadable"],
            ["settlement_failed", "Settlement failed"],
        ])(
            "shows the recorded %s hold reason and held-at timestamp",
            async (reason, label) => {
                renderHolds({
                    ...heldPage,
                    items: [{ ...held, hold_reason: reason, held_at: held.at }],
                });
                await screen.findByText(held.request_id);
                expect(screen.getByText("Hold reason: " + label)).toBeTruthy();
                expect(
                    screen.getByText(
                        "Held since: " + new Date(held.at!).toLocaleString(),
                    ),
                ).toBeTruthy();
            },
        );

        it("handles unknown historical start times without showing Invalid Date", async () => {
            renderHolds({ ...heldPage, items: [{ ...held, at: null }] });
            await screen.findByText(held.request_id);
            expect(within(holdsTable()).getByText("Not recorded")).toBeTruthy();
            expect(screen.getByText("Age not recorded")).toBeTruthy();
            expect(screen.queryByText(/Invalid Date/)).toBeNull();
        });

        it("pages forward and back with an encoded cursor while keeping whole-scope month totals", async () => {
            const cursor = "synthetic/+==";
            const first = {
                ...heldPage,
                has_more: true,
                next_cursor: cursor,
            };
            const second = {
                ...heldPage,
                items: [{ ...held, request_id: "synthetic-second-page" }],
            };
            renderHolds(first);
            holdReads.push(Promise.resolve(second), Promise.resolve(first));
            await screen.findByText(held.request_id);
            expect(
                screen.getByRole<HTMLButtonElement>("button", {
                    name: "Previous page",
                }).disabled,
            ).toBe(true);
            fireEvent.click(screen.getByRole("button", { name: "Next page" }));
            await screen.findByText("synthetic-second-page");
            expect(screen.queryByText(held.request_id)).toBeNull();
            expect(screen.getByText("Page 2")).toBeTruthy();
            expect(within(monthTable()).getByText("$7.00")).toBeTruthy();
            expect(api).toHaveBeenCalledWith(
                "/api/budgets/reservations?cursor=" +
                    encodeURIComponent(cursor),
            );
            expect(
                screen.getByRole<HTMLButtonElement>("button", {
                    name: "Next page",
                }).disabled,
            ).toBe(true);
            fireEvent.click(
                screen.getByRole("button", { name: "Previous page" }),
            );
            await screen.findByText(held.request_id);
            expect(screen.getByText("Page 1")).toBeTruthy();
        });

        it("shows loading and read failures without claiming an empty result, and retries", async () => {
            const pending = deferred();
            reads.push(Promise.resolve(balance));
            holdReads.push(pending.promise, Promise.resolve(heldPage));
            render(<BudgetPanel state={state} busy={false} save={save} />);
            await screen.findByText("Loading held reservations…");
            expect(
                screen.queryByText("No held reservations in your scope."),
            ).toBeNull();
            await act(async () =>
                pending.reject(new Error("Ledger unavailable")),
            );
            expect(screen.getByRole("alert").textContent).toBe(
                "Ledger unavailable",
            );
            fireEvent.click(
                screen.getByRole("button", { name: "Retry held reservations" }),
            );
            await screen.findByText(held.request_id);
            expect(screen.queryByRole("alert")).toBeNull();
        });

        it("shows an empty scoped response without actions or a fake zero-spend summary", async () => {
            renderHolds(emptyHolds);
            await screen.findByText("No held reservations in your scope.");
            expect(
                screen.queryByRole("table", { name: "Held reservations" }),
            ).toBeNull();
            expect(
                screen.queryByRole("table", {
                    name: "Held reservations by month",
                }),
            ).toBeNull();
            expect(
                screen.queryByRole("button", { name: "Release (not billed)" }),
            ).toBeNull();
        });

        it.each(["not_configured", "unavailable"])(
            "keeps holds visible when gateway outcomes are %s, without offering a release",
            async (status) => {
                renderHolds({
                    ...heldPage,
                    outcomes_status: status,
                    items: [
                        {
                            ...held,
                            gateway_outcome: {
                                result_code: null,
                                timestamp: null,
                                cause: "Gateway outcome unavailable",
                                releasable: false,
                            },
                        },
                    ],
                });
                await screen.findByText(held.request_id);
                expect(
                    screen.getByText(/Gateway outcomes are unavailable/),
                ).toBeTruthy();
                expect(within(monthTable()).getByText("$7.00")).toBeTruthy();
                expect(
                    screen.queryByRole("button", {
                        name: "Release (not billed)",
                    }),
                ).toBeNull();
                expect(
                    screen.getByRole("button", { name: "Close at maximum" }),
                ).toBeTruthy();
            },
        );

        it("labels the bounded read and monthly totals as potentially incomplete", async () => {
            renderHolds({ ...heldPage, read_limit_reached: true });
            await screen.findByText(held.request_id);
            expect(
                screen.getByText(/read limit of 10,000 holds was reached/),
            ).toBeTruthy();
            expect(
                screen.getByText(
                    /monthly totals cover only the rows read and may be incomplete/,
                ),
            ).toBeTruthy();
        });

        it.each([
            "admin",
            "billing",
            "auditor",
            "group_admin",
            "developer",
        ] as const)(
            "does not offer release or close actions to %s",
            async (role) => {
                renderHolds(heldPage, { ...state.viewer, role });
                await screen.findByText(held.request_id);
                expect(
                    screen.queryByRole("button", {
                        name: "Release (not billed)",
                    }),
                ).toBeNull();
                expect(
                    screen.queryByRole("button", { name: "Close at maximum" }),
                ).toBeNull();
            },
        );

        it("discards old pages and late responses when the viewer scope changes", async () => {
            const stalePage = deferred();
            const ownPage = {
                ...heldPage,
                items: [{ ...held, request_id: "own-request" }],
            };
            const view = renderHolds({
                ...heldPage,
                has_more: true,
                next_cursor: "old-scope",
            });
            holdReads.push(stalePage.promise, Promise.resolve(ownPage));
            await screen.findByText(held.request_id);
            fireEvent.click(screen.getByRole("button", { name: "Next page" }));
            reads.push(
                Promise.resolve({ ...balance, scopes: [balance.scopes[2]] }),
            );
            view.rerender(
                <BudgetPanel
                    state={{
                        ...state,
                        viewer: {
                            ...state.viewer,
                            id: "another-member",
                            role: "developer",
                            group_id: "another-team",
                        },
                    }}
                    busy={false}
                    save={save}
                />,
            );
            expect(screen.queryByText(held.request_id)).toBeNull();
            await screen.findByText("own-request");
            await act(async () =>
                stalePage.resolve({
                    ...heldPage,
                    items: [{ ...held, request_id: "foreign-request" }],
                }),
            );
            expect(screen.queryByText("foreign-request")).toBeNull();
            expect(screen.queryByText("Page 2")).toBeNull();
            expect(screen.getByText("own-request")).toBeTruthy();
            expect(
                screen.queryByRole("button", { name: "Release (not billed)" }),
            ).toBeNull();
        });

        it("requires an audited reason, prevents duplicate submission and reloads portal state and balances after release", async () => {
            const write = deferred();
            renderHolds();
            writes.push(write.promise);
            holdReads.push(Promise.resolve(emptyHolds));
            await screen.findByText(held.request_id);
            fireEvent.click(
                screen.getByRole("button", { name: "Release (not billed)" }),
            );
            const drawer = screen.getByRole("dialog", {
                name: "Release an unbilled hold",
            });
            expect(
                within(drawer).getByText(/server rechecks the gateway record/),
            ).toBeTruthy();
            expect(
                within(drawer).getByText(/leaves spent unchanged/),
            ).toBeTruthy();
            const reason = within(drawer).getByRole<HTMLTextAreaElement>(
                "textbox",
                { name: "Reason for release" },
            );
            expect(reason.required).toBe(true);
            expect(reason.minLength).toBe(5);
            expect(reason.maxLength).toBe(300);
            expect(reason.checkValidity()).toBe(false);
            fireEvent.change(reason, {
                target: { value: "Verified synthetic throttling" },
            });
            const confirm = within(drawer).getByRole<HTMLButtonElement>(
                "button",
                { name: "Release (not billed)" },
            );
            fireEvent.click(confirm);
            fireEvent.click(confirm);
            expect(confirm.disabled).toBe(true);
            expect(
                within(drawer).getByRole<HTMLButtonElement>("button", {
                    name: "Cancel",
                }).disabled,
            ).toBe(true);
            const calls = vi
                .mocked(api)
                .mock.calls.filter(
                    ([path]) => path === "/api/budgets/release-unbilled",
                );
            expect(calls).toHaveLength(1);
            expect(calls[0][1]?.method).toBe("POST");
            expect(JSON.parse(String(calls[0][1]?.body))).toEqual({
                request_id: held.request_id,
                reason: "Verified synthetic throttling",
            });
            await act(async () =>
                write.resolve({ released: true, charged_micro_usd: 0 }),
            );
            expect(reload).toHaveBeenCalledOnce();
            expect(
                vi
                    .mocked(api)
                    .mock.calls.filter(
                        ([path]) => path === "/api/budgets/reservations",
                    ),
            ).toHaveLength(1);
        });

        it("retains conservative close for a completed hold and never represents it as a refund", async () => {
            renderHolds({
                ...heldPage,
                items: [
                    {
                        ...held,
                        gateway_outcome: {
                            ...held.gateway_outcome,
                            result_code: "200",
                            cause: "Completed, not settled",
                            releasable: false,
                        },
                    },
                ],
            });
            writes.push(
                Promise.resolve({
                    closed: true,
                    charged_micro_usd: held.reserved,
                }),
            );
            holdReads.push(Promise.resolve(emptyHolds));
            await screen.findByText(held.request_id);
            expect(
                screen.queryByRole("button", { name: "Release (not billed)" }),
            ).toBeNull();
            fireEvent.click(
                screen.getByRole("button", { name: "Close at maximum" }),
            );
            const drawer = screen.getByRole("dialog", {
                name: "Close at the reserved maximum",
            });
            expect(
                within(drawer).getByText(
                    /records the full held amount as spent/,
                ),
            ).toBeTruthy();
            expect(
                within(drawer).getByText(/does not issue a refund/),
            ).toBeTruthy();
            expect(within(drawer).getByText("$1.234567")).toBeTruthy();
            fireEvent.change(
                within(drawer).getByRole("textbox", {
                    name: "Reason for conservative closure",
                }),
                {
                    target: { value: "Retain the full conservative charge" },
                },
            );
            fireEvent.click(
                within(drawer).getByRole("button", {
                    name: "Charge full reservation",
                }),
            );
            await act(async () => {});
            expect(reload).toHaveBeenCalledOnce();
            expect(api).toHaveBeenCalledWith("/api/budgets/close-reservation", {
                method: "POST",
                body: JSON.stringify({
                    request_id: held.request_id,
                    reason: "Retain the full conservative charge",
                }),
            });
            expect(
                vi
                    .mocked(api)
                    .mock.calls.some(
                        ([path]) => path === "/api/budgets/release-unbilled",
                    ),
            ).toBe(false);
            expect(within(drawer).queryByText(/Hold released at/)).toBeNull();
        });

        it("keeps the drawer and reason after a fresh server-evidence refusal", async () => {
            const write = deferred();
            renderHolds();
            writes.push(write.promise);
            await screen.findByText(held.request_id);
            fireEvent.click(
                screen.getByRole("button", { name: "Release (not billed)" }),
            );
            const drawer = screen.getByRole("dialog");
            const reason = within(drawer).getByRole<HTMLTextAreaElement>(
                "textbox",
                { name: "Reason for release" },
            );
            fireEvent.change(reason, {
                target: { value: "Review synthetic rejection" },
            });
            fireEvent.click(
                within(drawer).getByRole("button", {
                    name: "Release (not billed)",
                }),
            );
            await act(async () =>
                write.reject(
                    new Error(
                        "Completed, not settled. The hold was not released.",
                    ),
                ),
            );
            expect(within(drawer).getByRole("alert").textContent).toContain(
                "The hold was not released",
            );
            expect(reason.value).toBe("Review synthetic rejection");
            expect(
                within(drawer).getByRole<HTMLButtonElement>("button", {
                    name: "Release (not billed)",
                }).disabled,
            ).toBe(false);
            expect(
                vi
                    .mocked(api)
                    .mock.calls.filter(
                        ([path]) => path === "/api/budgets/reservations",
                    ),
            ).toHaveLength(1);
            expect(reload).not.toHaveBeenCalled();
        });

        it("preserves shared drawer unsaved-change protection", async () => {
            renderHolds();
            await screen.findByText(held.request_id);
            fireEvent.click(
                screen.getByRole("button", { name: "Release (not billed)" }),
            );
            const drawer = screen.getByRole("dialog");
            fireEvent.change(
                within(drawer).getByRole("textbox", {
                    name: "Reason for release",
                }),
                { target: { value: "Unsaved review reason" } },
            );
            fireEvent.click(
                within(drawer).getByRole("button", { name: "Cancel" }),
            );
            expect(
                within(drawer).getByText("Discard unsaved changes?"),
            ).toBeTruthy();
            fireEvent.click(
                within(drawer).getByRole("button", { name: "Keep editing" }),
            );
            expect(
                within(drawer).getByRole<HTMLTextAreaElement>("textbox").value,
            ).toBe("Unsaved review reason");
            expect(
                vi
                    .mocked(api)
                    .mock.calls.every(
                        ([, options]) => options?.method !== "POST",
                    ),
            ).toBe(true);
        });

        it("restores keyboard focus when a clean action drawer closes", async () => {
            renderHolds();
            await screen.findByText(held.request_id);
            const trigger = screen.getByRole("button", {
                name: "Release (not billed)",
            });
            trigger.focus();
            fireEvent.click(trigger);
            vi.useFakeTimers();
            fireEvent.click(
                within(screen.getByRole("dialog")).getByRole("button", {
                    name: "Cancel",
                }),
            );
            await act(async () => vi.advanceTimersByTime(200));
            expect(screen.queryByRole("dialog")).toBeNull();
            expect(document.activeElement).toBe(trigger);
        });
    });
    it("saves the owner's explicit unlimited choice without a hidden finite cap", async () => {
        reads.push(Promise.resolve(balance));
        save.mockResolvedValueOnce(true);
        render(<BudgetPanel state={state} busy={false} save={save} />);
        await screen.findByText("$0.06158");
        fireEvent.click(
            screen.getByRole("button", { name: "Organization limit" }),
        );
        fireEvent.click(
            screen.getByRole("checkbox", {
                name: "No organization spending limit",
            }),
        );
        expect(
            screen.getByRole<HTMLInputElement>("spinbutton", {
                name: "Monthly USD limit",
            }).disabled,
        ).toBe(true);
        fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
        expect(save).toHaveBeenCalledWith(
            "settings",
            expect.objectContaining({
                spend_limit_unlimited: true,
                spend_limit_usd: null,
            }),
        );
    });
});
