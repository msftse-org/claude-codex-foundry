import { expect, it } from "vitest";
import {
    breakdownRows,
    change,
    dailySeries,
    filterSearch,
    presetDates,
    readFilters,
    validRange,
    zero,
    type CostResult,
} from "./cost-analysis";

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
