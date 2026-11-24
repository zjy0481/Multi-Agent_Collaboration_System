"""Host-side Exa search with an optional SearXNG fallback."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from .model import PROJECT_ENV

EXA_SEARCH_URL = "https://api.exa.ai/search"


class SearchError(Exception):
    """A safe, credential-free search configuration or transport error."""


class SearchResults(list):
    def __init__(self, items, *, provider: str, degraded: bool = False):
        super().__init__(items)
        self.provider = provider
        self.degraded = degraded
        self.fallback_reason: str | None = None


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
    def __init__(self, endpoint: str | None = None, *, api_key: str | None = None,
                 exa_api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        if endpoint is not None and (not isinstance(endpoint, str) or not _valid_url(endpoint, endpoint=True)):
            raise SearchError("Search endpoint must be HTTPS or loopback HTTP without credentials or query")
        if endpoint is None and not exa_api_key:
            raise SearchError("Configure EXA_API_KEY or COURSE_SEARCH_URL on the host")
        self._endpoint = endpoint
        self._searxng_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._exa_headers = {"x-api-key": exa_api_key} if exa_api_key else None
        self._client = httpx.AsyncClient(headers={"Accept": "application/json"}, transport=transport,
                                         follow_redirects=False, trust_env=False)

    @classmethod
    def from_env(cls) -> SearchClient | None:
        try:
            values = dotenv_values(PROJECT_ENV, encoding="utf-8-sig", interpolate=False)
        except (OSError, UnicodeError):
            raise SearchError("Could not read the project .env as UTF-8") from None
        endpoint = (os.environ.get("COURSE_SEARCH_URL", values.get("COURSE_SEARCH_URL")) or "").strip() or None
        exa_api_key = (os.environ.get("EXA_API_KEY", values.get("EXA_API_KEY")) or "").strip() or None
        if not endpoint and not exa_api_key:
            return None
        api_key = os.environ.get("COURSE_SEARCH_API_KEY", values.get("COURSE_SEARCH_API_KEY")) or None
        return cls(endpoint, api_key=api_key, exa_api_key=exa_api_key)

    async def search(self, query: str, limit: int) -> SearchResults:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise SearchError("Search query must contain 1 to 1000 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise SearchError("Search limit must be between 1 and 10")

        fallback_reason = None
        if self._exa_headers is not None:
            try:
                data = await self._request("Exa", "POST", EXA_SEARCH_URL, 10, headers=self._exa_headers,
                                           json={"query": query, "numResults": limit,
                                                 "contents": {"highlights": True}})
                results = self._results(data, limit, provider="exa")
                if results or self._endpoint is None:
                    return results
                fallback_reason = "Exa returned no usable results"
            except SearchError as exc:
                if self._endpoint is None:
                    raise
                fallback_reason = str(exc)

        assert self._endpoint is not None
        try:
            data = await self._request("SearXNG", "GET", self._endpoint, 15, headers=self._searxng_headers,
                                       params={"q": query, "format": "json"})
        except SearchError as exc:
            if fallback_reason is not None:
                raise SearchError(f"{fallback_reason}. {exc}") from None
            raise
        results = self._results(data, limit, provider="searxng")
        if fallback_reason is not None:
            results.degraded = True
            results.fallback_reason = fallback_reason
        return results

    async def _request(self, provider: str, method: str, url: str, timeout: float, **options) -> dict:
        try:
            async with asyncio.timeout(timeout):
                async with self._client.stream(method, url, timeout=timeout, **options) as response:
                    if response.status_code != 200:
                        raise SearchError(f"{provider} returned HTTP {response.status_code}")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 1_000_000:
                            raise SearchError(f"{provider} response too large")
            data = json.loads(chunks)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError
        except (TimeoutError, httpx.TimeoutException):
            raise SearchError(f"{provider} request timed out") from None
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError):
            raise SearchError(f"{provider} request or response failed") from None
        return data

    @staticmethod
    def _results(data: dict, limit: int, *, provider: str) -> SearchResults:
        results = []
        for item in data["results"]:
            if not isinstance(item, dict):
                continue
            url, title = item.get("url"), item.get("title")
            if not isinstance(url, str) or len(url) > 4096 or not _valid_url(url, endpoint=False):
                continue
            if provider == "exa":
                highlights = item.get("highlights")
                snippet = "\n".join(part for part in highlights if isinstance(part, str)) if isinstance(highlights, list) else ""
                title = title if isinstance(title, str) and title.strip() else url
                engine = "exa"
            else:
                snippet = item.get("content", "")
                engine = item.get("engine")
            if not isinstance(title, str) or not isinstance(snippet, str):
                continue
            result = {"url": url, "title": title[:1000], "snippet": snippet[:4000]}
            if isinstance(engine, str):
                result["engine"] = engine[:100]
            results.append(result)
            if len(results) == limit:
                break
        return SearchResults(results, provider=provider,
                             degraded=provider == "searxng" and bool(data.get("unresponsive_engines")))

    async def aclose(self) -> None:
        await self._client.aclose()
