import asyncio
import json
import socket
from functools import partial

import httpx
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.search import SearchClient
from collaboration_runtime.source_fetch import SourceHTTPError, SourceReadError, fetch_public_source
from test_collaboration import Model, Sandbox, bootstrap, setup_team, OPERATOR


def test_degraded_source_and_exact_session_provenance(tmp_path, monkeypatch):
    def handler(request):
        if request.url.host == 'api.exa.ai':
            return httpx.Response(429)
        return httpx.Response(200, json={
            'results': [{'url': 'https://public.example/paper', 'title': 'Paper', 'content': 'snippet'}],
            'unresponsive_engines': [['brave', 'too many requests']],
        })

    search = SearchClient('https://search.example/search', exa_api_key='test', transport=httpx.MockTransport(handler))
    monkeypatch.setattr('collaboration_runtime.workspace.fetch_public_source', lambda url: {
        'url': url, 'content_type': 'text/plain', 'text': 'original source', 'truncated': False, 'sha256': 'a' * 64,
    })
    application = create_app(model=Model(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                             operator_token=OPERATOR, schedule=False, search=search)
    with TestClient(application) as client:
        _, lead, others, _ = setup_team(client)
        async def execute(session, name, args):
            return await application.state.workspace.execute(session, name, json.dumps(args))
        result = client.portal.call(execute, lead['session_id'], 'search_web', {'query': 'paper'})
        assert result['result']['evidence_kind'] == 'search_excerpt'
        assert result['result']['upstream_status'] == 'degraded'
        assert result['result']['provider'] == 'searxng'
        assert result['result']['fallback_reason'] == 'Exa returned HTTP 429'
        search_id = result['result']['search_id']
        assert result['result']['results'][0]['result_id'] == 1
        saved = client.portal.call(execute, lead['session_id'], 'get_search_result', {'search_id': search_id})
        assert saved == result
        selected = client.portal.call(execute, lead['session_id'], 'read_source',
                                      {'search_id': search_id, 'result_id': 1, 'max_chars': 8})
        assert selected['result']['url'] == 'https://public.example/paper'
        assert selected['result']['evidence_kind'] == 'source_text'
        assert selected['result']['text'] == 'original'
        assert selected['result']['text_range'] == {'start_char': 0, 'end_char': 8, 'total_chars': 15}
        full = client.portal.call(execute, lead['session_id'], 'get_source', {'source_id': selected['result']['source_id']})
        assert full['result']['text'] == 'original source'
        tail = client.portal.call(execute, lead['session_id'], 'get_source',
                                  {'source_id': selected['result']['source_id'], 'start_char': 9, 'max_chars': 6})
        assert tail['result']['text'] == 'source'
        assert tail['result']['sha256'] == full['result']['sha256']
        for invalid in (0, 2):
            failed = client.portal.call(execute, lead['session_id'], 'read_source', {'search_id': search_id, 'result_id': invalid})
            assert not failed['ok'] and 'result_id' in failed['error']
        both = client.portal.call(execute, lead['session_id'], 'read_source', {'search_id': search_id, 'result_id': 1, 'url': 'https://public.example/paper'})
        assert not both['ok']
        cross_scope = client.portal.call(execute, others[0]['session_id'], 'read_source', {'search_id': search_id, 'result_id': 1})
        assert not cross_scope['ok']
        denied = client.portal.call(execute, lead['session_id'], 'read_source', {'search_id': search_id, 'url': 'https://public.example/other'})
        assert not denied['ok'] and denied['code'] == 'source_result_mismatch'
        assert 'get_search_result' in denied['next_action']
        source = client.portal.call(execute, lead['session_id'], 'read_source', {'search_id': search_id, 'url': 'https://public.example/paper'})
        assert source['result']['text'] == 'original source'
        record = client.portal.call(execute, lead['session_id'], 'get_source', {'source_id': source['result']['source_id']})
        assert record == source
        outsider = client.portal.call(execute, others[0]['session_id'], 'get_source', {'source_id': source['result']['source_id']})
        assert not outsider['ok']

        def denied_source(url):
            raise SourceHTTPError(403)

        monkeypatch.setattr('collaboration_runtime.workspace.fetch_public_source', denied_source)
        rejected = client.portal.call(execute, lead['session_id'], 'read_source',
                                      {'search_id': search_id, 'result_id': 1})
        assert not rejected['ok'] and rejected['status'] == 403
        assert rejected['code'] == 'source_http_status'
        assert rejected['error'] == 'Source returned HTTP 403'
        assert 'different accessible public source' in rejected['next_action']
        assert 'result' not in rejected

        monkeypatch.setattr('collaboration_runtime.workspace.fetch_public_source', fetch_public_source)
        monkeypatch.setattr(socket, 'getaddrinfo',
                            lambda *a, **k: [(None, None, None, None, ('198.18.0.42', 443))])
        rejected = client.portal.call(execute, lead['session_id'], 'read_source',
                                      {'search_id': search_id, 'result_id': 1})
        assert not rejected['ok'] and rejected['code'] == 'source_non_public_address'
        assert 'operator' in rejected['next_action'] and 'not source text' in rejected['next_action']
        assert 'result' not in rejected and '198.18.0.42' not in json.dumps(rejected)
        assert client.portal.call(lambda: application.state.workspace.db.execute(
            'SELECT count(*) FROM workspace_sources').fetchone()[0]) == 2


def test_private_resolved_addresses_are_rejected(monkeypatch):
    import pytest
    from collaboration_runtime.search import SearchError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(None, None, None, None, ('127.0.0.1', 443))])
    with pytest.raises(SearchError, match='non-public'):
        fetch_public_source('https://example.test/private')


def test_dns_failure_is_distinct_from_non_public_address(monkeypatch):
    import pytest

    def unresolved(*_args, **_kwargs):
        raise socket.gaierror('system resolver diagnostic')

    monkeypatch.setattr(socket, 'getaddrinfo', unresolved)
    with pytest.raises(SourceReadError, match='Source DNS lookup failed') as caught:
        fetch_public_source('https://example.test/')
    assert caught.value.code == 'source_dns_failed'


def test_redirects_are_pinned_and_recorded(monkeypatch):
    import hashlib
    import collaboration_runtime.source_fetch as fetch
    calls = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **k: [(None, None, None, None, ('8.8.8.8', 443))])
    class Response:
        def __init__(self, status, headers, body=b''):
            self.status, self.headers, self.body = status, headers, body
        def getheader(self, name, default=None):
            return self.headers.get(name, default)
        def read(self, limit):
            return self.body[:limit]
    responses = iter([Response(302, {'Location': 'https://other.example/final'}),
                      Response(200, {'Content-Type': 'text/plain'}, b'evidence')])
    class Connection:
        def __init__(self, host, port, address):
            calls.append((host, address))
        def request(self, *args, **kwargs): pass
        def getresponse(self): return next(responses)
        def close(self): pass
    monkeypatch.setattr(fetch, 'PinnedHTTPS', Connection)
    result = fetch_public_source('https://public.example/start')
    assert calls == [('public.example', '8.8.8.8'), ('other.example', '8.8.8.8')]
    assert result['redirect_chain'] == ['https://public.example/start', 'https://other.example/final']
    assert result['final_url'] == 'https://other.example/final'
    assert result['sha256'] == hashlib.sha256(b'evidence').hexdigest()


def test_redirect_to_private_address_is_rejected(monkeypatch):
    import pytest
    import collaboration_runtime.source_fetch as fetch
    from collaboration_runtime.search import SearchError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **k: [(None, None, None, None,
        ('127.0.0.1' if host == 'private.example' else '8.8.8.8', 443))])
    class Response:
        status = 302
        def getheader(self, name, default=None): return 'https://private.example/'
    class Connection:
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(fetch, 'PinnedHTTPS', Connection)
    with pytest.raises(SearchError, match='non-public'):
        fetch_public_source('https://public.example/')


def test_source_http_403_is_reported(monkeypatch):
    import pytest
    import collaboration_runtime.source_fetch as fetch
    from collaboration_runtime.search import SearchError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('8.8.8.8', 443))])
    class Response:
        status = 403
    class Connection:
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(fetch, 'PinnedHTTPS', Connection)
    with pytest.raises(SearchError, match='HTTP 403'):
        fetch_public_source('https://public.example/')


def test_redirect_loop_and_limit(monkeypatch):
    import pytest
    import collaboration_runtime.source_fetch as fetch
    from collaboration_runtime.search import SearchError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('8.8.8.8', 443))])
    class Response:
        status = 302
        def __init__(self, location): self.location = location
        def getheader(self, name, default=None): return self.location
    class Connection:
        locations = iter(())
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response(next(self.locations))
        def close(self): pass
    monkeypatch.setattr(fetch, 'PinnedHTTPS', Connection)
    Connection.locations = iter(['/next', '/start'])
    with pytest.raises(SearchError, match='loop'):
        fetch_public_source('https://public.example/start')
    Connection.locations = iter(f'/{i}' for i in range(6))
    with pytest.raises(SearchError, match='limit'):
        fetch_public_source('https://public.example/start')
