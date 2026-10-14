# Public planning-rate reference

Verified **2026-10-14**, USD per million tokens. These are the standard public rates, not customer-specific contracted or invoiced prices. Refresh this reference before using it in a later customer deployment.

[Anthropic's model and Foundry pricing](https://platform.claude.com/docs/en/about-claude/pricing) publishes the rates below. [Microsoft's CCU billing documentation](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-billing) confirms that Foundry converts usage using Anthropic's published rates, with contractual discounts applied separately.

| Model | Base input | Output | 5-minute cache write | 1-hour cache write | Cache read |
| --- | ---: | ---: | ---: | ---: | ---: |
| Claude Opus 5.5 | 4.00 | 20.00 | 5.00 | 8.00 | 0.20 |
| Claude Opus 5 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 |
| Claude Opus 4.6 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 |
| Claude Opus 4.5 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 |
| Claude Sonnet 5.5 | 2.00 | 10.00 | 2.50 | 4.00 | 0.10 |
| Claude Sonnet 5 | 2.00 | 10.00 | 2.50 | 4.00 | 0.20 |
| Claude Sonnet 4.6 | 3.00 | 15.00 | 3.75 | 6.00 | 0.30 |
| Claude Sonnet 4.5 | 3.00 | 15.00 | 3.75 | 6.00 | 0.30 |
| Claude Haiku 4.5 | 1.00 | 5.00 | 1.25 | 2.00 | 0.10 |
| Claude Haiku 5.5, prompt ≤100,000 tokens | 0.10 | 0.50 | 0.125 | 0.20 | 0.01 |
| Claude Haiku 5.5, prompt >100,000 tokens | 0.50 | 2.50 | 0.625 | 1.00 | 0.05 |

**Deployment geography matters.** Foundry US Data Zone Standard adds a 1.1× multiplier to all applicable token categories. For example, Opus 5.5 on this deployment type uses **4.40 input / 22.00 output**. Check each deployed SKU; do not infer its billing geography from the Foundry resource's region. Global Standard uses the base rates.

The portal currently applies one input/output planning rate per model. Cache categories are documented above for reference but are not separately costed by its aggregate-metric estimator. Haiku 5.5's higher prompt-length tier also cannot be inferred from daily aggregated metrics. Source, verification date, and notes can be saved with each model rate and appear in the audit record. Changing a rate recalculates historic estimates; it does not change Azure's pricing.
