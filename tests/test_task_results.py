import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from fastapi.testclient import TestClient

from test_collaboration import app, auth, events, snapshot
from test_delegation import action, confirm_all, context, create, key, ok, terms


def register(client, base, dispatcher, tokens, goal, value, **revision):
    proposal = create(client, base, tokens[dispatcher], goal, value, **revision)['proposal_id']
    confirm_all(client, proposal, {dispatcher: tokens[dispatcher], **tokens})
    return ok(action(client, proposal, tokens[dispatcher], 'update' if revision else 'assign'))['task_id']


def publish(client, base, member, readers, content, artifact_id=None):
    return client.portal.call(partial(
        client.app.state.workspace.artifacts.publish, team_id=base.rsplit('/', 1)[-1],
        author_session_id=member['session_id'], source_path='result.txt', content=content,
        reader_session_ids=[reader['session_id'] for reader in readers], artifact_id=artifact_id,
    ))


def submission(version, revision=1):
    return {'request_id': key(), 'expected_revision': revision, 'version_ids': [version['version_id']],
            'evidence': ['deterministic check output'], 'open_issues': []}


def decision(outcome='approved', revision=1):
    return {'request_id': key(), 'expected_revision': revision, 'decision': outcome,
            'findings': [{'criterion': 1, 'met': outcome == 'approved', 'evidence': 'Observed check output'}],
            **({'expected': 'All checks pass', 'recheck_items': ['Repeat the failed check']}
               if outcome == 'returned' else {})}


def submit(client, task, token, body):
    return client.post(f'{task}/submissions', headers=auth(token), json=body)


def review(client, task, token, submission_id, body):
    return client.post(f'{task}/submissions/{submission_id}/review', headers=auth(token), json=body)


async def current_acceptance(client, base, reader, goal, task_id):
    actor = client.app.state.auth.member(reader['session_id'])
    return client.app.state.delegation.results.current_acceptance(base.rsplit('/', 1)[-1], actor, goal, task_id)


def test_return_resubmit_and_invalidate_only_pinned_input(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer, outsider = others[:3]
        lead_id, owner_id, reviewer_id, outsider_id = [member['member_id'] for member in (lead, owner, reviewer, outsider)]
        upstream = register(client, base, reviewer_id, tokens, goal, terms(owner_id, reviewer_id))
        task = f'{base}/tasks/{upstream}'
        v1 = publish(client, base, owner, [lead, reviewer], b'old content')
        v2 = publish(client, base, owner, [lead, reviewer], b'revised content', v1['artifact_id'])
        # This consumer works from an explicitly pinned candidate before its review.
        # Formal approval dependencies are covered separately in test_dependency_readiness.
        pinned = terms(reviewer_id, lead_id, input_version_ids=[v1['version_id']])
        downstream = register(client, base, lead_id, tokens, goal, pinned)
        independent = register(client, base, lead_id, tokens, goal, terms(reviewer_id, lead_id))
        for task_id in (downstream, independent):
            endpoint = f'{base}/tasks/{task_id}'
            material = publish(client, base, reviewer, [lead], task_id.encode())
            result = ok(submit(client, endpoint, tokens[reviewer_id], submission(material)))
            ok(review(client, endpoint, tokens[lead_id], result['submission_id'], decision()))
            assert client.portal.call(current_acceptance, client, base, lead, goal, task_id)

        lead_cursor = snapshot(client, base, tokens[lead_id])['cursor']
        outsider_cursor = snapshot(client, base, tokens[outsider_id])['cursor']
        body = submission(v1)
        assert submit(client, task, tokens[reviewer_id], body).status_code == 403
        assert submit(client, task, tokens[outsider_id], body).status_code == 404
        assert submit(client, task, tokens[owner_id], submission(v1, 2)).status_code == 409
        private = publish(client, base, owner, [], b'not shared with reviewer')
        assert submit(client, task, tokens[owner_id], submission(private)).status_code == 403
        first = ok(submit(client, task, tokens[owner_id], body))
        assert ok(submit(client, task, tokens[owner_id], body)) == first
        assert len(first['notification_message_ids']) == 2
        assert review(client, task, tokens[owner_id], first['submission_id'], decision()).status_code == 403
        invalid = {**decision('returned'), 'recheck_items': []}
        assert review(client, task, tokens[reviewer_id], first['submission_id'], invalid).status_code == 422
        returned = decision('returned')
        return_receipt = ok(review(client, task, tokens[reviewer_id], first['submission_id'], returned))
        assert ok(review(client, task, tokens[reviewer_id], first['submission_id'], returned)) == return_receipt
        assert submit(client, task, tokens[owner_id], submission(v1)).status_code == 409
        second = ok(submit(client, task, tokens[owner_id], submission(v2)))
        assert review(client, task, tokens[reviewer_id], first['submission_id'], decision()).status_code == 409
        assert client.portal.call(current_acceptance, client, base, lead, goal, upstream) is None
        assert client.portal.call(current_acceptance, client, base, lead, goal, downstream) is None
        assert client.portal.call(current_acceptance, client, base, lead, goal, independent)

        affected_endpoint = f'{base}/tasks/{downstream}'
        affected = ok(client.get(affected_endpoint, headers=auth(tokens[reviewer_id])))
        assert affected['status'] == 'needs_recheck'
        assert [version['version_id'] for version in affected['result']['stale_input_versions']] == [v1['version_id']]
        assert affected['result']['submissions'][0]['review']['decision'] == 'approved'
        replacement = publish(client, base, reviewer, [lead], b'downstream replacement')
        assert submit(client, affected_endpoint, tokens[reviewer_id], submission(replacement)).status_code == 409
        approved = ok(review(client, task, tokens[reviewer_id], second['submission_id'], decision()))
        fact = client.portal.call(current_acceptance, client, base, lead, goal, upstream)
        assert fact['review']['id'] == approved['review_id']
        assert [version['version_id'] for version in fact['versions']] == [v2['version_id']]

        assert client.post('/v1/auth/login', json={'token': tokens[lead_id]}).status_code == 204
        assert 'task-results' in client.get('/').text
        old, new = ok(client.get(task))['result']['submissions']
        assert old['review']['id'] == return_receipt['review_id']
        assert old['review']['findings'][0]['evidence'] == 'Observed check output'
        assert new['previous_return_id'] == old['review']['id']
        assert new['review']['decision'] == 'approved'
        assert client.get(old['versions'][0]['download_url']).content == b'old content'
        assert client.get(new['versions'][0]['download_url']).content == b'revised content'
        assert client.get(old['versions'][0]['download_url'], headers=auth(tokens[outsider_id])).status_code == 404
        assert client.get(task, headers=auth(tokens[outsider_id])).status_code == 404
        assert events(client, base, tokens[outsider_id], after=outsider_cursor) == []
        lead_events = events(client, base, tokens[lead_id], after=lead_cursor)
        assert any(kind == 'task.needs_recheck' and data['task']['id'] == downstream for kind, data, _ in lead_events)
        assert any(kind == 'task.returned' and data['result_reference'] == {
            'submission_id': first['submission_id'], 'review_id': return_receipt['review_id'],
        } for kind, data, _ in lead_events)
        for recipient in (lead_id, reviewer_id):
            delivered = ok(client.get(f'{base}/deliveries', headers=auth(tokens[recipient])))
            assert sum(item['message_id'] in first['notification_message_ids'] for item in delivered) == 1

        register(client, base, lead_id, tokens, goal, {**pinned, 'input_version_ids': [v2['version_id']]},
                 task_id=downstream, base_revision=1)
        assert client.portal.call(current_acceptance, client, base, lead, goal, downstream) is None
        updated = ok(submit(client, affected_endpoint, tokens[reviewer_id], submission(replacement, 2)))
        ok(review(client, affected_endpoint, tokens[lead_id], updated['submission_id'], decision(revision=2)))
        assert client.portal.call(current_acceptance, client, base, lead, goal, downstream)['task_revision'] == 2


def test_model_tools_and_conflicting_review_survive_restart(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer, dispatcher = others[:3]
        lead_id, owner_id, reviewer_id, dispatcher_id = [member['member_id'] for member in (lead, owner, reviewer, dispatcher)]
        value = terms(owner_id, reviewer_id)
        task_id = register(client, base, dispatcher_id, tokens, goal, value)
        endpoint = f'{base}/tasks/{task_id}'
        artifact = publish(client, base, owner, [reviewer], b'reviewer-only output')
        cursor = snapshot(client, base, tokens[lead_id])['cursor']
        tools = client.app.state.collaboration.delegation_tools
        visible_tools = client.portal.call(tools.tools_for, owner['session_id'])
        assert {'submit_result', 'review_result'} <= {item['function']['name'] for item in visible_tools}
        body = submission(artifact)
        submitted = client.portal.call(partial(tools.execute, owner['session_id'], 'submit_result',
                                              json.dumps({**body, 'task_id': task_id})))
        assert submitted['ok'], submitted
        receipt = submitted['result']
        submission_id = receipt['submission_id']
        assert len(receipt['notification_message_ids']) == 3
        assert ok(submit(client, endpoint, tokens[owner_id], body)) == receipt
        assert submit(client, endpoint, tokens[owner_id], {**body, 'evidence': ['changed']}).status_code == 409
        for reader in (lead_id, dispatcher_id):
            view = ok(client.get(endpoint, headers=auth(tokens[reader])))
            assert view['status'] == 'pending_review' and view['result']['submissions'] == []
        lead_event = next(data for kind, data, _ in events(client, base, tokens[lead_id], after=cursor)
                          if kind == 'task.submitted')
        assert 'result_reference' not in lead_event
        with ThreadPoolExecutor(max_workers=2) as workers:
            attempts = [workers.submit(review, client, endpoint, tokens[reviewer_id], submission_id, decision())
                        for _ in range(2)]
        assert sorted(attempt.result().status_code for attempt in attempts) == [201, 409]
        assert client.portal.call(current_acceptance, client, base, reviewer, goal, task_id)
        assert client.portal.call(current_acceptance, client, base, lead, goal, task_id) is None
        register(client, base, dispatcher_id, tokens, goal, {**value, 'deliverables': ['updated']},
                 task_id=task_id, base_revision=1)
        assert review(client, endpoint, tokens[reviewer_id], submission_id, decision()).status_code == 409
        assert submit(client, endpoint, tokens[owner_id], submission(artifact)).status_code == 409

    with TestClient(app(tmp_path)) as recovered:
        history = ok(recovered.get(endpoint, headers=auth(tokens[reviewer_id]), params={'revision': 1}))['result']
        assert history['current_submission_id'] is None
        assert history['submissions'][0]['review']['decision'] == 'approved'
        assert recovered.portal.call(current_acceptance, recovered, base, reviewer, goal, task_id) is None
        updated = ok(submit(recovered, endpoint, tokens[owner_id], submission(artifact, 2)))
        approved = recovered.portal.call(partial(recovered.app.state.collaboration.delegation_tools.execute,
                                              reviewer['session_id'], 'review_result', json.dumps({
                                                  **decision(revision=2), 'task_id': task_id,
                                                  'submission_id': updated['submission_id'],
                                              })))
        assert approved['ok'], approved
        (tmp_path / 'team-artifacts' / artifact['version_id'] / 'artifact').write_bytes(b'damaged')
        assert recovered.portal.call(current_acceptance, recovered, base, reviewer, goal, task_id) is None


def test_review_delivery_contains_readable_pinned_submission_and_criteria(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        task_id = register(client, base, lead['member_id'], tokens, goal,
                           terms(owner['member_id'], reviewer['member_id']))
        endpoint = f'{base}/tasks/{task_id}'
        version = publish(client, base, owner, [reviewer], b'review evidence')
        result = ok(submit(client, endpoint, tokens[owner['member_id']], submission(version)))
        state = client.app.state
        reviewer_actor = client.portal.call(state.auth.member, reviewer['session_id'])
        owner_actor = client.portal.call(state.auth.member, owner['session_id'])

        def contexts():
            return [state.delegation.execution_context(message_id, reviewer_actor)
                    for message_id in result['notification_message_ids']]

        contexts_for_reviewer = client.portal.call(contexts)
        review_context = next(item for item in contexts_for_reviewer if item and 'review_request' in item)
        assert review_context['review_request']['submission']['id'] == result['submission_id']
        assert review_context['review_request']['submission']['versions'][0]['version_id'] == version['version_id']
        assert review_context['task']['terms']['acceptance_criteria']
        assert 'review_result' in review_context['review_request']['instruction']
        assert all('review_request' not in (client.portal.call(state.delegation.execution_context, message_id, owner_actor) or {})
                   for message_id in result['notification_message_ids'])

        ok(review(client, endpoint, tokens[reviewer['member_id']], result['submission_id'], decision()))
        assert all('review_request' not in (item or {}) for item in client.portal.call(contexts))


def test_old_revision_submission_does_not_request_review_after_revision(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        lead_id, owner_id, reviewer_id = [member['member_id'] for member in (lead, owner, reviewer)]
        task_id = register(client, base, lead_id, tokens, goal, terms(owner_id, reviewer_id))
        version = publish(client, base, owner, [reviewer], b'old result')
        result = ok(submit(client, f'{base}/tasks/{task_id}', tokens[owner_id], submission(version)))
        state = client.app.state
        actor = client.portal.call(state.auth.member, reviewer['session_id'])

        def review_contexts():
            return [state.delegation.execution_context(message_id, actor)
                    for message_id in result['notification_message_ids']]

        assert any('review_request' in (item or {}) for item in client.portal.call(review_contexts))
        register(client, base, lead_id, tokens, goal, terms(owner_id, reviewer_id),
                 task_id=task_id, base_revision=1)
        assert all('review_request' not in (item or {}) for item in client.portal.call(review_contexts))
