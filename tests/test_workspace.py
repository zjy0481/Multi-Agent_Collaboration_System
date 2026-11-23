"""Application-level workspace authorization with in-memory sandbox and search."""
import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import httpx

from collaboration_runtime.sandbox import SandboxError
from collaboration_runtime.search import SearchClient
from collaboration_runtime.teams import LeadDecision, TeamService
from collaboration_runtime.workspace import WorkspaceService


class MemorySandbox:
    def __init__(self):
        self.files = {}
        self.runs = []

    def prepare(self, workspace_id):
        self.files.setdefault(workspace_id, {})

    def write_bytes(self, workspace_id, path, content, *, mode):
        files = self.files[workspace_id]
        if (mode == 'create' and path in files) or (mode == 'update' and path not in files):
            raise SandboxError('invalid file operation')
        files[path] = content

    def read_bytes(self, workspace_id, path):
        try:
            return self.files[workspace_id][path]
        except KeyError:
            raise SandboxError('file not found') from None

    def delete_file(self, workspace_id, path):
        try:
            del self.files[workspace_id][path]
        except KeyError:
            raise SandboxError('file not found') from None

    def run(self, workspace_id, command, *, inputs):
        self.runs.append((workspace_id, command, dict(inputs)))
        return {'exit_code': 0, 'stdout': '', 'stderr': '', 'timed_out': False}


class Model:
    async def complete(self, *_args):
        raise AssertionError('a real model must not be called')


async def bootstrap(_roles):
    return LeadDecision(role_id='lead', reason='coordinates')


def test_workspace_acl_assistant_versions_and_search(tmp_path):
    async def scenario():
        db = sqlite3.connect(tmp_path / 'state.sqlite3')
        db.row_factory = sqlite3.Row
        sandbox = MemorySandbox()
        teams = TeamService(db, Model(), sandbox, bootstrap)
        requests = []

        def search_response(request):
            requests.append(request)
            return httpx.Response(200, json={'results': [
                {'title': 'Example source', 'url': 'https://example.org/article',
                 'content': 'Example excerpt', 'engine': 'fixture'}]})

        search = SearchClient('https://search.example.test/search', transport=httpx.MockTransport(search_response))
        service = WorkspaceService(db, teams, sandbox, tmp_path / 'artifacts', search)
        roster = (Path(__file__).resolve().parents[1] / 'test samples' /
                  'scenario_1_software_engineering 2' / '01_role_initialization.md').read_text(encoding='utf-8')
        a = await teams.create('first', roster, 'First')
        b = await teams.create('second', roster, 'Second')
        def session(team, role):
            return next(m['session_id'] for m in team['members'] if m['role_id'] == role)
        dev, qa, third, outsider = session(a, 'dev'), session(a, 'qa'), session(a, 'release'), session(b, 'qa')
        async def call(actor, tool, **args):
            return await service.execute(actor, tool, json.dumps(args))

        assert (await call(dev, 'create_file', path='report.txt', content='old'))['ok']
        assert not (await call(dev, 'publish_artifact', path='report.txt', reader_session_ids=[outsider]))['ok']
        assert not (await call(outsider, 'read_file', path='report.txt'))['ok']
        old = (await call(dev, 'publish_artifact', path='report.txt', reader_session_ids=[qa]))['result']
        assert (await call(dev, 'update_file', path='report.txt', content='new'))['ok']
        new = (await call(dev, 'publish_artifact', path='report.txt', reader_session_ids=[qa], artifact_id=old['artifact_id']))['result']
        assert (await call(qa, 'read_artifact', version_id=old['version_id']))['result']['content'] == 'old'
        assert (await call(qa, 'read_artifact', version_id=new['version_id']))['result']['content'] == 'new'
        excerpt = (await call(qa, 'read_artifact', version_id=old['version_id'], start_char=1, max_chars=2))['result']
        assert excerpt['content'] == 'ld'
        assert excerpt['text_range'] == {'start_char': 1, 'end_char': 3, 'total_chars': 3}
        for actor in (third, outsider):
            assert not (await call(actor, 'read_artifact', version_id=old['version_id']))['ok']
            before = len(sandbox.runs)
            assert not (await call(actor, 'run_command', command='cat', artifact_version_ids=[old['version_id']]))['ok']
            assert len(sandbox.runs) == before
        assert (await call(qa, 'run_command', command='inspect', artifact_version_ids=[old['version_id']]))['ok']
        assert sandbox.runs[-1][2][old['version_id']].read_bytes() == b'old'

        assistant = uuid4().hex
        await service.register_assistant(parent_session_id=qa, session_id=assistant, scope_id='review-1',
                                         prompt='review old version', version_ids=[old['version_id']],
                                         allowed_tools=['read_artifact', 'run_command'])
        assert (await call(assistant, 'read_artifact', version_id=old['version_id']))['result']['content'] == 'old'
        assert not (await call(assistant, 'read_artifact', version_id=new['version_id']))['ok']
        assert not (await call(assistant, 'run_command', command='inspect', artifact_version_ids=[new['version_id']]))['ok']
        assert not (await call(assistant, 'create_file', path='x', content='x'))['ok']
        assert (await call(assistant, 'run_command', command='inspect', artifact_version_ids=[old['version_id']]))['ok']
        service.revoke_assistant(assistant)
        assert not (await call(assistant, 'read_artifact', version_id=old['version_id']))['ok']

        found = await call(qa, 'search_web', query='topic', limit=1)
        assert found['ok'] and len(requests) == 1
        record = found['result']
        assert record['results'][0] == {'title': 'Example source', 'url': 'https://example.org/article',
                                        'snippet': 'Example excerpt', 'engine': 'fixture', 'result_id': 1}
        reloaded = WorkspaceService(db, teams, sandbox, tmp_path / 'artifacts', search)
        saved = await reloaded.execute(qa, 'get_search_result', json.dumps({'search_id': record['search_id']}))
        assert saved['result'] == record
        assert not (await reloaded.execute(third, 'get_search_result', json.dumps({'search_id': record['search_id']})))['ok']
        await teams.close()
        await search.aclose()
        db.close()

    asyncio.run(scenario())
