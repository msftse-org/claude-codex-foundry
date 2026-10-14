import { expect, it } from "vitest";
import { standardRateEstimate } from "./estimates";
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
    });
});
it("uses supplied rates and does not subtract cache tokens without a cache rate", () => {
    expect(
        standardRateEstimate(
            [row],
            [{ model: "sonnet", input_per_million: 3, output_per_million: 15 }],
        ),
    ).toEqual({ usd: 10.5, complete: true, missingModels: [] });
});
