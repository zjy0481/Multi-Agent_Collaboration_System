import pytest

from collaboration_runtime.teams import TeamConflict
from test_collaboration import auth
from test_delegation import key, ok
from test_forks import fork_delivery, post_fork_delivery
from test_task_results import decision, publish, review, submission, submit


@pytest.mark.parametrize('phase', ['pending_review', 'returned', 'accepted'])
@pytest.mark.parametrize('boundary', ['new_fork', 'assistant_message', 'queued_job'])
def test_task_result_acceptance_ends_fork_scope(fork_delivery, phase, boundary):
    (client, runtime, scheduler, model, drain, base, lead_token, owner, tokens,
     goal, task_id, task_terms, helper, input_version, _) = fork_delivery
    fork = client.portal.call(runtime.forks.get, helper)
    team = ok(client.get(base, headers=auth(lead_token)))
    reviewer = next(member for member in team['members'] if member['member_id'] == task_terms['reviewer_id'])
    owner_token = tokens[owner['member_id']]
    reviewer_token = tokens[reviewer['member_id']]
    endpoint = f'{base}/tasks/{task_id}'

    queued = None
    if boundary == 'queued_job':
        queued = ok(client.post(f'/v1/forks/{helper}/messages', headers=auth(owner_token), json={
            'request_id': key(), 'text': 'Queued before the result decision', 'version_ids': [],
        }))

    first_version = publish(client, base, owner, [reviewer], b'first reviewed result')
    first = ok(submit(client, endpoint, owner_token, submission(first_version)))
    if phase in {'returned', 'accepted'}:
        ok(review(client, endpoint, reviewer_token, first['submission_id'], decision('returned')))
    if phase == 'accepted':
        final_version = publish(client, base, owner, [reviewer], b'revised reviewed result', first_version['artifact_id'])
        latest = ok(submit(client, endpoint, owner_token, submission(final_version)))
        ok(review(client, endpoint, reviewer_token, latest['submission_id'], decision()))
        download = f"/v1/artifacts/{final_version['artifact_id']}/versions/{final_version['version']}"
        response = client.get(download, headers=auth(reviewer_token))
        assert response.status_code == 200 and response.content == b'revised reviewed result'
    assert ok(client.get(endpoint, headers=auth(lead_token)))['status'] == phase

    if boundary == 'new_fork':
        same_scope = {'request_id': key(), 'from_turn_id': fork['from_turn_id'], 'goal_id': goal,
                      'task_id': task_id, 'expected_revision': 1, 'purpose': 'another task assistant'}
        response = client.post(f"/v1/sessions/{fork['parent_session_id']}/forks", headers=auth(lead_token), json=same_scope)
        assert response.status_code == (409 if phase == 'accepted' else 201), response.text
        if phase == 'accepted':
            assert response.json()['detail'] == 'Task is no longer active'
    elif boundary == 'assistant_message':
        if phase == 'accepted':
            with pytest.raises(TeamConflict, match='Task is no longer active'):
                post_fork_delivery(client, runtime, helper, owner, input_version['version_id'])
        else:
            assert post_fork_delivery(client, runtime, helper, owner, input_version['version_id'])['message_id']
    else:
        client.portal.call(drain, scheduler._claim_assistant)
        job = client.portal.call(lambda: runtime.db.execute("""
            SELECT status, turn_id FROM assistant_jobs
            WHERE session_id = ? AND json_extract(input, '$.message_id') = ?
        """, (helper, queued['message_id'])).fetchone())
        if phase == 'accepted':
            assert (job['status'], job['turn_id'], model.inputs) == ('cancelled', None, [])
        else:
            assert job['status'] == 'completed' and job['turn_id'] is not None
            assert len(model.inputs) == 1 and queued['message_id'] in model.inputs[0]
