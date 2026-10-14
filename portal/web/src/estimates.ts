import type { State, TokenUsage } from "./types";

export function standardRateEstimate(
    rows: TokenUsage[],
    prices: State["prices"],
) {
    let usd = 0;
    const missing = new Set<string>();
    for (const row of rows) {
        const price = prices.find((p) => p.model === row.model);
        if (!price && row.input_tokens + row.output_tokens > 0)
            missing.add(row.model);
        else if (price)
            usd +=
                (row.input_tokens * price.input_per_million +
                    row.output_tokens * price.output_per_million) /
                1_000_000;
    }
    return { usd, complete: missing.size === 0, missingModels: [...missing] };
}
