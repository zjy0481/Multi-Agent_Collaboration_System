import asyncio
import json
import socket
from functools import partial

import httpx
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.search import SearchClient
from collaboration_runtime.source_fetch import fetch_public_source
from test_collaboration import Model, Sandbox, bootstrap, setup_team, OPERATOR


def test_degraded_source_and_exact_session_provenance(tmp_path, monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'results': [{'url': 'https://public.example/paper', 'title': 'Paper', 'content': 'snippet'}],
        'unresponsive_engines': [['brave', 'too many requests']],
    }))
    search = SearchClient('https://search.example/search', transport=transport)
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
        assert result['result']['upstream_status'] == 'degraded'
        search_id = result['result']['search_id']
        saved = client.portal.call(execute, lead['session_id'], 'get_search_result', {'search_id': search_id})
        assert saved['result']['upstream_status'] == 'degraded'
        denied = client.portal.call(execute, lead['session_id'], 'read_source', {'search_id': search_id, 'url': 'https://public.example/other'})
        assert not denied['ok']
        source = client.portal.call(execute, lead['session_id'], 'read_source', {'search_id': search_id, 'url': 'https://public.example/paper'})
        assert source['result']['text'] == 'original source'
        record = client.portal.call(execute, lead['session_id'], 'get_source', {'source_id': source['result']['source_id']})
        assert record == source
        outsider = client.portal.call(execute, others[0]['session_id'], 'get_source', {'source_id': source['result']['source_id']})
        assert not outsider['ok']


def test_private_resolved_addresses_are_rejected(monkeypatch):
    import pytest
    from collaboration_runtime.search import SearchError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(None, None, None, None, ('127.0.0.1', 443))])
    with pytest.raises(SearchError, match='non-public'):
        fetch_public_source('https://example.test/private')
