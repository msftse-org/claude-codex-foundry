"""Conservative, versioned spend accounting. No telemetry estimates authorize spend."""

from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from .domain import StrictModel

MICRO = Decimal(1_000_000)
COMPACTION_MIN_OUTPUT_TOKENS = 20_000
# Rates are reviewed snapshots of public list prices. A passed review date is a
# warning unless the owner opts into enforcement; it never silently stops access.
MAX_REVIEW_DAYS = 366
Rate = Annotated[Decimal, Field(ge=0, le=10000, decimal_places=6)]
RATE_FIELDS = ("input", "cache_read", "cache_write", "cache_write_1h", "output")


def micros(value):
    amount = Decimal(str(value)) * MICRO
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise ValueError("Dollar amounts must be nonnegative with at most six decimal places")
    return int(amount)


class Rates(StrictModel):
    """Actual per-category list rates in USD per million tokens.

    Anthropic: input is uncached input; cache_write is the 5-minute write and
    cache_write_1h the one-hour write. OpenAI: input is uncached input, cache_read
    is cached input and cache_write is an explicit cache write (newer models).
    """

    input: Rate
    output: Rate
    cache_read: Rate | None = None
    cache_write: Rate | None = None
    cache_write_1h: Rate | None = None


class LongContextRates(Rates):
    """Rates for a request whose prompt (all input categories) exceeds the threshold."""

    threshold_tokens: int = Field(ge=1, le=10_000_000)


class BudgetPrice(StrictModel):
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    provider: Literal["anthropic", "openai"]
    deployment_version: str = Field(min_length=1, max_length=80)
    max_input_tokens: int = Field(ge=1, le=10_000_000)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    default_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    # Ceilings cover every admitted cache/context category and size reservations.
    input_ceiling_per_million: Decimal = Field(gt=0, le=10000, decimal_places=6)
    output_ceiling_per_million: Decimal = Field(gt=0, le=10000, decimal_places=6)
    # Optional actual list rates. With them, settlement charges what the provider
    # bills per category; without them it charges the ceilings (legacy behavior).
    rates: Rates | None = None
    long_context: LongContextRates | None = None
    source_url: HttpUrl
    verified_at: AwareDatetime
    # The date by which the owner should review this rate ("review by").
    valid_until: AwareDatetime
    note: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def valid(self):
        if self.default_output_tokens > self.max_output_tokens:
            raise ValueError("Default output cannot exceed the verified model maximum")
        if (
            not 0
            < (self.valid_until - self.verified_at).total_seconds()
            <= (MAX_REVIEW_DAYS * 86400)
        ):
            raise ValueError(f"Set a review date within {MAX_REVIEW_DAYS} days of verification")
        if self.long_context and not self.rates:
            raise ValueError("Long-context rates require standard rates")
        for tier in filter(None, (self.rates, self.long_context)):
            inputs = [getattr(tier, k) for k in RATE_FIELDS[:-1] if getattr(tier, k) is not None]
            if max(inputs) > self.input_ceiling_per_million:
                raise ValueError("The input ceiling must cover every input and cache rate")
            if tier.output > self.output_ceiling_per_million:
                raise ValueError("The output ceiling must cover every output rate")
        return self

    def review_due(self, at=None):
        return (at or datetime.now(UTC)) >= self.valid_until

    def check_current(self, at=None, enforce=True):
        at = at or datetime.now(UTC)
        if at < self.verified_at:
            raise ValueError("This model's price verification date is in the future")
        if enforce and self.review_due(at):
            raise ValueError("This model's enforcement price or capacity verification has expired")

    def usage_cost(self, provider, usage):
        """Charge the provider's reported usage at its actual category rates."""
        if self.rates is None:
            return self.cost(*usage_tokens(provider, usage))
        parts = usage_breakdown(provider, usage)
        prompt = (
            parts["input"] + parts["cache_read"] + parts["cache_write"] + parts["cache_write_1h"]
        )
        tier = (
            self.long_context
            if self.long_context and prompt > self.long_context.threshold_tokens
            else self.rates
        )
        listed = [getattr(tier, k) for k in RATE_FIELDS[:-1] if getattr(tier, k) is not None]
        total = Decimal(0)
        for key, tokens in parts.items():
            rate = getattr(tier, key)
            if rate is None:
                # Unlisted categories are charged conservatively, never for free.
                rate = max(listed) if key.startswith("cache_write") else tier.input
            total += tokens * rate
        # USD/million tokens numerically equals micro-USD/token. Round charges up.
        return int(total.to_integral_value(rounding=ROUND_CEILING))

    def cost(self, input_tokens, output_tokens):
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or min(input_tokens, output_tokens) < 0
        ):
            raise ValueError("Invalid provider token usage")
        # USD/million tokens numerically equals micro-USD/token. Round charges up.
        return int(
            (
                input_tokens * self.input_ceiling_per_million
                + output_tokens * self.output_ceiling_per_million
            ).to_integral_value(rounding=ROUND_CEILING)
        )

    def prepare(self, body, enforce_review=False):
        """Reject separately billed tools/tiers and enforce an output bound upstream."""
        self.check_current(enforce=enforce_review)
        if not isinstance(body, dict) or not isinstance(body.get("tools", []), list):
            raise ValueError("Provide a JSON object and a tools array")
        if "stream" in body and type(body["stream"]) is not bool:
            raise ValueError("Stream must be a boolean")
        common = {
            "model",
            "stream",
            "tools",
            "tool_choice",
            "metadata",
            "temperature",
            "top_p",
            "service_tier",
        }
        provider_fields = (
            {
                "messages",
                "system",
                "max_tokens",
                "stop_sequences",
                "top_k",
                "thinking",
                "output_config",
                "cache_control",
                "context_management",
            }
            if self.provider == "anthropic"
            else {
                "input",
                "instructions",
                "max_output_tokens",
                "parallel_tool_calls",
                "reasoning",
                "text",
                "include",
                "store",
                "background",
                "truncation",
                "prompt_cache_key",
                "prompt_cache_retention",
                "safety_identifier",
                "user",
                "stream_options",
                "client_metadata",
            }
        )
        if set(body) - common - provider_fields:
            raise ValueError("This request contains features without verified budget bounds")
        if body.get("model") != self.model:
            raise ValueError("Model does not match its verified enforcement price")
        if body.get("background") or body.get("previous_response_id") or body.get("conversation"):
            raise ValueError("Budgeted requests require replayed, synchronous conversation input")
        if body.get("service_tier") not in (None, "auto", "default", "standard_only") or body.get(
            "speed"
        ) not in (None, "standard"):
            raise ValueError("Premium service tiers are not enabled for this budget policy")
        if any(key in body for key in ("container", "mcp_servers", "skills")):
            raise ValueError(
                "Server-managed tools and compaction require separately verified pricing"
            )
        if "context_management" in body and body["context_management"] != {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
        }:
            raise ValueError("Only thinking preservation is enabled; compaction is not priced")
        for tool in body.get("tools", []):
            children = [tool]
            if isinstance(tool, dict) and tool.get("type") == "namespace":
                if self.provider != "openai" or not isinstance(tool.get("tools"), list):
                    raise ValueError("A tool namespace must contain client tools")
                children = tool["tools"]
            if any(
                not isinstance(child, dict) or child.get("type") not in (None, "function", "custom")
                for child in children
            ):
                raise ValueError("Only client-executed tools are enabled for hard budgets")
        field = "max_tokens" if self.provider == "anthropic" else "max_output_tokens"
        compacting = (
            self.provider == "openai"
            and isinstance(body.get("input"), list)
            and any(
                isinstance(item, dict) and item.get("type") == "compaction_trigger"
                for item in body["input"]
            )
        )
        maximum = body.get(field)
        if maximum is None:
            maximum = max(
                self.default_output_tokens,
                COMPACTION_MIN_OUTPUT_TOKENS if compacting else 1,
            )
        if type(maximum) is not int or not 1 <= maximum <= self.max_output_tokens:
            raise ValueError("Choose an output limit within the verified model maximum")
        if compacting and maximum < COMPACTION_MIN_OUTPUT_TOKENS:
            raise ValueError("Compaction requires max_output_tokens of at least 20000")
        request = {**body, field: maximum}
        request["service_tier"] = "standard_only" if self.provider == "anthropic" else "default"
        request.pop("speed", None)
        if self.provider == "openai":
            request["store"] = False
        # Reserve the model's verified input ceiling. Token counting/byte estimates
        # are not treated as a mathematically safe bound on provider billing.
        return request, self.cost(self.max_input_tokens, maximum), maximum


def usage_tokens(provider, usage):
    if not isinstance(usage, dict):
        raise ValueError("Authoritative provider usage is missing")
    keys = ("input_tokens", "output_tokens")
    values = [usage.get(key) for key in keys]
    if any(type(x) is not int or x < 0 for x in values):
        raise ValueError("Provider input/output usage is incomplete")
    input_tokens, output_tokens = values
    if provider == "anthropic":
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            value = usage.get(key, 0)
            if type(value) is not int or value < 0:
                raise ValueError("Invalid cache usage")
            input_tokens += value
    # OpenAI input includes cached tokens; output includes reasoning. Do not double count.
    return input_tokens, output_tokens


def _count(value, message):
    if type(value) is not int or value < 0:
        raise ValueError(message)
    return value


def usage_breakdown(provider, usage):
    """Split authoritative usage into billed categories without double counting."""
    total_input, output = usage_tokens(provider, usage)
    parts = dict.fromkeys(RATE_FIELDS, 0)
    parts["output"] = output
    if provider == "anthropic":
        parts["input"] = _count(usage["input_tokens"], "Invalid input usage")
        parts["cache_read"] = _count(usage.get("cache_read_input_tokens", 0), "Invalid cache usage")
        written = _count(usage.get("cache_creation_input_tokens", 0), "Invalid cache usage")
        detail = usage.get("cache_creation") or {}
        if not isinstance(detail, dict):
            raise ValueError("Invalid cache usage")
        short = _count(detail.get("ephemeral_5m_input_tokens", 0), "Invalid cache usage")
        long = _count(detail.get("ephemeral_1h_input_tokens", 0), "Invalid cache usage")
        if short + long > written:
            raise ValueError("Invalid cache usage")
        # Writes without a duration breakdown are charged at the one-hour rate.
        parts["cache_write"] = short
        parts["cache_write_1h"] = written - short
    else:
        details = usage.get("input_tokens_details") or {}
        if not isinstance(details, dict):
            raise ValueError("Invalid cached usage")
        parts["cache_read"] = _count(details.get("cached_tokens", 0), "Invalid cached usage")
        parts["cache_write"] = _count(details.get("cache_write_tokens", 0), "Invalid cached usage")
        uncached = total_input - parts["cache_read"] - parts["cache_write"]
        if uncached < 0:
            raise ValueError("Invalid cached usage")
        parts["input"] = uncached
    return parts
