import asyncio

import httpx
import pytest

from collaboration_runtime.search import SearchClient, SearchError


def run(coro):
    return asyncio.run(coro)


def test_searxng_parameters_filter_and_limit():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [
            {"url": "javascript:alert(1)", "title": "bad", "content": "bad"},
            {"url": "https://source.example/a", "title": "Title", "content": "Snippet", "engine": "example"},
            {"url": "http://source.example/b", "title": "Second", "content": "Second snippet"},
        ]})

    async def check():
        client = SearchClient("https://search.example/search", api_key="secret-test", transport=httpx.MockTransport(handler))
        try:
            result = await client.search("test & format=xml", 1)
            assert result == [{"url": "https://source.example/a", "title": "Title", "snippet": "Snippet", "engine": "example"}]
            assert seen[0].url.host == "search.example"
            assert seen[0].url.params["q"] == "test & format=xml"
            assert seen[0].url.params["format"] == "json"
            assert seen[0].headers["authorization"] == "Bearer secret-test"
        finally:
            await client.aclose()

    run(check())


@pytest.mark.parametrize("status", [302, 403, 500])
def test_http_failures_do_not_follow_redirect_or_leak_secret(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example/"}, text="secret-test")

    async def check():
        client = SearchClient("https://search.example/search", api_key="secret-test", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(SearchError) as exc:
                await client.search("question", 3)
            assert "secret-test" not in str(exc.value)
            assert len(calls) == 1
        finally:
            await client.aclose()

    run(check())


def test_bad_config_response_and_input(monkeypatch):
    monkeypatch.delenv("COURSE_SEARCH_URL", raising=False)
    assert SearchClient.from_env() is None
    with pytest.raises(SearchError):
        SearchClient("http://remote.example/search")
    with pytest.raises(SearchError):
        SearchClient("https://user:password@search.example/search")

    async def check():
        client = SearchClient("http://127.0.0.1/search", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": {}})))
        try:
            with pytest.raises(SearchError):
                await client.search("test", 1)
            with pytest.raises(SearchError):
                await client.search("test", 11)
            with pytest.raises(SearchError):
                await client.search(" ", 1)
        finally:
            await client.aclose()

    run(check())
