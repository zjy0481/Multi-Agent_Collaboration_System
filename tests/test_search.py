import asyncio
import json
import os

import httpx
import pytest

from collaboration_runtime.search import EXA_SEARCH_URL, SearchClient, SearchError


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_live_exa_key(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)


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
            assert result.provider == "searxng" and result.fallback_reason is None and not result.degraded
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


def test_bad_config_response_and_input(monkeypatch, tmp_path):
    monkeypatch.setattr("collaboration_runtime.search.PROJECT_ENV", tmp_path / "missing.env")
    monkeypatch.delenv("COURSE_SEARCH_URL", raising=False)
    monkeypatch.delenv("COURSE_SEARCH_API_KEY", raising=False)
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


def test_dotenv_bom_no_interpolation_and_environment_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('COURSE_SEARCH_URL=https://file.example/search\nCOURSE_SEARCH_API_KEY=${LITERAL_SECRET}\n', encoding="utf-8-sig")
    monkeypatch.setattr("collaboration_runtime.search.PROJECT_ENV", env_file)
    monkeypatch.delenv("COURSE_SEARCH_URL", raising=False)
    monkeypatch.delenv("COURSE_SEARCH_API_KEY", raising=False)
    client = SearchClient.from_env()
    assert client._endpoint == "https://file.example/search"
    assert client._searxng_headers["Authorization"] == "Bearer ${LITERAL_SECRET}"
    assert "COURSE_SEARCH_URL" not in os.environ
    assert "COURSE_SEARCH_API_KEY" not in os.environ
    run(client.aclose())
    monkeypatch.setenv("COURSE_SEARCH_URL", "https://override.example/search")
    monkeypatch.setenv("COURSE_SEARCH_API_KEY", "environment-secret")
    client = SearchClient.from_env()
    assert client._endpoint == "https://override.example/search"
    assert client._searxng_headers["Authorization"] == "Bearer environment-secret"
    run(client.aclose())
    monkeypatch.setenv("COURSE_SEARCH_URL", "")
    assert SearchClient.from_env() is None


def test_dotenv_load_and_provider_auth_errors_do_not_fallback(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr("collaboration_runtime.search.PROJECT_ENV", env_file)
    monkeypatch.delenv("COURSE_SEARCH_URL", raising=False)
    monkeypatch.delenv("COURSE_SEARCH_API_KEY", raising=False)
    env_file.write_bytes(b"\xff")
    with pytest.raises(SearchError, match="UTF-8"):
        SearchClient.from_env()
    env_file.write_text("COURSE_SEARCH_URL=https://search.example/search\nCOURSE_SEARCH_API_KEY=secret-test\n", encoding="utf-8")

    async def check():
        client = SearchClient.from_env()
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="secret-test")))
        try:
            with pytest.raises(SearchError, match="401") as error:
                await client.search("test", 1)
            assert "secret-test" not in str(error.value)
        finally:
            await client.aclose()
    run(check())


@pytest.mark.parametrize(("process_key", "expected_key"), [(None, "file-test"), (" exa-test ", "exa-test")])
def test_exa_config_precedence_and_highlights(tmp_path, monkeypatch, process_key, expected_key):
    env_file = tmp_path / ".env"
    env_file.write_text("EXA_API_KEY=' file-test '\n", encoding="utf-8-sig")
    monkeypatch.setattr("collaboration_runtime.search.PROJECT_ENV", env_file)
    if process_key is not None:
        monkeypatch.setenv("EXA_API_KEY", process_key)
    monkeypatch.setenv("COURSE_SEARCH_URL", "http://127.0.0.1:8888/search")
    monkeypatch.setenv("COURSE_SEARCH_API_KEY", "fallback-test")
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == EXA_SEARCH_URL and request.method == "POST"
        assert request.headers["x-api-key"] == expected_key
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {"query": "official source", "numResults": 1,
                                               "contents": {"highlights": True}}
        return httpx.Response(200, json={"results": [
            {"url": "file:///tmp/private", "title": "invalid"},
            {"url": "https://example.org/source", "title": "Source", "highlights": ["One", "Two"]},
            {"url": "https://example.org/extra", "title": "Extra"},
        ]})

    async def check():
        client = SearchClient.from_env()
        assert os.environ.get("EXA_API_KEY") == process_key
        await client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await client.search("official source", 1)
            assert result == [{"url": "https://example.org/source", "title": "Source",
                               "snippet": "One\nTwo", "engine": "exa"}]
            assert result.provider == "exa" and result.fallback_reason is None and not result.degraded
            assert len(calls) == 1
        finally:
            await client.aclose()

    run(check())


@pytest.mark.parametrize(("failure", "reason"), [
    (401, "Exa returned HTTP 401"), (402, "Exa returned HTTP 402"), (429, "Exa returned HTTP 429"),
    (503, "Exa returned HTTP 503"), ("timeout", "Exa request timed out"),
    ("invalid", "Exa request or response failed"), ("empty", "Exa returned no usable results"),
])
def test_exa_fallback_logs_safe_reason_and_isolates_credentials(failure, reason):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "api.exa.ai":
            assert request.headers["x-api-key"] == "exa-test"
            assert "authorization" not in request.headers
            if failure == "timeout":
                raise httpx.ReadTimeout("exa-test", request=request)
            if failure == "invalid":
                return httpx.Response(200, text="exa-test")
            if failure == "empty":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(failure, text="exa-test")
        assert request.method == "GET" and request.url.params["q"] == "question"
        assert request.url.host == "127.0.0.1"
        assert "x-api-key" not in request.headers
        assert request.headers["authorization"] == "Bearer fallback-test"
        return httpx.Response(200, json={"results": [{"url": "https://example.org", "title": "Fallback"}]})

    async def check():
        client = SearchClient("http://127.0.0.1:8888/search", api_key="fallback-test", exa_api_key="exa-test",
                              transport=httpx.MockTransport(handler))
        try:
            result = await client.search("question", 1)
            assert result[0]["title"] == "Fallback"
            assert result.provider == "searxng" and result.fallback_reason == reason and result.degraded
            assert len(calls) == 2
        finally:
            await client.aclose()

    run(check())


def test_exa_only_failure_and_dual_failure_are_safe():
    def handler(request):
        return httpx.Response(429 if request.url.host == "api.exa.ai" else 503, text="exa-test")

    async def check(endpoint, message):
        client = SearchClient(endpoint, exa_api_key="exa-test", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(SearchError) as error:
                await client.search("question", 1)
            assert str(error.value) == message
        finally:
            await client.aclose()

    run(check(None, "Exa returned HTTP 429"))
    run(check("http://127.0.0.1:8888/search", "Exa returned HTTP 429. SearXNG returned HTTP 503"))
