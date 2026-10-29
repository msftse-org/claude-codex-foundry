"""Out-of-the-box prices: public catalog, actual-rate settlement and review dates."""

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from claude_foundry.portal.budget_pricing import BudgetPrice, usage_breakdown
from claude_foundry.price_catalog import catalog_prices, load, price_for

ROOT = Path(__file__).resolve().parents[1]


def deployment(name, model, version="1", fmt="Anthropic", sku="GlobalStandard", **properties):
    return {
        "name": name,
        "sku": {"name": sku},
        "properties": {
            "model": {"name": model, "version": version, "format": fmt},
            "provisioningState": "Succeeded",
            "versionUpgradeOption": "NoAutoUpgrade",
            **properties,
        },
    }


def price(**overrides):
    now = datetime.now(UTC)
    value = dict(
        model="claude-sonnet-5-5",
        provider="anthropic",
        deployment_version="2",
        max_input_tokens=1_000_000,
        max_output_tokens=128_000,
        input_ceiling_per_million="4",
        output_ceiling_per_million="10",
        rates={
            "input": "2",
            "cache_write": "2.50",
            "cache_write_1h": "4",
            "cache_read": "0.10",
            "output": "10",
        },
        source_url="https://example.com/price",
        verified_at=(now - timedelta(days=1)).isoformat(),
        valid_until=(now + timedelta(days=89)).isoformat(),
        note="Synthetic test pricing, never production rates",
    )
    value.update(overrides)
    return BudgetPrice.model_validate(value)


def test_anthropic_settlement_charges_each_cache_category_at_its_rate():
    usage = {
        "input_tokens": 1_000,
        "output_tokens": 2_000,
        "cache_read_input_tokens": 100_000,
        "cache_creation_input_tokens": 30_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 20_000,
            "ephemeral_1h_input_tokens": 10_000,
        },
    }
    # 1k*2 + 100k*0.10 + 20k*2.50 + 10k*4 + 2k*10 micro-USD
    assert price().usage_cost("anthropic", usage) == 2_000 + 10_000 + 50_000 + 40_000 + 20_000
    # Legacy ceilings would charge every input token at 4 USD/MTok.
    assert price().cost(131_000, 2_000) == 544_000


def test_cache_writes_without_a_duration_breakdown_use_the_one_hour_rate():
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000}
    assert usage_breakdown("anthropic", usage)["cache_write_1h"] == 1_000
    assert price().usage_cost("anthropic", usage) == 4_000


def test_openai_cached_and_written_input_are_subsets_of_input():
    value = price(
        model="gpt-6-sol",
        provider="openai",
        input_ceiling_per_million="5",
        output_ceiling_per_million="15",
        rates={"input": "2", "cache_read": "0.20", "cache_write": "2.50", "output": "10"},
    )
    usage = {
        "input_tokens": 10_000,
        "output_tokens": 1_000,
        "input_tokens_details": {"cached_tokens": 6_000, "cache_write_tokens": 1_000},
    }
    assert value.usage_cost("openai", usage) == 3_000 * 2 + 6_000 // 5 + 2_500 + 10_000
    with pytest.raises(ValueError):
        value.usage_cost(
            "openai",
            {"input_tokens": 10, "output_tokens": 0, "input_tokens_details": {"cached_tokens": 11}},
        )


def test_long_context_tier_applies_above_the_threshold_including_cache():
    value = price(
        model="claude-haiku-5-5",
        input_ceiling_per_million="1",
        output_ceiling_per_million="2.50",
        rates={"input": "0.10", "cache_read": "0.01", "output": "0.50"},
        long_context={
            "threshold_tokens": 100_000,
            "input": "0.50",
            "cache_read": "0.05",
            "output": "2.50",
        },
    )
    short = {"input_tokens": 1_000, "output_tokens": 1_000, "cache_read_input_tokens": 99_000}
    long = {"input_tokens": 2_000, "output_tokens": 1_000, "cache_read_input_tokens": 99_000}
    assert value.usage_cost("anthropic", short) == 100 + 990 + 500
    assert value.usage_cost("anthropic", long) == 1_000 + 4_950 + 2_500


def test_unlisted_categories_are_charged_conservatively():
    value = price(rates={"input": "2", "output": "10"})
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 1_000,
        "cache_creation_input_tokens": 1_000,
    }
    assert value.usage_cost("anthropic", usage) == 2_000 + 2_000


def test_ceilings_must_cover_every_listed_rate():
    with pytest.raises(ValueError, match="input ceiling"):
        price(input_ceiling_per_million="3")
    with pytest.raises(ValueError, match="output ceiling"):
        price(output_ceiling_per_million="9")


def test_review_date_warns_by_default_and_blocks_only_when_enforced():
    now = datetime.now(UTC)
    overdue = price(
        verified_at=(now - timedelta(days=100)).isoformat(),
        valid_until=(now - timedelta(days=10)).isoformat(),
    )
    assert overdue.review_due()
    request, held, _ = overdue.prepare({"model": overdue.model, "max_tokens": 10})
    assert request["max_tokens"] == 10 and held > 0
    with pytest.raises(ValueError, match="expired"):
        overdue.prepare({"model": overdue.model, "max_tokens": 10}, enforce_review=True)
    with pytest.raises(ValueError, match="within 366 days"):
        price(valid_until=(now + timedelta(days=400)).isoformat())


def test_every_catalog_entry_produces_a_valid_price():
    catalog = load()
    for name, entry in catalog["models"].items():
        fmt = "OpenAI" if entry["provider"] == "openai" else "Anthropic"
        version = (entry.get("versions") or ["1"])[0]
        for sku in ("GlobalStandard", "DataZoneStandard"):
            value, reason = price_for(name, deployment(name, name, version, fmt, sku), catalog)
            assert reason is None, name
            parsed = BudgetPrice.model_validate(value)
            assert parsed.deployment_version == version
            assert "E" not in value["input_ceiling_per_million"]
            assert parsed.valid_until - parsed.verified_at == timedelta(days=catalog["review_days"])


def test_data_zone_deployments_pay_the_documented_premium():
    catalog = load()
    global_price, _ = price_for("s", deployment("s", "claude-sonnet-5-5"), catalog)
    zone_price, _ = price_for(
        "s", deployment("s", "claude-sonnet-5-5", sku="DataZoneStandard"), catalog
    )
    assert Decimal(zone_price["rates"]["input"]) == Decimal(
        global_price["rates"]["input"]
    ) * Decimal("1.1")
    assert "Data Zone 1.1x" in zone_price["note"]


@pytest.mark.parametrize(
    "item, reason",
    [
        (deployment("x", "unknown-model"), "no public price"),
        (deployment("x", "gpt-4o", "2024-05-13", "OpenAI"), "priced differently"),
        (deployment("x", "claude-sonnet-5-5", sku="ProvisionedManaged"), "not covered"),
        (deployment("x", "claude-sonnet-5-5", provisioningState="Creating"), "not succeeded"),
        (
            deployment(
                "x", "claude-sonnet-5-5", versionUpgradeOption="OnceNewDefaultVersionAvailable"
            ),
            "pin the model version",
        ),
    ],
)
def test_unpriceable_deployments_are_skipped_with_a_reason(item, reason):
    value, why = price_for("x", item)
    assert value is None and reason in why


def test_catalog_prices_only_offered_models_with_matching_providers():
    providers = {
        "claude": {"models": [{"name": "claude-sonnet-5-5"}, {"name": "gpt-5"}]},
        "codex": {"models": [{"name": "gpt-5.5"}, {"name": "missing"}]},
    }
    deployed = [
        deployment("claude-sonnet-5-5", "claude-sonnet-5-5", "2"),
        deployment("gpt-5", "gpt-5", "2025-08-07", "OpenAI"),
        deployment("gpt-5.5", "gpt-5.5", "2026-04-24", "OpenAI"),
        deployment("unoffered", "claude-opus-5-5", "2"),
    ]
    prices, skipped = catalog_prices(providers, deployed)
    assert [p["model"] for p in prices] == ["claude-sonnet-5-5", "gpt-5.5"]
    assert skipped == {
        "gpt-5": "catalog provider openai does not match anthropic",
        "missing": "not deployed",
    }
    assert prices[1]["long_context"]["threshold_tokens"] == 272_000


def test_publish_plan_adds_missing_prices_and_replaces_only_on_request():
    spec = importlib.util.spec_from_file_location(
        "publish_prices", ROOT / "scripts/publish_prices.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog_price, _ = price_for(
        "claude-sonnet-5-5", deployment("claude-sonnet-5-5", "claude-sonnet-5-5", "2")
    )
    reviewed = {**catalog_price, "rates": None}
    state = {"budget_prices": [reviewed]}
    assert module.plan(state, [catalog_price], replace=False) == []
    assert module.plan(state, [catalog_price], replace=True) == [catalog_price]
    assert module.plan({"budget_prices": []}, [catalog_price], replace=False) == [catalog_price]
