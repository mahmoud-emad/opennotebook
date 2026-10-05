"""What models cost: the AI endpoint's model list, read at most every ten
minutes, and never with a caller waiting once it has been read. OpenRouter
lists a price per token for input and output; a model it does not price
counts as unpriced, never as free."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx2

log = logging.getLogger(__name__)

TTL_SECONDS = 600
# How long a list that could not be read is not asked for again.
RETRY_SECONDS = 60


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
    """The price list, read from the endpoint and kept.

    A list older than `TTL_SECONDS` is still answered with at once, while a
    fresh one is read in the background (stale while it revalidates). Only
    the very first read is waited for. However many callers ask at once,
    one read is in flight; none of them holds a lock while it is. A read
    that fails is not tried again for `RETRY_SECONDS`, so an endpoint that is
    down costs one slow call a minute, not one per estimate."""

    def __init__(self, base_url: str, api_key: str, http: httpx2.AsyncClient | None = None) -> None:
        self._url = base_url.rstrip("/") + "/models"
        self._key = api_key
        self._http = http
        self._prices: dict[str, Price] = {}
        self._read_at = 0.0
        self._failed_at: float | None = None
        self._reading: asyncio.Task[None] | None = None

    async def prices(self) -> dict[str, Price]:
        """The current prices; the last list read when the endpoint fails,
        and none while it has never answered."""
        now = time.monotonic()
        if self._prices and now - self._read_at < TTL_SECONDS:
            return self._prices
        if self._failed_at is not None and now - self._failed_at < RETRY_SECONDS:
            return self._prices
        reading = self._read()
        if not self._prices:
            # Nothing to answer with yet: this one read is waited for, by
            # every caller that asks meanwhile.
            await asyncio.shield(reading)
        return self._prices

    def _read(self) -> asyncio.Task[None]:
        """The read in flight, started when there is none."""
        loop = asyncio.get_running_loop()
        if self._reading is None or self._reading.done() or self._reading.get_loop() is not loop:
            self._reading = loop.create_task(self._fetch())
        return self._reading

    async def _fetch(self) -> None:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            http = self._http or httpx2.AsyncClient(timeout=20)
            try:
                r = await http.get(self._url, headers=headers)
            finally:
                if self._http is None:
                    await http.aclose()
            r.raise_for_status()
            fresh = parse_models(r.json())
        except httpx2.HTTPError, ValueError:
            fresh = {}
        except Exception:
            # A read in the background has nobody to raise to.
            log.exception("the price list could not be read")
            fresh = {}
        if fresh:
            self._prices, self._read_at, self._failed_at = fresh, time.monotonic(), None
        else:
            self._failed_at = time.monotonic()

    async def price(self, model: str) -> Price | None:
        return (await self.prices()).get(model)
