"""Estimated model cost, from a price table kept outside the code.

Prices change, and differ by provider, tier and date; a number baked into the
code would be wrong silently. ``bench/pricing.yaml`` holds them, each with the
source it was read from and when. A model with no entry is reported as
unpriced (``None``), never as free.

Every figure here is an estimate for comparing strategies, not billing data.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

PRICING_PATH = Path("bench/pricing.yaml")
MTOK = Decimal(1_000_000)


class ModelPrice(BaseModel):
    """US dollars per million tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input: Decimal
    output: Decimal
    """Thinking tokens are billed as output by both providers priced here."""
    cache_read: Decimal | None = None
    """None: cached input is billed as ordinary input."""
    cache_write: Decimal | None = None
    """Writing the prompt cache (Claude). None: billed as ordinary input."""
    aliases: list[str] = Field(default_factory=list)
    source: str = ""
    as_of: str = ""


class PriceTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str = "USD"
    providers: dict[str, dict[str, ModelPrice]] = Field(default_factory=dict)

    def price(self, provider: str | None, model: str | None) -> ModelPrice | None:
        if not provider or not model:
            return None
        models = self.providers.get(provider, {})
        if model in models:
            return models[model]
        return next((p for p in models.values() if model in p.aliases), None)

    def cost(
        self,
        provider: str | None,
        model: str | None,
        *,
        input_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> Decimal | None:
        """``input_tokens`` is every prompt token, cached or not (see
        ``cua.benchmark.baseline_runner.normalize_usage``); the cached and
        cache-written ones are charged at their own rates when there are any."""
        if provider == "scripted":
            return Decimal(0)
        price = self.price(provider, model)
        if price is None:
            return None
        read = min(cache_read_tokens, input_tokens) if price.cache_read is not None else 0
        write = min(cache_write_tokens, input_tokens - read) if price.cache_write is not None else 0
        total = (
            Decimal(input_tokens - read - write) * price.input
            + Decimal(read) * (price.cache_read or Decimal(0))
            + Decimal(write) * (price.cache_write or Decimal(0))
            + Decimal(output_tokens + thinking_tokens) * price.output
        ) / MTOK
        return total.quantize(Decimal("0.000001"))


def load_prices(path: Path = PRICING_PATH) -> PriceTable:
    if not path.is_file():
        return PriceTable()
    return PriceTable.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
