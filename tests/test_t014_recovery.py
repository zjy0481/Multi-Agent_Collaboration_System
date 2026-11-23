"""T014: simultaneous draft and scoped clarification recovery."""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from test_clarifications import ask, inbox
from test_collaboration import OPERATOR, app, auth, send, snapshot
from test_delegation import context, create, ok, terms


def test_draft_and_parallel_scoped_waits_survive_restart(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        public, private, independent = others[:3]
        token = lambda member: tokens[member['member_id']]
        draft = create(client, base, token(lead), goal,
                       terms(independent['member_id'], lead['member_id']))
        pid = draft['proposal_id']
        payloads = [
            dict(request_id='t014-public', text='Public work', mentions=[public['member_id']]),
            dict(request_id='t014-private', text='Private work', recipient_id=private['member_id']),
            dict(request_id='t014-independent', text='Independent work', mentions=[independent['member_id']]),
        ]
        with ThreadPoolExecutor(max_workers=3) as pool:
            work, secret, other = list(pool.map(lambda p: send(client, base, OPERATOR, **p), payloads))
        q1 = ask(client, public, 't014-question-public', work['message_id'], 'Public question?')['question']['question_id']
        q2 = ask(client, private, 't014-question-private', secret['message_id'], 'Private question?')['question']['question_id']
        assert any(d['message_id'] == other['message_id'] for d in inbox(client, base, token(independent)))

    with TestClient(app(tmp_path)) as client:
        view = ok(client.get(f'/v1/proposals/{pid}', headers=auth(token(lead))))
        assert view['version'] == 1
        answer = send(client, base, OPERATOR, request_id='t014-answer-private', text='Private decision',
                      reply_to=q2, mentions=[public['member_id']])
        resumed = [d for d in inbox(client, base, token(private))
                   if d['purpose'] == 'clarification.resume' and d['message_id'] == answer['message_id']]
        assert len(resumed) == 1 and resumed[0]['source_message_id'] == secret['message_id']
        assert not any(d['message_id'] == answer['message_id'] for d in inbox(client, base, token(public)))
        assert not any(d['message_id'] == work['message_id'] for d in inbox(client, base, token(public)))
        assert any(d['message_id'] == other['message_id'] for d in inbox(client, base, token(independent)))
        questions = {q['question_id']: q for q in snapshot(client, base, OPERATOR)['questions']}
        assert questions[q1]['answer_message_id'] is None
        assert questions[q2]['answer_message_id'] == answer['message_id']
        outside = snapshot(client, base, token(public))
        assert not ({secret['message_id'], q2, answer['message_id']} & {m['id'] for m in outside['messages']})


def test_historical_fork_runs_while_parent_active_and_survives_restart(tmp_path):
    from test_forks import Model, application, start_turn, terminal, wait_until

    model = Model()
    with TestClient(application(tmp_path, model)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_token = tokens[lead['member_id']]
        wait_until(lambda: not client.app.state.runtime.active.get(lead['session_id']))
        turns = []
        for text in ('HISTORY_FIRST_SENTINEL', 'HISTORY_SECOND_SENTINEL', 'HISTORY_THIRD_SECRET'):
            turn = start_turn(client, lead, lead_token, text)
            assert terminal(client, lead['session_id'], lead_token, turn)['status'] == 'completed'
            turns.append(turn)
        endpoint = f"/v1/sessions/{lead['session_id']}/forks"
        body = {'request_id': 't014-fork', 'from_turn_id': turns[1], 'goal_id': goal,
                'purpose': 'FORK_PURPOSE_7', 'scope_id': 't014-history',
                'history_turn_ids': turns[:2], 'version_ids': [], 'allowed_tools': [],
                'contact_session_ids': [others[0]['session_id']]}
        hold = start_turn(client, lead, lead_token, 'PARENT_HOLD')
        try:
            assert model.block_parent.wait(5)
            fork = ok(client.post(endpoint, headers=auth(lead_token), json=body))
            assert fork['from_turn_id'] == turns[1]
            assert model.fork_seen.wait(5)
            assert 'HISTORY_SECOND_SENTINEL' in model.fork_messages
            assert 'HISTORY_THIRD_SECRET' not in model.fork_messages
            assert 'PRIVATE_PARENT_CHAT_SENTINEL' not in model.fork_messages
            assert not model.release_parent.is_set()
        finally:
            model.release_parent.set()
        assert terminal(client, lead['session_id'], lead_token, hold)['status'] == 'completed'
    with TestClient(application(tmp_path, model)) as client:
        replay = ok(client.post(endpoint, headers=auth(lead_token), json=body))
        assert replay['session_id'] == fork['session_id']
        assert replay['from_turn_id'] == turns[1]
