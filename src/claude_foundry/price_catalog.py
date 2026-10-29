"""Public list prices that make a fresh deployment usable without hand-entered rates.

The catalog is a dated, sourced snapshot, not an invoice. Prices apply only to
succeeded, version-pinned Global or Data Zone Standard deployments that the
published client profiles offer. Anything else is skipped with a reason and
stays closed until an owner reviews its price.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

CATALOG = Path(__file__).with_name("price_catalog.json")
DEFAULT_OUTPUT_TOKENS = 4096
SIX_PLACES = Decimal("0.000001")


def load(path=CATALOG):
    return json.loads(Path(path).read_text())


def _usd(value):
    """Plain decimal text with at most six places, never scientific notation."""
    return format(Decimal(value).quantize(SIX_PLACES).normalize(), "f")


def _scale(rates, multiplier):
    if not rates:
        return None
    return {
        key: value if key == "threshold_tokens" else _usd(Decimal(value) * multiplier)
        for key, value in rates.items()
    }


def _ceilings(tiers):
    inputs = [
        Decimal(tier[key])
        for tier in tiers
        for key in ("input", "cache_read", "cache_write", "cache_write_1h")
        if tier.get(key) is not None
    ]
    outputs = [Decimal(tier["output"]) for tier in tiers]
    return _usd(max(inputs)), _usd(max(outputs))


def price_for(name, deployment, catalog=None):
    """Return (price, None) for one live deployment, or (None, reason)."""
    catalog = catalog or load()
    properties = deployment.get("properties", {})
    model = properties.get("model", {})
    entry = catalog["models"].get(model.get("name"))
    if not entry:
        return None, "no public price in the catalog"
    version = model.get("version")
    if entry.get("versions") and version not in entry["versions"]:
        return None, f"version {version} is priced differently from the catalog"
    sku = deployment.get("sku", {}).get("name")
    if sku not in ("GlobalStandard", "DataZoneStandard"):
        return None, f"{sku} deployments are not covered by the catalog"
    if properties.get("provisioningState") != "Succeeded":
        return None, "the deployment has not succeeded"
    if properties.get("versionUpgradeOption") != "NoAutoUpgrade":
        return None, "automatic version upgrades are enabled; pin the model version first"
    data_zone = sku == "DataZoneStandard"
    multiplier = Decimal(catalog["data_zone_multiplier"]) if data_zone else Decimal(1)
    rates = _scale(entry["rates"], multiplier)
    long_context = _scale(entry.get("long_context"), multiplier)
    input_ceiling, output_ceiling = _ceilings([t for t in (rates, long_context) if t])
    checked = datetime.fromisoformat(catalog["checked_at"]).replace(tzinfo=UTC)
    note = (
        f"Public list price checked {catalog['checked_at']}"
        + (f", Data Zone {catalog['data_zone_multiplier']}x" if data_zone else ", Global")
        + ". "
        + entry.get("note", catalog["notes"][entry["provider"]])
    )
    price = {
        "model": name,
        "provider": entry["provider"],
        "deployment_version": version,
        "max_input_tokens": entry["max_input_tokens"],
        "max_output_tokens": entry["max_output_tokens"],
        "default_output_tokens": min(DEFAULT_OUTPUT_TOKENS, entry["max_output_tokens"]),
        "input_ceiling_per_million": input_ceiling,
        "output_ceiling_per_million": output_ceiling,
        "rates": rates,
        "source_url": catalog["sources"][entry["provider"]],
        "verified_at": checked.isoformat(),
        "valid_until": (checked + timedelta(days=catalog["review_days"])).isoformat(),
        "note": note[:1000],
    }
    if long_context:
        price["long_context"] = long_context
    return price, None


def catalog_prices(providers, deployed, catalog=None):
    """Price every deployment the client profiles publish; report what was skipped."""
    catalog = catalog or load()
    offered = {m["name"]: client for client, p in providers.items() for m in p["models"]}
    actual = {d["name"]: d for d in deployed}
    prices, skipped = [], {}
    for name in sorted(offered):
        if name not in actual:
            skipped[name] = "not deployed"
            continue
        price, reason = price_for(name, actual[name], catalog)
        expected = "openai" if offered[name] == "codex" else "anthropic"
        if price and price["provider"] != expected:
            price, reason = None, f"catalog provider {price['provider']} does not match {expected}"
        if price:
            prices.append(price)
        else:
            skipped[name] = reason
    return prices, skipped
