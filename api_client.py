import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit

import aiosqlite
import httpx

from database import cache_get, cache_set


logger = logging.getLogger(__name__)
API_BASE = "https://pokeapi.co/api/v2"
USER_AGENT = "PokedexGo/1.0 (Telegram bot; contact via Telegram)"
MAX_RETRY_DELAY_SEC = 5.0


import time

class PokeAPI:
    def __init__(
        self,
        client: httpx.AsyncClient,
        db: aiosqlite.Connection,
        ttl_sec: int,
        max_retries: int = 2,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.client = client
        self.db = db
        self.ttl_sec = ttl_sec
        self.max_retries = max_retries
        self._l1_cache: dict[str, tuple[float, Any]] = {}

    async def get(self, url: str, *, cache_response: bool = True) -> Any:
        if cache_response:
            now = time.time()
            if url in self._l1_cache:
                saved_at, cached_data = self._l1_cache[url]
                if now - saved_at <= self.ttl_sec:
                    return cached_data
                else:
                    self._l1_cache.pop(url, None)

            cached = await cache_get(self.db, url, self.ttl_sec)
            if cached is not None:
                self._l1_cache[url] = (now, cached)
                return cached

        parsed_url = urlsplit(url)
        safe_netloc = parsed_url.netloc.rsplit("@", 1)[-1]
        endpoint = f"{safe_netloc}{parsed_url.path}"
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.get(
                    url,
                    follow_redirects=True,
                    headers={"User-Agent": USER_AGENT},
                )
            except httpx.RequestError as error:
                if attempt < self.max_retries:
                    logger.warning(
                        "Request to %s failed with %s; retrying (%d/%d)",
                        endpoint,
                        type(error).__name__,
                        attempt + 1,
                        self.max_retries,
                    )
                    await asyncio.sleep(self._retry_delay(attempt))
                    continue
                logger.error(
                    "Request to %s failed after retries with %s",
                    endpoint,
                    type(error).__name__,
                )
                return None

            if response.status_code != 200:
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        logger.warning(
                            "Request to %s returned HTTP %d; retrying (%d/%d)",
                            endpoint,
                            response.status_code,
                            attempt + 1,
                            self.max_retries,
                        )
                        await asyncio.sleep(self._retry_delay(
                            attempt, response.headers.get("Retry-After"),
                        ))
                        continue
                logger.warning("Request to %s returned HTTP %d", endpoint, response.status_code)
                return None

            try:
                data = response.json()
            except json.JSONDecodeError:
                if attempt < self.max_retries:
                    logger.warning(
                        "Request to %s returned invalid JSON; retrying (%d/%d)",
                        endpoint,
                        attempt + 1,
                        self.max_retries,
                    )
                    await asyncio.sleep(self._retry_delay(attempt))
                    continue
                logger.error("Request to %s returned invalid JSON after retries", endpoint)
                return None

            if cache_response:
                self._l1_cache[url] = (time.time(), data)
                await cache_set(self.db, url, data)
            return data
        return None

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), MAX_RETRY_DELAY_SEC)
            except ValueError:
                logger.debug("Ignoring non-numeric Retry-After value")
        return min(0.5 * (2 ** attempt), MAX_RETRY_DELAY_SEC)

    async def pokemon(self, name_or_id: str | int) -> dict | None:
        return await self.get(f"{API_BASE}/pokemon/{name_or_id}")

    async def cry_url(self, pid: int | str) -> str | None:
        p = await self.pokemon(pid)
        if isinstance(p, dict):
            cries = p.get("cries")
            if isinstance(cries, dict) and cries.get("latest"):
                return cries.get("latest")
        return f"https://raw.githubusercontent.com/PokeAPI/cries/main/cries/pokemon/latest/{pid}.ogg"

    async def pokemon_names(self) -> list[str] | None:
        data = await self.get(f"{API_BASE}/pokemon?limit=1025")
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return None
        return [
            entry["name"]
            for entry in results
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        ]

    async def species(self, pid: int) -> dict | None:
        return await self.get(f"{API_BASE}/pokemon-species/{pid}")

    async def type(self, type_name: str) -> dict | None:
        return await self.get(f"{API_BASE}/type/{type_name}")

    async def item(self, name: str) -> dict | None:
        return await self.get(f"{API_BASE}/item/{name}")

    async def generation(self, n: str) -> dict | None:
        return await self.get(f"{API_BASE}/generation/{n}")

    async def move(self, name: str) -> dict | None:
        return await self.get(f"{API_BASE}/move/{name}")

    async def ability(self, name: str) -> dict | None:
        return await self.get(f"{API_BASE}/ability/{name}")

    async def nature(self, name: str) -> dict | None:
        return await self.get(f"{API_BASE}/nature/{name}")

    async def stat(self, name: str) -> dict | None:
        return await self.get(f"{API_BASE}/stat/{name}")

    async def berry(self, name: str | int) -> dict | None:
        return await self.get(f"{API_BASE}/berry/{name}")

    async def berry_flavor(self, name: str) -> dict | None:
        return await self.get(f"{API_BASE}/berry-flavor/{name}")

    async def berry_firmness(self, name: str) -> dict | None:
        return await self.get(f"{API_BASE}/berry-firmness/{name}")

    async def berries(self) -> dict | None:
        return await self.get(f"{API_BASE}/berry?limit=1000")

    async def encounters(self, name_or_id: str | int) -> list[dict] | None:
        result = await self.get(f"{API_BASE}/pokemon/{name_or_id}/encounters")
        return result if isinstance(result, list) else None

