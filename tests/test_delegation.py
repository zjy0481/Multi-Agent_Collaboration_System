"""HTTP acceptance coverage for delegation and task revisions (T004)."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from test_collaboration import ROOT, OPERATOR, app, auth, events, send, setup_team, snapshot


def key():
    return uuid4().hex


def ok(response):
    assert response.is_success, response.text
    return response.json()


def context(client):
    base, lead, others, tokens = setup_team(client)
    source = send(client, base, OPERATOR, request_id=key(), text='Goal', kind='assignment')
    goal = ok(client.post(f'{base}/goals', headers=auth(tokens[lead['member_id']]),
                          json={'request_id': key(), 'source_message_id': source['message_id']}))
    return base, lead, others, tokens, goal['goal_id']


def terms(owner, reviewer, **extra):
    return {'deliverables': ['implementation'], 'acceptance_criteria': ['passes review'],
            'owner_id': owner, 'reviewer_id': reviewer, 'commitment_ids': [],
            'input_version_ids': [], 'dependency_ids': [], **extra}


def create(client, base, token, goal, value, **extra):
    payload = {'request_id': key(), 'goal_id': goal, 'terms': value, **extra}
    if 'task_id' not in payload:
        payload.setdefault('scope_id', key())
    return ok(client.post(f'{base}/proposals', headers=auth(token), json=payload))


def action(client, proposal, token, name, version=1, **extra):
    return client.post(f'/v1/proposals/{proposal}/{name}', headers=auth(token),
                       json={'request_id': key(), 'expected_version': version, **extra})


def confirm_all(client, proposal, tokens, version=1):
    view = ok(client.get(f'/v1/proposals/{proposal}', headers=auth(next(iter(tokens.values()))), params={'version': version}))
    for member in view['required_member_ids']:
        ok(action(client, proposal, tokens[member], 'confirm', version))


def test_confirmation_counter_rejection_and_withdrawal(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        a, b = [m['member_id'] for m in others[:2]]
        lead_id = lead['member_id']
        draft = create(client, base, tokens[lead_id], goal, terms(a, b))
        pid = draft.get('proposal_id', draft.get('id'))
        assert action(client, pid, tokens[lead_id], 'assign').status_code == 409
        ok(action(client, pid, tokens[a], 'confirm'))
        revised = ok(action(client, pid, tokens[b], 'counter', terms=terms(a, b, deliverables=['revised'])))
        assert revised['version'] == 2
        assert action(client, pid, tokens[lead_id], 'assign', 2).status_code == 409
        assert action(client, pid, tokens[a], 'confirm', 1).status_code == 409
        confirm_all(client, pid, tokens, 2)
        ok(action(client, pid, tokens[lead_id], 'assign', 2))
        for name in ('reject', 'withdraw'):
            draft = create(client, base, tokens[lead_id], goal, terms(a, b))
            pid = draft.get('proposal_id', draft.get('id'))
            ok(action(client, pid, tokens[a if name == 'reject' else lead_id], name, reason='No longer suitable'))
            assert action(client, pid, tokens[lead_id], 'assign').status_code == 409


def test_registration_replay_concurrency_and_notifications(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_id, a, b = lead['member_id'], others[0]['member_id'], others[1]['member_id']
        cursor = snapshot(client, base, tokens[lead_id])['cursor']
        draft = create(client, base, tokens[lead_id], goal, terms(a, b))
        pid = draft.get('proposal_id', draft.get('id'))
        received = ok(client.get(f'{base}/deliveries', headers=auth(tokens[a])))
        proposal_delivery, = [item for item in received if item['purpose'] == 'delegation.proposal']
        collaboration = client.app.state.collaboration
        principal = client.portal.call(collaboration.auth.resolve, tokens[a])
        work = client.portal.call(collaboration.execution_input, proposal_delivery, principal)
        assert work['proposal']['id'] == pid and work['proposal']['version'] == 1
        confirm_all(client, pid, tokens)
        payload = {'request_id': key(), 'expected_version': 1}
        url = f'/v1/proposals/{pid}/assign'
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda member: client.post(url, headers=auth(tokens[member]), json={**payload, 'request_id': key()}), [lead_id, a]))
        successes = [r.json() for r in responses if r.is_success]
        assert len(successes) == 2, [r.text for r in responses]
        assert successes[0] == successes[1]
        receipt = ok(client.post(url, headers=auth(tokens[lead_id]), json=payload))
        assert ok(client.post(url, headers=auth(tokens[lead_id]), json=payload)) == receipt
        assert client.post(url, headers=auth(tokens[lead_id]), json={**payload, 'expected_version': 2}).status_code == 409
        task = ok(client.get(f"{base}/tasks/{receipt['task_id']}", headers=auth(tokens[a])))
        assert task['revision'] == 1 and task['dispatcher_id'] == lead_id
        observed = events(client, base, tokens[lead_id], after=cursor)
        assert any(kind == 'task.assigned' and data['task']['id'] == receipt['task_id'] for kind, data, _ in observed)
        assert any(kind == 'message.created' and data['message']['id'] == receipt['notification_message_id'] for kind, data, _ in observed)
        assert any(item['message_id'] == receipt['notification_message_id'] for item in ok(client.get(f'{base}/deliveries', headers=auth(tokens[lead_id]))))


def test_revision_staleness_and_changed_commitments(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_id = lead['member_id']
        a, b, c = [m['member_id'] for m in others[:3]]
        initial = create(client, base, tokens[a], goal, terms(a, b))
        pid = initial.get('proposal_id', initial.get('id'))
        confirm_all(client, pid, tokens)
        assigned = ok(action(client, pid, tokens[a], 'assign'))
        task_id = assigned['task_id']
        first = create(client, base, tokens[lead_id], goal, terms(c, lead_id), task_id=task_id, base_revision=1)
        second = create(client, base, tokens[a], goal, terms(a, b, deliverables=['other']), task_id=task_id, base_revision=1)
        first_id, second_id = first.get('proposal_id', first.get('id')), second.get('proposal_id', second.get('id'))
        view = ok(client.get(f'/v1/proposals/{first_id}', headers=auth(tokens[lead_id])))
        assert {a, b, c, lead_id}.issubset(set(view['required_member_ids']))
        confirm_all(client, second_id, tokens)
        confirm_all(client, first_id, tokens)
        updated = ok(action(client, first_id, tokens[lead_id], 'update'))
        assert updated['task_id'] == task_id and updated['revision'] == 2
        assert ok(action(client, first_id, tokens[lead_id], 'update')) == updated
        stale = action(client, second_id, tokens[a], 'update')
        assert stale.status_code == 409
        detail = stale.json()['detail']
        assert detail['expected_revision'] == 1 and detail['current_revision'] == 2
        assert detail['changes']['owner_id'] == {'before': a, 'after': c}
        rebased = ok(action(client, second_id, tokens[a], 'counter', base_revision=2, terms=terms(a, b)))
        assert rebased['version'] == 2 and set(rebased['pending_member_ids']) == {a, b, c, lead_id}
        confirm_all(client, second_id, tokens, 2)
        assert ok(action(client, second_id, tokens[a], 'update', 2))['revision'] == 3
        assert ok(client.get(f'{base}/tasks/{task_id}', headers=auth(tokens[a]), params={'revision': 1}))['terms']['owner_id'] == a


def test_authorization_cross_team_and_cycles(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        leader = lead['member_id']
        a, b = [m['member_id'] for m in others[:2]]
        draft = create(client, base, tokens[leader], goal, terms(a, b))
        pid = draft.get('proposal_id', draft.get('id'))
        outsider = others[2]['member_id']
        assert action(client, pid, tokens[outsider], 'confirm').status_code in (403, 404)
        roster = (ROOT / 'test samples' / 'scenario_1_software_engineering 2' / '01_role_initialization.md').read_text(encoding='utf-8')
        other = ok(client.post('/v1/teams', headers=auth(OPERATOR), json={'request_id': key(), 'roster': roster}))
        foreign = ok(client.post(f"/v1/teams/{other['team_id']}/members/{other['lead_member_id']}/credential", headers=auth(OPERATOR)))['token']
        assert client.get(f'/v1/proposals/{pid}', headers=auth(foreign)).status_code == 404
        assert action(client, pid, foreign, 'confirm').status_code == 404
        confirm_all(client, pid, tokens)
        first = ok(action(client, pid, tokens[leader], 'assign'))['task_id']
        child = create(client, base, tokens[leader], goal, terms(a, b, parent_task_id=first, dependency_ids=[first]))
        child_id = child.get('proposal_id', child.get('id'))
        confirm_all(client, child_id, tokens)
        second = ok(action(client, child_id, tokens[leader], 'assign'))['task_id']
        for relation in ({'parent_task_id': second}, {'dependency_ids': [second]}):
            cycle = client.post(f'{base}/proposals', headers=auth(tokens[leader]), json={
                'request_id': key(), 'goal_id': goal, 'task_id': first, 'base_revision': 1,
                'terms': terms(a, b, **relation)})
            assert cycle.status_code == 422, cycle.text


def test_assistant_scope_and_direct_effects(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        workspace = client.app.state.workspace
        collaboration = client.app.state.collaboration
        scope = key()
        helper = key()
        client.portal.call(partial(workspace.register_assistant, parent_session_id=lead['session_id'],
                                   session_id=helper, scope_id=scope, prompt='Draft delegation',
                                   version_ids=[], allowed_tools=['create_proposal']))
        payload = {'request_id': key(), 'goal_id': goal, 'scope_id': scope,
                   'terms': terms(others[0]['member_id'], others[1]['member_id'])}
        def invoke(name, body):
            return client.portal.call(collaboration.execute_tool, helper, name, json.dumps(body))
        result = invoke('create_proposal', payload)
        assert result['ok'], result
        proposal = result['result'].get('proposal_id', result['result'].get('id'))
        for name in ('assign_task', 'confirm_proposal'):
            denied = invoke(name, {'request_id': key(), 'proposal_id': proposal, 'expected_version': 1})
            assert not denied['ok'] and denied['status'] == 403
        denied = invoke('create_proposal', {**payload, 'request_id': key(), 'scope_id': key()})
        assert not denied['ok']
        client.portal.call(workspace.revoke_assistant, helper)
        assert not invoke('create_proposal', {**payload, 'request_id': key()})['ok']
        unprivileged = key()
        client.portal.call(partial(workspace.register_assistant, parent_session_id=lead['session_id'],
                                   session_id=unprivileged, scope_id=scope, prompt='No tools',
                                   version_ids=[], allowed_tools=[]))
        denied = client.portal.call(collaboration.execute_tool, unprivileged, 'create_proposal', json.dumps(payload))
        assert not denied['ok']


def test_new_member_creates_and_updates_granted_task(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        leader, reviewer = lead['member_id'], others[0]['member_id']
        team = ok(client.get(base, headers=auth(OPERATOR)))
        expanded = ok(client.post(f'{base}/members', headers=auth(OPERATOR), json={
            'request_id': key(), 'expected_version': team['contract']['version'],
            'role_id': 'designer', 'name': 'Designer', 'prompt': 'Design the interface.'}))
        newcomer = next(m for m in expanded['members'] if m['role_id'] == 'designer')
        newcomer_id = newcomer['member_id']
        tokens[newcomer_id] = ok(client.post(f'{base}/members/{newcomer_id}/credential', headers=auth(OPERATOR)))['token']
        # A new formal member can initiate a first delegation.
        draft = create(client, base, tokens[newcomer_id], goal, terms(newcomer_id, reviewer))
        pid = draft.get('proposal_id', draft.get('id'))
        confirm_all(client, pid, tokens)
        own_task = ok(action(client, pid, tokens[newcomer_id], 'assign'))
        assert ok(client.get(f"{base}/tasks/{own_task['task_id']}", headers=auth(tokens[newcomer_id])))['dispatcher_id'] == newcomer_id

        # A task initially outside the new member's scope becomes accessible through a revision.
        initial = create(client, base, tokens[leader], goal, terms(leader, reviewer))
        initial_id = initial.get('proposal_id', initial.get('id'))
        confirm_all(client, initial_id, tokens)
        task_id = ok(action(client, initial_id, tokens[leader], 'assign'))['task_id']
        assert client.get(f'{base}/tasks/{task_id}', headers=auth(tokens[newcomer_id])).status_code in (403, 404)
        grant = create(client, base, tokens[leader], goal, terms(newcomer_id, reviewer), task_id=task_id, base_revision=1)
        grant_id = grant.get('proposal_id', grant.get('id'))
        confirm_all(client, grant_id, tokens)
        ok(action(client, grant_id, tokens[leader], 'update'))
        revision = create(client, base, tokens[newcomer_id], goal,
                          terms(newcomer_id, reviewer, deliverables=['new design']), task_id=task_id, base_revision=2)
        revision_id = revision.get('proposal_id', revision.get('id'))
        confirm_all(client, revision_id, tokens)
        cursor = snapshot(client, base, tokens[leader])['cursor']
        result = ok(action(client, revision_id, tokens[newcomer_id], 'update'))
        assert result['task_id'] == task_id and result['revision'] == 3
        observed = events(client, base, tokens[leader], after=cursor)
        assert any(kind == 'task.updated' and data['task']['id'] == task_id and data['task']['revision'] == 3
                   for kind, data, _ in observed), observed
        assert any(kind == 'message.created' and data['message']['id'] == result['notification_message_id']
                   for kind, data, _ in observed)
        assert any(item['message_id'] == result['notification_message_id']
                   for item in ok(client.get(f'{base}/deliveries', headers=auth(tokens[leader]))))


def test_private_goal_and_artifact_references_keep_their_audiences(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_id = lead['member_id']
        owner, reviewer = others[:2]
        owner_id, reviewer_id = owner['member_id'], reviewer['member_id']
        outsider_token = tokens[others[2]['member_id']]
        cursor = snapshot(client, base, outsider_token)['cursor']
        version = client.portal.call(partial(
            client.app.state.workspace.artifacts.publish, team_id=base.rsplit('/', 1)[-1],
            author_session_id=lead['session_id'], source_path='input.txt', content=b'private artifact',
            reader_session_ids=[owner['session_id']],
        ))
        body = {'request_id': key(), 'goal_id': goal, 'scope_id': key(),
                'terms': terms(owner_id, reviewer_id, input_version_ids=[version['version_id']])}
        assert client.post(f'{base}/proposals', headers=auth(tokens[lead_id]), json=body).status_code == 403

        draft = create(client, base, tokens[lead_id], goal, terms(owner_id, owner_id,
                       input_version_ids=[version['version_id']]))
        pid = draft['proposal_id']
        confirm_all(client, pid, tokens)
        task = ok(action(client, pid, tokens[lead_id], 'assign'))
        assert client.get(f'/v1/proposals/{pid}', headers=auth(outsider_token)).status_code == 404
        assert client.get(f"{base}/tasks/{task['task_id']}", headers=auth(outsider_token)).status_code == 404
        assert events(client, base, outsider_token, after=cursor) == []
        endpoint = f"{base}/messages/{task['notification_message_id']}"
        assert client.get(endpoint, headers=auth(outsider_token)).status_code == 404
        assert ok(client.get(endpoint, headers=auth(tokens[lead_id])))['mentions'] == [lead_id]

    with TestClient(app(tmp_path / 'private')) as client:
        base, lead, others, tokens = setup_team(client)
        lead_id = lead['member_id']
        member_id = others[0]['member_id']
        lead_token, member_token = tokens[lead_id], tokens[member_id]
        source = send(client, base, OPERATOR, request_id=key(), text='Private goal', kind='assignment',
                      recipient_id=lead_id)
        goal = ok(client.post(f'{base}/goals', headers=auth(lead_token),
                              json={'request_id': key(), 'source_message_id': source['message_id']}))['goal_id']
        body = {'request_id': key(), 'goal_id': goal, 'scope_id': key(), 'terms': terms(member_id, lead_id)}
        assert client.post(f'{base}/proposals', headers=auth(lead_token), json=body).status_code == 403
        assert client.post(f'{base}/proposals', headers=auth(member_token), json=body).status_code == 404
        cursor = snapshot(client, base, member_token)['cursor']
        draft = create(client, base, lead_token, goal, terms(lead_id, lead_id))
        confirm_all(client, draft['proposal_id'], tokens)
        task = ok(action(client, draft['proposal_id'], lead_token, 'assign'))
        assert events(client, base, member_token, after=cursor) == []
        assert client.get(f"{base}/messages/{task['notification_message_id']}", headers=auth(member_token)).status_code == 404


def test_assignment_required_inputs_are_scoped_and_enforced(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        owner, reviewer, unrelated = others[:3]
        lead_id, owner_id, reviewer_id = lead['member_id'], owner['member_id'], reviewer['member_id']
        version = client.portal.call(partial(
            client.app.state.workspace.artifacts.publish, team_id=base.rsplit('/', 1)[-1],
            author_session_id=lead['session_id'], source_path='required.txt', content=b'input',
            reader_session_ids=[owner['session_id'], reviewer['session_id']],
        ))['version_id']
        source = send(client, base, OPERATOR, request_id=key(), text='Goal', kind='assignment',
                      required_inputs=[{'owner_id': owner_id, 'version_ids': [version]}])
        goal = ok(client.post(f'{base}/goals', headers=auth(tokens[lead_id]),
                              json={'request_id': key(), 'source_message_id': source['message_id']}))['goal_id']
        payload = {'request_id': key(), 'goal_id': goal, 'scope_id': key(), 'terms': terms(owner_id, reviewer_id)}
        failure = client.post(f'{base}/proposals', headers=auth(tokens[lead_id]), json=payload)
        assert failure.status_code == 422 and version in failure.text
        create(client, base, tokens[lead_id], goal, terms(unrelated['member_id'], unrelated['member_id']))
        draft = create(client, base, tokens[lead_id], goal, terms(owner_id, reviewer_id, input_version_ids=[version]))
        assert action(client, draft['proposal_id'], tokens[owner_id], 'counter',
                      terms=terms(owner_id, reviewer_id)).status_code == 422
        payload['terms'] = terms(owner_id, unrelated['member_id'], input_version_ids=[version])
        assert client.post(f'{base}/proposals', headers=auth(tokens[lead_id]), json=payload).status_code == 403
        confirm_all(client, draft['proposal_id'], tokens)
        ok(action(client, draft['proposal_id'], tokens[lead_id], 'assign'))


def test_registration_rechecks_graph_after_other_revision(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, _, tokens, goal = context(client)
        lead_id = lead['member_id']
        token = tokens[lead_id]

        def assign():
            proposal = create(client, base, token, goal, terms(lead_id, lead_id))['proposal_id']
            confirm_all(client, proposal, tokens)
            return ok(action(client, proposal, token, 'assign'))['task_id']

        a, b = assign(), assign()
        first = create(client, base, token, goal, terms(lead_id, lead_id, dependency_ids=[b]),
                       task_id=a, base_revision=1)['proposal_id']
        second = create(client, base, token, goal, terms(lead_id, lead_id, dependency_ids=[a]),
                        task_id=b, base_revision=1)['proposal_id']
        for proposal in (first, second):
            confirm_all(client, proposal, tokens)
        assert ok(action(client, first, token, 'update'))['revision'] == 2
        rejected = action(client, second, token, 'update')
        assert rejected.status_code == 422 and 'cycle' in rejected.json()['detail']
        assert ok(client.get(f'{base}/tasks/{b}', headers=auth(token)))['revision'] == 1


def test_confirmation_history_and_registration_recover_after_restart(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_token, owner = tokens[lead['member_id']], others[0]['member_id']
        draft = create(client, base, lead_token, goal, terms(owner, lead['member_id']))
        pid = draft['proposal_id']
        ok(action(client, pid, tokens[owner], 'confirm'))
        ok(action(client, pid, lead_token, 'counter', terms=terms(owner, lead['member_id'], deliverables=['new'])))
        confirm_all(client, pid, tokens, 2)
    with TestClient(app(tmp_path)) as client:
        history = ok(client.get(f'/v1/proposals/{pid}', headers=auth(lead_token), params={'version': 1}))
        assert history['confirmation_ids'] == [owner] and history['status'] == 'superseded'
        receipt = ok(action(client, pid, lead_token, 'assign', 2))
        assert ok(action(client, pid, tokens[owner], 'assign', 2)) == receipt
        assert len(snapshot(client, base, lead_token)['tasks']) == 1


def test_compact_snapshot_filters_acl_and_bounds_results(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_id = lead['member_id']
        a, b, c = [member['member_id'] for member in others[:3]]
        first = create(client, base, tokens[a], goal, terms(a, b))
        create(client, base, tokens[a], goal, terms(a, b))
        collaboration = client.app.state.collaboration
        service = client.app.state.delegation
        actor = client.portal.call(collaboration.auth.resolve, tokens[a])
        outsider = client.portal.call(collaboration.auth.resolve, tokens[c])
        team_id = base.rsplit('/', 1)[-1]
        compact = client.portal.call(service.compact_snapshot, team_id, actor, goal, 1)
        assert len(compact['proposals']) == 1 and compact['truncated']['proposals']
        assert set(compact['proposals'][0]) == {'id', 'goal_id', 'task_id', 'scope_id', 'version', 'status', 'pending_member_ids'}
        hidden = client.portal.call(service.compact_snapshot, team_id, outsider, goal)
        assert hidden['proposals'] == [] and not hidden['truncated']['proposals']
        confirm_all(client, first['proposal_id'], tokens)
        assigned = ok(action(client, first['proposal_id'], tokens[a], 'assign'))
        compact = client.portal.call(service.compact_snapshot, team_id, actor, goal)
        assert compact['tasks'][0]['id'] == assigned['task_id']
        assert compact['tasks'][0]['revision'] == 1
        full = client.portal.call(service.snapshot, team_id, actor)
        compact_bytes = len(json.dumps(compact).encode())
        full_bytes = len(json.dumps(full).encode())
        print(f'compact={compact_bytes} full={full_bytes}')
        assert compact_bytes < full_bytes


def test_compact_snapshot_includes_active_goal_without_delegation(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        collaboration = client.app.state.collaboration
        service = client.app.state.delegation
        actor = client.portal.call(collaboration.auth.resolve, tokens[lead['member_id']])
        team_id = base.rsplit('/', 1)[-1]
        compact = client.portal.call(service.compact_snapshot, team_id, actor)
        assert compact['active_goals'] == [{'id': goal, 'status': 'active', 'revision': 1}]
        assert compact['proposals'] == [] and compact['tasks'] == []
