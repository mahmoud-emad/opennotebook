"""What models cost: the AI endpoint's model list, read at most every ten
minutes. OpenRouter lists a price per token for input and output; a model it
does not price counts as unpriced, never as free."""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx2

TTL_SECONDS = 600


@dataclass(frozen=True)
class Price:
    input_per_token: float
    output_per_token: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens * self.input_per_token + output_tokens * self.output_per_token


def hint(p: Price) -> str:
    """A price as the settings page shows it: "$1 / $5 per M tokens"."""

    def usd(per_token: float) -> str:
        m = per_token * 1_000_000
        s = f"{m:.0f}" if m >= 10 else f"{m:.2f}" if m >= 0.1 else f"{m:.3f}"
        return "$" + (s.rstrip("0").rstrip(".") if "." in s else s)

    if p.input_per_token == 0 and p.output_per_token == 0:
        return "free"
    return f"{usd(p.input_per_token)} / {usd(p.output_per_token)} per M tokens"


def parse_models(body: Any) -> dict[str, Price]:
    """`{"data": [{"id": …, "pricing": {"prompt": "0.000001", …}}]}` to prices."""
    out: dict[str, Price] = {}
    data = body.get("data") if isinstance(body, dict) else None
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        pricing = m.get("pricing")
        try:
            assert isinstance(pricing, dict)
            out[str(m["id"])] = Price(float(pricing["prompt"]), float(pricing["completion"]))
        except AssertionError, KeyError, TypeError, ValueError:
            continue
    return out


class Catalogue:
    def __init__(self, base_url: str, api_key: str, http: httpx2.AsyncClient | None = None) -> None:
        self._url = base_url.rstrip("/") + "/models"
        self._key = api_key
        self._http = http
        self._prices: dict[str, Price] = {}
        self._read_at = 0.0
        self._lock = asyncio.Lock()

    async def prices(self) -> dict[str, Price]:
        """The current prices; the last list read when the endpoint fails."""
        async with self._lock:
            if time.monotonic() - self._read_at < TTL_SECONDS and self._prices:
                return self._prices
            headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
            try:
                http = self._http or httpx2.AsyncClient(timeout=20)
                r = await http.get(self._url, headers=headers)
                if self._http is None:
                    await http.aclose()
                r.raise_for_status()
                if fresh := parse_models(r.json()):
                    self._prices, self._read_at = fresh, time.monotonic()
            except httpx2.HTTPError, ValueError:
                pass
            return self._prices

    async def price(self, model: str) -> Price | None:
        return (await self.prices()).get(model)
