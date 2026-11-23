import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.model import ChatCompletionsModel, CourseModel, OpenCodeModel
from test_collaboration import OPERATOR, auth, send, setup_team
from test_scheduler import Sandbox, application, tool, turns, wait_until


@pytest.mark.parametrize('mode', ['course', 'deepseek', 'opencode'])
def test_provider_quota_preserves_queue_and_checkpoint(tmp_path, mode):
    available = threading.Event()
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            message = tool('run_command', 'test-command', {'command': 'first'})
        elif not available.is_set():
            return httpx.Response(402)
        else:
            message = {'role': 'assistant', 'content': 'finished'}
        return httpx.Response(200, json={'choices': [{'message': message}]})

    if mode == 'course':
        model = CourseModel('https://course.example', 'course-model', 'test-key')
    elif mode == 'opencode':
        model = OpenCodeModel('https://course.example', 'deepseek-flash', 'test-key')
    else:
        model = ChatCompletionsModel('https://course.example', 'deepseek-flash', 'test-key', mode=mode)
    asyncio.run(model.aclose())
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    sandbox = Sandbox()
    with TestClient(application(tmp_path, model, sandbox)) as client:
        base, lead, others, tokens = setup_team(client)
        lead_token = tokens[lead['member_id']]
        other = others[0]
        other_token = tokens[other['member_id']]
        operator = auth(OPERATOR)
        send(client, base, OPERATOR, request_id='work-before-quota', text='first turn', mentions=[lead['member_id']])
        paused = wait_until(lambda: next((t for t in turns(client, lead['session_id'], lead_token)
                                          if t['status'] == 'recoverable'), None))
        assert client.get('/v1/model/status', headers=operator).json() == {
            'status': 'paused', 'reason': 'Course model paused after provider HTTP 402',
        }
        send(client, base, OPERATOR, request_id='queued-during-quota', text='second turn', mentions=[other['member_id']])
        assert client.post('/v1/model/resume', headers=auth(other_token)).status_code == 403
        direct = client.post(f"/v1/sessions/{other['session_id']}/turns", headers=auth(other_token), json={'text': 'direct'})
        assert direct.status_code == 503
        assert not turns(client, other['session_id'], other_token)
        assert len(requests) == 2

        available.set()
        assert client.post('/v1/model/resume', headers=operator).json()['status'] == 'ready'
        completed = wait_until(lambda: next((t for t in turns(client, lead['session_id'], lead_token)
                                              if t['status'] == 'completed'), None))
        assert completed['turn_id'] == paused['turn_id']
        assert wait_until(lambda: any(t['status'] == 'completed' for t in turns(client, other['session_id'], other_token)))
        assert len(sandbox.commands) == 1
        assert len(requests) == 4
        assert any(any(m['role'] == 'tool' and m['tool_call_id'] == 'test-command' for m in request['messages'])
                   for request in requests[2:])
