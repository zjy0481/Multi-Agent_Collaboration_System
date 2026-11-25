from fastapi.testclient import TestClient
from test_collaboration import app, auth, OPERATOR, events, snapshot
from test_delegation import context, terms, ok
from test_task_results import register, publish, submit, submission, review, decision


def test_candidate_request_persists_without_claiming_an_independent_review(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        owner_id, reviewer_id = owner['member_id'], reviewer['member_id']
        task = register(client, base, lead['member_id'], tokens, goal, terms(owner_id, reviewer_id))
        url = f'{base}/tasks/{task}/delivery-requests'
        listing = f'{base}/delivery-requests'
        body = {'request_id':'r1', 'expected_revision':1, 'paths':['draft.py'], 'note':'please check'}
        assert client.get(listing, headers=auth(tokens[owner_id])).status_code == 403
        assert client.post(url, headers=auth(tokens[owner_id]), json=body).status_code == 403
        cursor = snapshot(client, base, OPERATOR)['cursor']
        receipt = ok(client.post(url, headers=auth(OPERATOR), json=body))
        assert ok(client.post(url, headers=auth(OPERATOR), json=body)) == receipt
        assert client.post(url, headers=auth(OPERATOR), json={**body,'paths':['other']}).status_code == 409
        assert client.post(url, headers=auth(OPERATOR), json={**body,'request_id':'r2'}).status_code == 409
        entry = next(t for t in ok(client.get(listing, headers=auth(OPERATOR)))['tasks'] if t['id'] == task)
        assert entry['request'] == receipt and 'paths' not in str(entry)
        delivery = next(d for d in ok(client.get(f'{base}/deliveries', headers=auth(tokens[owner_id]))) if d['purpose']=='task.delivery_requested')
        service = client.app.state.collaboration
        principal = client.portal.call(service.auth.resolve, tokens[owner_id])
        work = client.portal.call(service.execution_input, delivery, principal)
        assert work['candidate_delivery']['paths'] == ['draft.py']
        assert work['candidate_delivery']['reviewer_session_id'] == reviewer['session_id']
        assert any(kind == 'delivery.requested' for kind, _, _ in events(client, base, OPERATOR, after=cursor))
        version = publish(client, base, owner, [lead, reviewer], b'result')
        sent = ok(submit(client, f'{base}/tasks/{task}', tokens[owner_id], submission(version)))
        entry = next(t for t in ok(client.get(listing, headers=auth(OPERATOR)))['tasks'] if t['id'] == task)
        assert entry['request']['status'] == 'stale' and 'submission_id' not in entry['request']
        ok(review(client, f'{base}/tasks/{task}', tokens[reviewer_id], sent['submission_id'], decision()))
        entry = next(t for t in ok(client.get(listing, headers=auth(OPERATOR)))['tasks'] if t['id'] == task)
        assert entry['request']['status'] == 'stale' and 'review_id' not in entry['request']
        client.portal.call(service.delivery_requests.finish, delivery['message_id'], principal)
        assert any(kind == 'delivery.finished' for kind, _, _ in events(client, base, OPERATOR, after=cursor))
    with TestClient(app(tmp_path)) as client:
        entry = next(t for t in ok(client.get(listing, headers=auth(OPERATOR)))['tasks'] if t['id'] == task)
        assert entry['request']['status'] == 'stale'


def test_request_paths_recoverable_and_context_invalidation(tmp_path):
    from collaboration_runtime.collaboration import CollaborationInvalid
    import pytest
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        task = register(client, base, lead['member_id'], tokens, goal,
                        terms(owner['member_id'], reviewer['member_id']))
        url = f'{base}/tasks/{task}/delivery-requests'
        body = {'request_id': 'path', 'expected_revision': 1, 'paths': []}
        for paths in [['../x'], ['/x'], ['a//b'], ['a\\b'], ['a', 'a'], ['x\n'], [str(i) for i in range(9)]]:
            assert client.post(url, headers=auth(OPERATOR), json={**body, 'paths': paths}).status_code == 422
        ok(client.post(url, headers=auth(OPERATOR), json=body))
        delivery = next(d for d in ok(client.get(f'{base}/deliveries', headers=auth(tokens[owner['member_id']])))
                        if d['purpose'] == 'task.delivery_requested')
        service = client.app.state.collaboration
        principal = client.portal.call(service.auth.resolve, tokens[owner['member_id']])
        def recoverable():
            db = service.db
            with db:
                db.execute("INSERT INTO turns (id,role,status) VALUES ('test-turn',?,'recoverable')", (owner['session_id'],))
                db.execute("INSERT INTO scheduled_turns VALUES (?,?,?,?)", ('test-turn', delivery['event_id'], owner['session_id'], delivery['purpose']))
                db.execute("UPDATE message_deliveries SET status='running' WHERE event_id=? AND session_id=?", (delivery['event_id'], owner['session_id']))
        client.portal.call(recoverable)
        listing = f'{base}/delivery-requests'
        entry = next(t for t in ok(client.get(listing, headers=auth(OPERATOR)))['tasks'] if t['id'] == task)
        assert entry['request']['status'] == 'running'
        assert client.post(url, headers=auth(OPERATOR), json={**body, 'request_id':'second'}).status_code == 409
        version = publish(client, base, owner, [lead, reviewer], b'result')
        ok(submit(client, f'{base}/tasks/{task}', tokens[owner['member_id']], submission(version)))
        with pytest.raises(CollaborationInvalid):
            client.portal.call(service.delivery_requests.context, delivery['message_id'], principal)
        def cancel():
            with service.db:
                service.db.execute("UPDATE team_goals SET status='cancelled' WHERE id=?", (goal,))
        client.portal.call(cancel)
        entry = next(t for t in ok(client.get(listing, headers=auth(OPERATOR)))['tasks'] if t['id'] == task)
        assert entry['request']['status'] == 'stale'
