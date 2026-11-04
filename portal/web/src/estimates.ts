import type { State, TokenUsage } from "./types";

export const metricCount = (value: number) =>
    Number.isFinite(value) ? Math.max(0, value) : 0;

function providerFor(
    model: string,
    price: State["prices"][number] | undefined,
    models: State["models"],
) {
    const metadata = models.find((m) => m.name === model);
    const provider = (
        price?.provider ||
        metadata?.provider ||
        metadata?.format ||
        ""
    ).toLowerCase();
    if (provider === "openai" || provider === "codex") return "openai";
    if (provider === "anthropic" || provider === "claude") return "anthropic";
    return undefined;
}

export function tokenCount(
    row: TokenUsage,
    prices: State["prices"],
    models: State["models"] = [],
) {
    const provider = providerFor(
        row.model,
        prices.find((p) => p.model === row.model),
        models,
    );
    return (
        metricCount(row.input_tokens) +
        metricCount(row.output_tokens) +
        (provider === "openai" ? 0 : metricCount(row.cached_tokens))
    );
}

export function standardRateEstimate(
    rows: TokenUsage[],
    prices: State["prices"],
    models: State["models"] = [],
) {
    let usd = 0;
    const missing = new Set<string>();
    const cacheMissing = new Set<string>();
    const providerMissing = new Set<string>();
    for (const row of rows) {
        const price = prices.find((p) => p.model === row.model);
        const input = metricCount(row.input_tokens);
        const output = metricCount(row.output_tokens);
        const cached = metricCount(row.cached_tokens);
        const provider = providerFor(row.model, price, models);
        if (!price) {
            if (input + output + cached > 0) missing.add(row.model);
            if (cached > 0 && !provider) providerMissing.add(row.model);
            continue;
        }
        if (cached > 0 && price.cache_read_per_million == null)
            cacheMissing.add(row.model);
        if (cached > 0 && !provider) {
            providerMissing.add(row.model);
            continue;
        }
        const pricedCache =
            provider === "openai" ? Math.min(input, cached) : cached;
        usd +=
            ((input - (provider === "openai" ? pricedCache : 0)) *
                price.input_per_million +
                pricedCache *
                    (price.cache_read_per_million ?? price.input_per_million) +
                output * price.output_per_million) /
            1_000_000;
    }
    return {
        usd,
        complete: missing.size === 0 && providerMissing.size === 0,
        missingModels: [...missing],
        cacheRateMissingModels: [...cacheMissing],
        providerMissingModels: [...providerMissing],
    };
}
