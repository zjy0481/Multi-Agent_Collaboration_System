"""Opt-in end-to-end report from a scheduled course-model close and Docker artifact."""
import json
import os
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.model import load_model
from collaboration_runtime.sandbox import DockerSandbox
from test_collaboration import OPERATOR, auth, bootstrap, send
from test_delegation import context, ok
from test_goal_lifecycle import acceptance, lifecycle, register
from test_task_results import decision, review, submission, submit

pytestmark = pytest.mark.skipif(
    os.environ.get('RUN_DOCKER_TESTS') != '1' or os.environ.get('RUN_LIVE_COURSE_TESTS') != '1',
    reason='requires explicit Docker and live course opt-ins',
)


def test_scheduled_course_close_exports_report_and_docker_evidence(tmp_path):
    course = load_model()
    assert course.identity['mode'] == 'course'
    marker = 'T010_CLOSE_' + uuid4().hex
    calls = []

    class RoutedModel:
        identity = course.identity

        async def complete(self, messages, tools):
            close_ids = {call['id'] for record in calls for call in record['response'].get('tool_calls') or []
                         if call.get('function', {}).get('name') == 'close_goal'}
            is_receipt = bool(messages and messages[-1].get('role') == 'tool'
                              and messages[-1].get('tool_call_id') in close_ids)
            is_prompt = any(marker in (m.get('content') or '') for m in messages if m.get('role') == 'user')
            if not (is_prompt or is_receipt):
                return {'role': 'assistant', 'content': 'Fixture completed.'}
            actual = json.loads(json.dumps(messages, ensure_ascii=False))
            response = await course.complete(actual, tools)
            calls.append({'messages': actual, 'tools': tools, 'response': response})
            return response

        async def aclose(self):
            await course.aclose()

    sandbox = DockerSandbox(tmp_path / 'sandbox')
    workspaces = []
    try:
        application = create_app(model=RoutedModel(), sandbox=sandbox, state_dir=tmp_path / 'state',
                                 operator_token=OPERATOR, bootstrap=bootstrap, schedule=True)
        with TestClient(application) as client:
            base, lead, others, tokens, goal_id = context(client)
            workspaces = [member['workspace_id'] for member in (lead, *others)]
            owner, reviewer = others[:2]
            task_id = register(client, base, lead['member_id'], tokens, goal_id,
                               owner['member_id'], reviewer['member_id'])
            proof = 't010-docker-proof'
            command = sandbox.run(owner['workspace_id'], f"printf '{proof}' > result.txt; cat result.txt")
            assert command['exit_code'] == 0 and command['stdout'] == proof
            published = client.portal.call(application.state.workspace.execute, owner['session_id'],
                                           'publish_artifact', json.dumps({'path': 'result.txt',
                                           'reader_session_ids': [lead['session_id'], reviewer['session_id']]}))
            assert published['ok'], published
            artifact = published['result']
            endpoint = f'{base}/tasks/{task_id}'
            submitted = ok(submit(client, endpoint, tokens[owner['member_id']], submission(artifact)))
            ok(review(client, endpoint, tokens[reviewer['member_id']], submitted['submission_id'], decision()))
            revision = ok(acceptance(client, base, tokens[lead['member_id']], goal_id, 1,
                                     [('required', task_id)]))['revision']
            source = lifecycle(client, base, tokens[lead['member_id']], goal_id)['goal']['source_message_id']
            args = {'request_id': uuid4().hex, 'goal_id': goal_id, 'expected_revision': revision,
                    'outcome': 'completed', 'final_version_ids': [artifact['version_id']]}
            sent = send(client, base, OPERATOR, request_id=uuid4().hex, kind='supplement', reply_to=source,
                        text=f'{marker}. Call close_goal once with these exact JSON arguments: {json.dumps(args)}. Then report the tool result.')
            deadline = time.monotonic() + 180
            turn = None
            while time.monotonic() < deadline:
                row = client.portal.call(lambda: application.state.runtime.db.execute('''
                    SELECT t.id,t.status FROM scheduled_turns s JOIN turns t ON t.id=s.turn_id
                    WHERE s.event_id=? AND s.session_id=?''', (sent['event_id'], lead['session_id'])).fetchone())
                if row and row['status'] not in ('running', 'recoverable'):
                    turn = dict(row)
                    break
                time.sleep(0.1)
            assert turn and turn['status'] == 'completed', turn
            receipts = client.portal.call(lambda: [dict(row) for row in application.state.runtime.db.execute('''
                SELECT call_id,state,result FROM execution_receipts WHERE turn_id=? AND name='close_goal'
                ORDER BY step,position''', (turn['id'],)).fetchall()])
            assert len(receipts) == 1 and receipts[0]['state'] == 'completed'
            assert json.loads(receipts[0]['result'])['ok']
            assert any(not call['tools'] and any(
                m.get('role') == 'tool' and m.get('tool_call_id') == receipts[0]['call_id']
                and json.loads(m['content']).get('ok') for m in call['messages']
            ) for call in calls[1:])
            user_report = ok(client.get(f'/v1/goals/{goal_id}/report', headers=auth(OPERATOR)))
            assert user_report['status'] == 'completed'
            assert user_report['counts'] == {'registered_tasks': 1, 'accepted_tasks': 1}
            assert user_report['subtask_completion_rate'] == 1
            assert user_report['run_modes'] == ['course']
            assert any(call['message_id'] == sent['message_id'] and call['model'] == course.identity['model']
                       and 'turn_id' not in call for call in user_report['references']['model_calls'])
            assert any(v['version_id'] == artifact['version_id'] for v in user_report['references']['versions'])
            download = client.get(f"/v1/artifacts/{artifact['artifact_id']}/versions/{artifact['version']}", headers=auth(OPERATOR))
            assert download.status_code == 200 and download.content == proof.encode()
            lead_report = ok(client.get(f'/v1/goals/{goal_id}/report', headers=auth(tokens[lead['member_id']])))
            assert any(call['turn_id'] == turn['id'] and call['mode'] == 'course'
                       for call in lead_report['references']['model_calls'])
            assert any(item['task_id'] == task_id and item['decision'] == 'approved'
                       for item in lead_report['references']['reviews'])
            aggregate = ok(client.get(f'{base}/evaluation/report', headers=auth(OPERATOR),
                                      params=[('assignment_ids', source), ('assignment_ids', source)]))
            assert aggregate['assignment_ids'] == [source]
            assert aggregate['counts']['assigned_goals'] == aggregate['counts']['completed_goals'] == 1
            assert aggregate['goal_completion_rate'] == 1 and aggregate['goals'] == [user_report]
    finally:
        if workspaces:
            sandbox.cleanup(*workspaces)
