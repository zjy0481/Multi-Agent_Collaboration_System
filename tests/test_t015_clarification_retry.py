"""Offline regression: an answered clarification survives a model timeout."""
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from collaboration_runtime.model import ModelUnavailable
from test_collaboration import OPERATOR, auth, setup_team, send, snapshot
from test_goal_lifecycle import assignment, goal
from test_scheduler import Sandbox, application, current_delivery, tool, turns, wait_until

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 't015'))
from recovery import retry_key


def test_answered_clarification_retry_preserves_receipt(tmp_path):
    class Model:
        def __init__(self):
            self.resume_payloads = []
            self.failed = False

        async def complete(self, messages, tools):
            payload = current_delivery(messages)
            if payload['delivery']['purpose'] != 'clarification.resume':
                return tool('ask_human', 'ask', {'request_id':'question',
                            'source_message_id':payload['message']['id'], 'text':'Which rule?'})
            self.resume_payloads.append(payload)
            if not any(m.get('tool_call_id') == 'one-command' for m in messages):
                return tool('run_command', 'one-command', {'command':'first'})
            if not self.failed:
                self.failed = True
                raise ModelUnavailable('Course API request timed out')
            return {'role':'assistant', 'content':'Answer applied'}

    model, sandbox = Model(), Sandbox()
    with TestClient(application(tmp_path, model, sandbox)) as client:
        base, lead, _, tokens = setup_team(client)
        source = assignment(client, base, 'Work requiring clarification')
        registered = goal(client, base, tokens[lead['member_id']], source)
        question = wait_until(lambda: next(iter(snapshot(client, base, OPERATOR)['questions']), None))
        answer = send(client, base, OPERATOR, request_id='answer', text='Rule A', reply_to=question['question_id'])
        session = lead['session_id']
        failed = wait_until(lambda: next((t for t in turns(client, session, tokens[lead['member_id']])
                                          if t['status'] == 'failed'), None))
        def read_recovery():
            db = client.app.state.runtime.db
            tid = failed['turn_id']
            checkpoint = client.app.state.runtime.journal.checkpoint(tid)
            event = db.execute("SELECT payload FROM events WHERE turn_id=? AND type='model.failed' ORDER BY rowid DESC LIMIT 1", (tid,)).fetchone()
            scope = client.app.state.goal_lifecycle._scope_objects(base.split('/')[-1], registered['goal_id'], source['message_id'])
            return checkpoint['generation'], json.loads(event['payload']), scope['message_ids']
        generation, failure, scope = client.portal.call(read_recovery)
        turn = {'id':failed['turn_id'], 'role':session, 'purpose':'clarification.resume',
                'message_id':answer['message_id'], 'status':'failed', 'generation':generation,
                'failure':{**failure, 'type':'model.failed'}}
        assert retry_key(turn, source['message_id'], scope, set(), set(), {}, 120)
        assert len(sandbox.commands) == 1
        response = client.post(f"/v1/sessions/{session}/turns/{failed['turn_id']}/retry", headers=auth(OPERATOR))
        assert response.status_code == 202, response.text
        wait_until(lambda: any(t['turn_id'] == failed['turn_id'] and t['status'] == 'completed'
                               for t in turns(client, session, tokens[lead['member_id']])))
        assert len(sandbox.commands) == 1
        assert all([item['id'] for item in p['answers']] == [answer['message_id']] for p in model.resume_payloads)
        assert all(p['source_message']['id'] == source['message_id'] for p in model.resume_payloads)
