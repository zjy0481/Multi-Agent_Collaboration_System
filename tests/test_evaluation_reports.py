"""Deterministic HTTP acceptance tests for recomputable evaluation reports."""
from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient

from test_clarifications import ask
from test_collaboration import OPERATOR, ROOT, app, auth, send, setup_team
from test_delegation import create, key, ok, terms
from test_goal_lifecycle import (accept_task, acceptance, activate, assignment, goal,
                                 lifecycle, register, terminate)
from test_scheduler import Sandbox as SchedulerSandbox, application as scheduled_app, wait_until
from test_task_results import decision, publish, review, submission, submit


def report(client, goal_id, token):
    return ok(client.get(f'/v1/goals/{goal_id}/report', headers=auth(token)))


def aggregate(client, base, token, ids):
    return client.get(f'{base}/evaluation/report', headers=auth(token),
                      params=[('assignment_ids', value) for value in ids])


def test_report_recomputes_assignment_and_frozen_task_counts(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        lead_id, owner, reviewer = lead['member_id'], others[0], others[1]
        token = tokens[lead_id]
        sources = [assignment(client, base, text) for text in ('Deliver', 'Fail', 'Cancel before registration')]
        first = goal(client, base, token, sources[0])['goal_id']
        task, version = accept_task(client, base, tokens, first, lead_id, lead, owner, reviewer)
        assert acceptance(client, base, token, first, 1, [('delivery', task)]).status_code == 201
        ok(terminate(client, base, token, first, 2, 'completed', final_version_ids=[version['version_id']]))
        second = goal(client, base, token, sources[1])['goal_id']
        task = register(client, base, lead_id, tokens, second, owner['member_id'], reviewer['member_id'])
        endpoint = f'{base}/tasks/{task}'
        v1 = publish(client, base, owner, [lead, reviewer], b'initial')
        submitted = ok(submit(client, endpoint, tokens[owner['member_id']], submission(v1)))
        ok(review(client, endpoint, tokens[reviewer['member_id']], submitted['submission_id'], decision('returned')))
        v2 = publish(client, base, owner, [lead, reviewer], b'reworked', v1['artifact_id'])
        submitted = ok(submit(client, endpoint, tokens[owner['member_id']], submission(v2)))
        ok(review(client, endpoint, tokens[reviewer['member_id']], submitted['submission_id'], decision()))
        accept_task(client, base, tokens, second, lead_id, lead, owner, reviewer)
        missing_task = register(client, base, lead_id, tokens, second, owner['member_id'], reviewer['member_id'])
        create(client, base, token, second, terms(owner['member_id'], reviewer['member_id']))
        ok(acceptance(client, base, token, second, 1, [('missing', missing_task)]))
        ok(terminate(client, base, token, second, 2, 'failed', reason='Dependency failed', evidence=['Failure evidence']))
        question = ask(client, lead, key(), sources[2]['message_id'], 'Clarify before registration?')
        send(client, base, OPERATOR, request_id=key(), text='Clarified', reply_to=question['question']['question_id'])
        ok(client.post(f"{base}/assignments/{sources[2]['message_id']}/terminate", headers=auth(OPERATOR),
                       json={'request_id': key(), 'reason': 'User cancelled'}))
        ids = [source['message_id'] for source in sources]
        result = ok(aggregate(client, base, OPERATOR, ids + [ids[0]]))
        assert result['assignment_ids'] == ids
        assert result['counts'] == {'assigned_goals': 3, 'completed_goals': 1, 'failed_goals': 1,
                                    'timed_out_goals': 0, 'cancelled_goals': 1, 'unfinished_goals': 0}
        assert result['goal_completion_rate'] == pytest.approx(1 / 3)
        by_assignment = {item['assignment_id']: item for item in result['goals']}
        failed = by_assignment[ids[1]]
        assert failed['counts'] == {'registered_tasks': 3, 'accepted_tasks': 2}
        assert failed['subtask_completion_rate'] == pytest.approx(2 / 3)
        assert failed['terminal']['reason'] == 'Dependency failed'
        stored = lifecycle(client, base, OPERATOR, second)['terminal']
        assert failed['terminal'] == {key: stored[key] for key in failed['terminal']}
        assert failed['acceptance_revision'] == 2
        assert not failed['references']['reviews']
        owner_report = report(client, second, tokens[owner['member_id']])
        assert set(owner_report['task_ids']) == set(stored['frozen']['effective_task_ids'])
        assert set(owner_report['accepted_task_ids']) == set(stored['frozen']['accepted_task_ids'])
        assert [ref['decision'] for ref in owner_report['references']['reviews'] if ref['task_id'] == task] == [
            'returned', 'approved',
        ]
        outsider = report(client, second, tokens[others[2]['member_id']])
        assert outsider['counts_scope'] == 'visible_tasks'
        assert outsider['counts'] == {'registered_tasks': 0, 'accepted_tasks': 0}
        assert outsider['task_ids'] == outsider['references']['reviews'] == []
        assert outsider['subtask_completion_rate'] is None
        cancelled = by_assignment[ids[2]]
        assert cancelled['goal_id'] is None and cancelled['status'] == 'cancelled'
        assert cancelled['subtask_completion_rate'] is None
        wait, = cancelled['human_waits']
        assert wait['question_id'] == question['question']['question_id']
        assert wait['ended_at'] and wait['duration_seconds'] >= 0
        for item in by_assignment.values():
            assert item['elapsed_seconds'] >= 0
            assert item['terminal']['server_time']
            assert datetime.fromisoformat(item['assigned_at'].replace('Z', '+00:00')) <= datetime.fromisoformat(item['terminal']['server_time'].replace('Z', '+00:00'))
        assert report(client, second, OPERATOR) == failed
        assert ok(aggregate(client, base, OPERATOR, ids))['goals'] == result['goals']


def test_pending_zero_denominator_and_scope_boundaries(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, _, tokens = setup_team(client)
        source = assignment(client, base, 'Pending')
        token = tokens[lead['member_id']]
        pending = ok(aggregate(client, base, token, [source['message_id']]))['goals'][0]
        assert pending['status'] == 'pending_registration' and pending['goal_id'] is None
        assert pending['elapsed_seconds'] is None and pending['terminal'] is None
        registered = goal(client, base, token, source)['goal_id']
        item = report(client, registered, token)
        assert item['counts'] == {'registered_tasks': 0, 'accepted_tasks': 0}
        assert item['subtask_completion_rate'] is None and item['status'] == 'active'
        ordinary = send(client, base, OPERATOR, request_id=key(), text='Ordinary message')
        for bad in (key(), ordinary['message_id']):
            assert aggregate(client, base, token, [source['message_id'], bad]).status_code == 404
        roster = next((ROOT / 'test samples').glob('scenario_1*/01_role_initialization.md')).read_text(encoding='utf-8')
        team = ok(client.post('/v1/teams', headers=auth(OPERATOR), json={'request_id': key(), 'roster': roster}))
        other_base = f"/v1/teams/{team['team_id']}"
        other_token = ok(client.post(f"{other_base}/members/{team['lead_member_id']}/credential",
                                     headers=auth(OPERATOR)))['token']
        foreign = assignment(client, other_base, 'Foreign assignment')
        assert aggregate(client, base, token, [foreign['message_id']]).status_code == 404
        assert client.get(f'/v1/goals/{registered}/report', headers=auth(other_token)).status_code == 404
        assert client.get(f'/v1/goals/{key()}/report', headers=auth(token)).status_code == 404
        assert ok(aggregate(client, base, token, []))['goal_completion_rate'] is None


def test_report_projects_private_wait_and_preserves_stop_on_late_answer(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        lead_token = tokens[lead['member_id']]
        source = assignment(client, base, 'Public work')
        goal_id = goal(client, base, lead_token, source)['goal_id']
        member = others[0]
        work = send(client, base, OPERATOR, request_id=key(), kind='supplement', reply_to=source['message_id'],
                    recipient_id=member['member_id'], text='Private work text')
        question = client.portal.call(client.app.state.collaboration.execute_tool, member['session_id'],
                                      'ask_human', json.dumps({'request_id': key(),
                                      'source_message_id': work['message_id'], 'text': 'Private clarification'}))
        assert question['ok'], question
        question_id = question['result']['question']['question_id']
        assert report(client, goal_id, OPERATOR)['human_waits'][0]['ended_at'] is None
        assert report(client, goal_id, lead_token)['human_waits'] == []
        ok(terminate(client, base, OPERATOR, goal_id, 1, 'cancelled', reason='User stopped'))
        before = report(client, goal_id, OPERATOR)
        answer = send(client, base, OPERATOR, request_id=key(), reply_to=question_id, text='Late private answer')
        after = report(client, goal_id, OPERATOR)
        assert after['human_waits'] == before['human_waits']
        assert after['elapsed_seconds'] == before['elapsed_seconds']
        assert answer['message_id'] in {message['id'] for message in after['references']['dialogue']}
        other = report(client, goal_id, lead_token)
        assert not other['human_waits']
        assert {message['id'] for message in other['references']['dialogue']} == {source['message_id']}

        hidden = send(client, base, OPERATOR, request_id=key(), kind='assignment',
                      recipient_id=lead['member_id'], text='Private assignment')
        hidden_goal = goal(client, base, lead_token, hidden)['goal_id']
        member_token = tokens[member['member_id']]
        assert not_found(client, hidden_goal, member_token) == not_found(client, key(), member_token)
        assert aggregate(client, base, member_token, [hidden['message_id']]).json() == aggregate(
            client, base, member_token, [key()],
        ).json()


def not_found(client, goal_id, token):
    response = client.get(f'/v1/goals/{goal_id}/report', headers=auth(token))
    assert response.status_code == 404
    return response.json()


def test_preregistration_wait_follows_assignment_with_another_goal_active(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, _, tokens = setup_team(client)
        token = tokens[lead['member_id']]
        first = assignment(client, base, 'Current assignment')
        first_goal = goal(client, base, token, first)['goal_id']
        upcoming = assignment(client, base, 'Next assignment')
        question = ask(client, lead, key(), upcoming['message_id'], 'Clarify the next assignment?')
        question_id = question['question']['question_id']
        send(client, base, OPERATOR, request_id=key(), text='Next assignment clarification', reply_to=question_id)
        second = goal(client, base, token, upcoming)
        assert second['status'] == 'queued'
        ok(terminate(client, base, token, first_goal, 1, 'failed', reason='Failed', evidence=['Failure fact']))
        ok(activate(client, base, token, second['goal_id']))
        ok(terminate(client, base, OPERATOR, second['goal_id'], 1, 'cancelled', reason='Stopped second'))

        first_report, second_report = ok(aggregate(client, base, token, [first['message_id'], upcoming['message_id']]))['goals']
        assert first_report['human_waits'] == []
        wait, = second_report['human_waits']
        assert wait['question_id'] == question_id
        assert wait['started_at'] < wait['ended_at'] < second_report['registered_at']
        start = datetime.fromisoformat(second_report['assigned_at'])
        end = datetime.fromisoformat(second_report['terminal']['server_time'])
        assert second_report['elapsed_seconds'] == (end - start).total_seconds()
        assert wait['duration_seconds'] < second_report['elapsed_seconds']


def test_timeout_reason_and_time_are_preserved(tmp_path):
    from collaboration_runtime.api import create_app
    from test_collaboration import Model, Sandbox, bootstrap
    from test_goal_lifecycle import HOST_TOKEN

    application = create_app(model=Model(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                             operator_token=OPERATOR, host_token=HOST_TOKEN, schedule=False)
    with TestClient(application) as client:
        base, lead, _, tokens = setup_team(client)
        source = assignment(client, base, 'Time limited')
        goal_id = goal(client, base, tokens[lead['member_id']], source)['goal_id']
        ok(client.post(f'{base.replace("/v1/teams/", "/v1/host/teams/")}/goals/{goal_id}/terminal-events',
                       headers=auth(HOST_TOKEN), json={'event_key': key(), 'expected_revision': 1,
                       'outcome': 'timed_out', 'reason': 'Deadline exceeded', 'evidence': ['Deadline reached']}))
        item = report(client, goal_id, OPERATOR)
        assert item['status'] == 'timed_out'
        assert item['terminal']['reason'] == 'Deadline exceeded'
        assert item['terminal']['server_time'] and item['elapsed_seconds'] >= 0
        result = ok(aggregate(client, base, OPERATOR, [source['message_id']]))
        assert result['counts']['timed_out_goals'] == 1
        assert result['goal_completion_rate'] == 0


def test_model_references_follow_visible_scheduled_messages(tmp_path):
    class Model:
        identity = {'mode': 'mock', 'endpoint': None, 'model': 'report-fixture'}

        async def complete(self, messages, tools):
            return {'role': 'assistant', 'content': 'Acknowledged'}

    with TestClient(scheduled_app(tmp_path, Model(), SchedulerSandbox())) as client:
        base, lead, others, tokens = setup_team(client)
        token = tokens[lead['member_id']]
        source = assignment(client, base, 'Original assignment')
        goal_id = goal(client, base, token, source)['goal_id']
        private = send(client, base, OPERATOR, request_id=key(), kind='supplement', reply_to=source['message_id'],
                       recipient_id=others[0]['member_id'], text='Private supplement')
        unscoped = send(client, base, OPERATOR, request_id=key(), text='Unrelated request')

        def finished(event_id):
            return client.portal.call(lambda: client.app.state.runtime.db.execute("""
                SELECT t.id FROM scheduled_turns s JOIN turns t ON t.id = s.turn_id
                WHERE s.event_id = ? AND t.status = 'completed'
            """, (event_id,)).fetchone())

        for receipt in (source, private, unscoped):
            wait_until(lambda: finished(receipt['event_id']))
        lead_calls = report(client, goal_id, token)['references']['model_calls']
        call, = lead_calls
        assert call['message_id'] == source['message_id'] and call['model'] == 'report-fixture'
        assert call['turn_id'] == finished(source['event_id'])['id'] and call['event_seq'] > 0
        user_view = report(client, goal_id, OPERATOR)
        assert user_view['run_modes'] == ['mock']
        user_calls = user_view['references']['model_calls']
        assert {call['message_id'] for call in user_calls} == {source['message_id'], private['message_id']}
        assert all('turn_id' not in call for call in user_calls)
