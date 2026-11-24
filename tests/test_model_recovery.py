import asyncio

import httpx
import pytest

from collaboration_runtime.model import CourseModel, ModelError


def run_with_responses(responses):
    async def run():
        requests = []

        async def handler(request):
            requests.append(request.read())
            response = responses[len(requests) - 1]
            if isinstance(response, Exception):
                raise response
            if response == 200:
                return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]})
            return httpx.Response(response)

        model = CourseModel('https://course.example/v1', 'example', 'secret')
        await model._client.aclose()
        model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await model.complete([{'role': 'user', 'content': 'hello'}], [])
            return result, requests
        finally:
            await model.aclose()

    return asyncio.run(run())


def test_timeout_and_temporary_http_retry_the_same_model_request(monkeypatch):
    async def no_delay(seconds):
        pass
    monkeypatch.setattr('collaboration_runtime.model.asyncio.sleep', no_delay)
    result, requests = run_with_responses([httpx.ReadTimeout('unavailable'), 503, 200])
    assert result['content'] == 'done'
    assert len(requests) == 3 and len(set(requests)) == 1


def test_permanent_http_failure_is_not_retried(monkeypatch):
    async def no_delay(seconds):
        pass
    monkeypatch.setattr('collaboration_runtime.model.asyncio.sleep', no_delay)
    with pytest.raises(ModelError, match='HTTP 401'):
        run_with_responses([401])


def test_retry_exhaustion_reports_last_failure(monkeypatch):
    async def no_delay(seconds):
        pass
    monkeypatch.setattr('collaboration_runtime.model.asyncio.sleep', no_delay)
    with pytest.raises(ModelError, match='timed out'):
        run_with_responses([httpx.ReadTimeout('x'), httpx.ReadTimeout('x'), httpx.ReadTimeout('x')])


def test_scheduler_retries_model_after_tool_without_running_tool_twice(tmp_path, monkeypatch):
    import json
    import sys
    from pathlib import Path
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(Path(__file__).parent))
    from test_collaboration import OPERATOR, send, setup_team
    from test_scheduler import Sandbox, application, turns, wait_until

    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'single-command', 'type': 'function', 'function': {
                    'name': 'run_command', 'arguments': json.dumps({'command': 'first'})}}]}
        elif len(calls) == 2:
            raise httpx.ReadTimeout('timeout', request=request)
        else:
            message = {'role': 'assistant', 'content': 'done'}
        return httpx.Response(200, json={'choices': [{'message': message}]})

    model = CourseModel('https://course.example/v1', 'example', 'secret')
    asyncio.run(model._client.aclose())
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sandbox = Sandbox()
    with TestClient(application(tmp_path, model, sandbox)) as client:
        base, lead, _, tokens = setup_team(client)
        send(client, base, OPERATOR, request_id='retry-tool', text='execute', mentions=[lead['member_id']])
        completed = wait_until(lambda: [t for t in turns(client, lead['session_id'], tokens[lead['member_id']])
                                        if t['status'] == 'completed'])
        assert completed
        assert len(sandbox.commands) == 1
        assert len(calls) == 3 and calls[1] == calls[2]
        assert any(message.get('role') == 'tool' for message in calls[2]['messages'])



def test_operator_retries_exhausted_model_turn_from_checkpoint(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from test_collaboration import OPERATOR, auth, send, setup_team
    from test_scheduler import Sandbox, application, turns, wait_until

    calls = []
    failing = [True]

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'single-command', 'type': 'function', 'function': {
                    'name': 'run_command', 'arguments': json.dumps({'command': 'first'})}}]}
        elif failing[0]:
            return httpx.Response(503)
        else:
            message = {'role': 'assistant', 'content': 'recovered'}
        return httpx.Response(200, json={'choices': [{'message': message}]})

    model = CourseModel('https://course.example/v1', 'example', 'secret')
    asyncio.run(model._client.aclose())
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sandbox = Sandbox()
    with TestClient(application(tmp_path, model, sandbox)) as client:
        base, lead, others, tokens = setup_team(client)
        send(client, base, OPERATOR, request_id='retry-failed', text='execute', mentions=[lead['member_id']])
        failed = wait_until(lambda: next((t for t in turns(client, lead['session_id'], tokens[lead['member_id']])
                                         if t['status'] == 'failed'), None), timeout=10)
        endpoint = f"/v1/sessions/{lead['session_id']}/turns/{failed['turn_id']}/retry"
        assert client.post(endpoint, headers=auth(tokens[others[0]['member_id']])).status_code == 403
        failing[0] = False
        assert client.post(endpoint, headers=auth(OPERATOR)).status_code == 202
        completed = wait_until(lambda: next((t for t in turns(client, lead['session_id'], tokens[lead['member_id']])
                                            if t['status'] == 'completed'), None), timeout=10)
        assert completed['turn_id'] == failed['turn_id']
        assert len(sandbox.commands) == 1
        assert calls[-1] == calls[-2]
        assert client.post(endpoint, headers=auth(OPERATOR)).status_code == 202


def test_retry_rejects_delivery_when_its_goal_has_ended(tmp_path):
    from fastapi.testclient import TestClient
    from collaboration_runtime.api import create_app
    from collaboration_runtime.model import ModelUnavailable
    from collaboration_runtime.scheduler import DeliveryScheduler
    from test_collaboration import OPERATOR, Sandbox, auth, bootstrap, setup_team
    from test_goal_lifecycle import assignment, goal, terminate

    class Unavailable:
        async def complete(self, messages, tools):
            raise ModelUnavailable('Course API request timed out')

    app = create_app(model=Unavailable(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                     operator_token=OPERATOR, schedule=False)
    with TestClient(app) as client:
        base, lead, _, tokens = setup_team(client)
        source = assignment(client, base, 'An assignment')
        registered = goal(client, base, tokens[lead['member_id']], source)
        scheduler = DeliveryScheduler(app.state.runtime, app.state.collaboration, app.state.workspace)

        async def fail():
            job = scheduler._claim_one()
            app.state.runtime.launch(*job)
            await app.state.runtime.active[job[0]].task
            return job[1]

        turn_id = client.portal.call(fail)
        assert terminate(client, base, OPERATOR, registered['goal_id'], 1, 'cancelled').status_code == 201
        endpoint = f"/v1/sessions/{lead['session_id']}/turns/{turn_id}/retry"
        assert client.post(endpoint, headers=auth(OPERATOR)).status_code == 409


@pytest.mark.parametrize('obstacle', ['another_recoverable', 'old_generation'])
def test_retry_rejects_unsafe_recovery(tmp_path, obstacle):
    from fastapi.testclient import TestClient
    from collaboration_runtime.api import create_app
    from collaboration_runtime.model import ModelUnavailable
    from collaboration_runtime.scheduler import DeliveryScheduler
    from test_collaboration import OPERATOR, Sandbox, auth, bootstrap, send, setup_team

    class Unavailable:
        async def complete(self, messages, tools):
            raise ModelUnavailable('Course API request timed out')

    app = create_app(model=Unavailable(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                     operator_token=OPERATOR, schedule=False)
    with TestClient(app) as client:
        base, lead, _, _ = setup_team(client)
        send(client, base, OPERATOR, request_id='retry-obstacle', text='work', mentions=[lead['member_id']])
        scheduler = DeliveryScheduler(app.state.runtime, app.state.collaboration, app.state.workspace)

        async def fail_and_create_obstacle():
            runtime = app.state.runtime
            job = scheduler._claim_one()
            runtime.launch(*job)
            await runtime.active[job[0]].task
            with runtime.db:
                if obstacle == 'another_recoverable':
                    other = runtime.create_turn(lead['session_id'], 'other job')
                    runtime._write_finish(other, 'recoverable', error='interrupted')
                else:
                    runtime.journal.next_generation(job[1])
            return job[1]

        turn_id = client.portal.call(fail_and_create_obstacle)
        endpoint = f"/v1/sessions/{lead['session_id']}/turns/{turn_id}/retry"
        assert client.post(endpoint, headers=auth(OPERATOR)).status_code == 409


def test_operator_resumes_step_limit_without_replaying_tools(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from test_collaboration import OPERATOR, auth, send, setup_team
    from test_scheduler import Sandbox, application, turns, wait_until

    step_limit = 20

    class StepModel:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls <= step_limit:
                return {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': f'command-{self.calls}', 'type': 'function', 'function': {
                        'name': 'run_command', 'arguments': json.dumps({'command': str(self.calls)})}}]}
            return {'role': 'assistant', 'content': 'continued'}

    model = StepModel()
    sandbox = Sandbox()
    with TestClient(application(tmp_path, model, sandbox)) as client:
        base, lead, _, tokens = setup_team(client)
        send(client, base, OPERATOR, request_id='limit-resume', text='work', mentions=[lead['member_id']])
        failed = wait_until(lambda: next((t for t in turns(client, lead['session_id'], tokens[lead['member_id']])
                                         if t['status'] == 'failed'), None), timeout=10)
        endpoint = f"/v1/sessions/{lead['session_id']}/turns/{failed['turn_id']}/retry"
        assert len(sandbox.commands) == step_limit
        assert client.post(endpoint, headers=auth(OPERATOR)).status_code == 202
        completed = wait_until(lambda: next((t for t in turns(client, lead['session_id'], tokens[lead['member_id']])
                                            if t['status'] == 'completed'), None), timeout=10)
        assert completed['turn_id'] == failed['turn_id']
        assert len(sandbox.commands) == step_limit and model.calls == step_limit + 1
        assert client.post(endpoint, headers=auth(OPERATOR)).status_code == 202
