"""Conservative, versioned spend accounting. No telemetry estimates authorize spend."""

from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from .domain import StrictModel

MICRO = Decimal(1_000_000)


def micros(value):
    amount = Decimal(str(value)) * MICRO
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise ValueError("Dollar amounts must be nonnegative with at most six decimal places")
    return int(amount)


class BudgetPrice(StrictModel):
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    provider: Literal["anthropic", "openai"]
    deployment_version: str = Field(min_length=1, max_length=80)
    max_input_tokens: int = Field(ge=1, le=10_000_000)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    default_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    # Owner-reviewed ceilings must cover every admitted cache/context category.
    # Settlement deliberately uses these conservative rates, not guessed cache discounts.
    input_ceiling_per_million: Decimal = Field(gt=0, le=10000, decimal_places=6)
    output_ceiling_per_million: Decimal = Field(gt=0, le=10000, decimal_places=6)
    source_url: HttpUrl
    verified_at: AwareDatetime
    valid_until: AwareDatetime
    note: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def valid(self):
        if self.default_output_tokens > self.max_output_tokens:
            raise ValueError("Default output cannot exceed the verified model maximum")
        if not 0 < (self.valid_until - self.verified_at).total_seconds() <= 31 * 86400:
            raise ValueError("Verify enforcement pricing at least every 31 days")
        return self

    def check_current(self, at=None):
        at = at or datetime.now(UTC)
        if not self.verified_at <= at < self.valid_until:
            raise ValueError("This model's enforcement price or capacity verification has expired")

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

    def prepare(self, body):
        """Reject separately billed tools/tiers and enforce an output bound upstream."""
        self.check_current()
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
        maximum = body.get(field, self.default_output_tokens)
        if maximum is None:
            maximum = self.default_output_tokens
        if type(maximum) is not int or not 1 <= maximum <= self.max_output_tokens:
            raise ValueError("Choose an output limit within the verified model maximum")
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
