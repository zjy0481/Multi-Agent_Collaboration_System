"""The Lead's initial assignment can hand off to their own ready task."""
import json

from fastapi.testclient import TestClient

from test_collaboration import app
from test_delegation import confirm_all, context, create, terms
from test_delivery_priority import run_one, scheduler
from test_scheduler import current_delivery, tool


def test_assignment_yields_to_own_formal_task_after_complete_batch(tmp_path):
    class Model:
        def __init__(self):
            self.order = []

        async def complete(self, messages, tools):
            payload = current_delivery(messages)
            purpose = payload['delivery']['purpose']
            if purpose == 'task.assigned':
                self.order.append('formal')
                return {'role': 'assistant', 'content': 'Formal task handled'}
            if messages[-1]['role'] == 'user':
                decision = tool('assign_task', 'assign', {
                    'request_id': 'assign-own', 'proposal_id': proposal, 'expected_version': 1,
                })
                decision['tool_calls'] += tool('list_participants', 'same-batch', {})['tool_calls']
                return decision
            assert [m.get('tool_call_id') for m in messages[-2:]] == ['assign', 'same-batch']
            assert 'resumed after your own formal task' in messages[0]['content']
            self.order.append('assignment resumed')
            return {'role': 'assistant', 'content': 'Done'}

    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal_id = context(client)
        proposal = create(client, base, tokens[lead['member_id']], goal_id,
                          terms(lead['member_id'], others[0]['member_id']))['proposal_id']
        confirm_all(client, proposal, tokens)
        queue = scheduler(client)
        model = Model()
        queue.runtime.model = model
        initial = client.portal.call(run_one, queue)
        assert client.portal.call(lambda: queue.db.execute(
            "SELECT status FROM turns WHERE id=?", (initial,)).fetchone()[0]) == 'yielded'
        formal = client.portal.call(run_one, queue)
        assert formal != initial and model.order == ['formal']
        resumed = client.portal.call(run_one, queue)
        assert resumed == initial and model.order == ['formal', 'assignment resumed']
        assert client.portal.call(lambda: queue.db.execute(
            "SELECT count(*) FROM execution_receipts WHERE turn_id=?", (initial,)).fetchone()[0]) == 2


def test_assignment_does_not_yield_to_blocked_own_task(tmp_path):
    from test_delegation import action, ok

    class Model:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return tool('list_participants', 'check', {})
            return {'role': 'assistant', 'content': 'Waiting for the prerequisite'}

    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal_id = context(client)
        token = tokens[lead['member_id']]
        upstream = create(client, base, token, goal_id,
                          terms(others[0]['member_id'], lead['member_id']))['proposal_id']
        confirm_all(client, upstream, tokens)
        upstream_id = ok(action(client, upstream, token, 'assign'))['task_id']
        downstream = create(client, base, token, goal_id,
                            terms(lead['member_id'], others[0]['member_id'], dependency_ids=[upstream_id]))['proposal_id']
        confirm_all(client, downstream, tokens)
        ok(action(client, downstream, token, 'assign'))
        queue = scheduler(client)
        queue.runtime.model = Model()
        initial = client.portal.call(run_one, queue)
        status, yielded = client.portal.call(lambda: (
            queue.db.execute('SELECT status FROM turns WHERE id=?', (initial,)).fetchone()[0],
            queue.db.execute("SELECT count(*) FROM events WHERE turn_id=? AND type='turn.delivery_yielded'", (initial,)).fetchone()[0],
        ))
        assert status == 'completed' and yielded == 0
