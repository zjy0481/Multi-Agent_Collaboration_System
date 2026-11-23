"""Fixed-endpoint SearXNG JSON search transport."""
from __future__ import annotations

import ipaddress
import json
import os
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from .model import PROJECT_ENV


class SearchError(Exception):
    """A safe, credential-free search configuration or transport error."""


class SearchResults(list):
    def __init__(self, items, degraded=False):
        super().__init__(items)
        self.degraded = degraded


def _valid_url(url: str, *, endpoint: bool) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port
        if not host or parts.username is not None or parts.password is not None:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        if endpoint and (parts.query or parts.fragment):
            return False
        if endpoint and parts.scheme == "http":
            try:
                return ipaddress.ip_address(host).is_loopback
            except ValueError:
                return host.lower() == "localhost"
        return True
    except ValueError:
        return False


class SearchClient:
    def __init__(self, endpoint: str, *, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(endpoint, str) or not _valid_url(endpoint, endpoint=True):
            raise SearchError("Search endpoint must be HTTPS or loopback HTTP without credentials or query")
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._endpoint = endpoint
        self._client = httpx.AsyncClient(headers=headers, transport=transport, timeout=15, follow_redirects=False, trust_env=False)

    @classmethod
    def from_env(cls) -> SearchClient | None:
        try:
            values = dotenv_values(PROJECT_ENV, encoding="utf-8-sig", interpolate=False)
        except (OSError, UnicodeError):
            raise SearchError("Could not read the project .env as UTF-8") from None
        endpoint = (os.environ.get("COURSE_SEARCH_URL", values.get("COURSE_SEARCH_URL")) or "").strip()
        if not endpoint:
            return None
        api_key = os.environ.get("COURSE_SEARCH_API_KEY", values.get("COURSE_SEARCH_API_KEY")) or None
        return cls(endpoint, api_key=api_key)

    async def search(self, query: str, limit: int) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise SearchError("Search query must contain 1 to 1000 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise SearchError("Search limit must be between 1 and 10")
        try:
            async with self._client.stream("GET", self._endpoint, params={"q": query, "format": "json"}) as response:
                if response.status_code != 200:
                    raise SearchError(f"Search provider returned HTTP {response.status_code}")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 1_000_000:
                        raise SearchError("Search provider response too large")
            data = json.loads(chunks)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError
        except SearchError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError):
            raise SearchError("Search provider request or response failed") from None

        results = []
        for item in data["results"]:
            if not isinstance(item, dict):
                continue
            url, title, snippet = item.get("url"), item.get("title"), item.get("content", "")
            if not isinstance(url, str) or len(url) > 4096 or not _valid_url(url, endpoint=False):
                continue
            if not isinstance(title, str) or not isinstance(snippet, str):
                continue
            result = {"url": url, "title": title[:1000], "snippet": snippet[:4000]}
            if isinstance(item.get("engine"), str):
                result["engine"] = item["engine"][:100]
            results.append(result)
            if len(results) == limit:
                break
        return SearchResults(results, bool(data.get("unresponsive_engines")))

    async def aclose(self) -> None:
        await self._client.aclose()
