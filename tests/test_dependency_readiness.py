from fastapi.testclient import TestClient
from test_collaboration import app, auth
from test_delegation import context, terms, ok
from test_task_results import register, publish, submit, submission, review, decision


def test_dependencies_release_once_and_already_approved_registers_ready(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        lead_id, owner_id, reviewer_id = (m['member_id'] for m in (lead, owner, reviewer))
        first = register(client, base, lead_id, tokens, goal, terms(owner_id, reviewer_id))
        second = register(client, base, lead_id, tokens, goal, terms(owner_id, reviewer_id))
        downstream = register(client, base, lead_id, tokens, goal,
                              terms(reviewer_id, lead_id, dependency_ids=[first, second]))
        def status(task):
            return ok(client.get(f'{base}/tasks/{task}', headers=auth(tokens[lead_id])))['status']
        def notifications():
            return client.portal.call(lambda: client.app.state.runtime.db.execute(
                "SELECT COUNT(*) FROM delegation_events WHERE json_extract(data, '$.id') = ? "
                "AND seq IN (SELECT event_id FROM delegation_notifications WHERE message_id IN "
                "(SELECT message_id FROM message_deliveries WHERE purpose = 'task.ready'))",
                (downstream,)).fetchone()[0])
        assert status(downstream) == 'waiting_dependencies'
        for index, task in enumerate((first, second)):
            artifact = publish(client, base, owner, [lead, reviewer], str(index).encode())
            receipt = ok(submit(client, f'{base}/tasks/{task}', tokens[owner_id], submission(artifact)))
            body = decision()
            ok(review(client, f'{base}/tasks/{task}', tokens[reviewer_id], receipt['submission_id'], body))
            ok(review(client, f'{base}/tasks/{task}', tokens[reviewer_id], receipt['submission_id'], body))
            assert notifications() == index
        assert status(downstream) == 'ready'
        later = register(client, base, lead_id, tokens, goal,
                         terms(reviewer_id, lead_id, dependency_ids=[first, second]))
        assert status(later) == 'ready'
