import asyncio
import threading

from fastapi.testclient import TestClient
from test_collaboration import OPERATOR, auth, send
from test_goal_close_runtime import ClosingModel, create_scheduled_app, prepare_goals, key, wait_until


def test_non_assignment_turn_falls_back_and_is_idempotent(tmp_path):
    class Model(ClosingModel):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
        async def complete(self, messages, tools):
            text = str(messages[-1].get('content') or '')
            if 'HOLD_ACTIVE' in text:
                self.started.set()
                assert await asyncio.to_thread(self.release.wait, 8)
                return {'role': 'assistant', 'content': 'done'}
            return await super().complete(messages, tools)
    model = Model()
    with TestClient(create_scheduled_app(tmp_path, model)) as client:
        base, lead, tokens, initial, first, second = prepare_goals(client, model)
        wait_until(lambda: not client.app.state.runtime.active.get(lead['session_id']))
        send(client, base, OPERATOR, request_id=key(), text='HOLD_ACTIVE', kind='supplement', reply_to=initial['message_id'])
        assert model.started.wait(8)
        request_id = key()
        payload = dict(request_id=request_id, text='new input', kind='supplement', reply_to=initial['message_id'], steer_active=True)
        try:
            a = send(client, base, OPERATOR, **payload)
            b = send(client, base, OPERATOR, **payload)
            assert a == b
            assert a['active_input'] == [{'member_id': lead['member_id'], 'status': 'fallback'}]
        finally:
            model.release.set()
        def status():
            return client.portal.call(lambda: client.app.state.runtime.db.execute(
                "SELECT status FROM message_deliveries WHERE message_id=?", (a['message_id'],)).fetchone()[0])
        wait_until(lambda: status() == 'completed')
        def records():
            db = client.app.state.runtime.db
            return db.execute("SELECT count(*) FROM events WHERE type='turn.input_applied' AND payload LIKE ?", ('%'+a['message_id']+'%',)).fetchone()[0], db.execute("SELECT count(*) FROM message_deliveries WHERE message_id=?", (a['message_id'],)).fetchone()[0]
        applied, deliveries = client.portal.call(records)
        assert applied == 0 and deliveries == 1


def test_active_input_rejects_non_supplement_and_non_operator(tmp_path):
    model = ClosingModel()
    with TestClient(create_scheduled_app(tmp_path, model)) as client:
        base, lead, tokens, initial, first, second = prepare_goals(client, model)
        payload = {'request_id':key(), 'text':'x', 'kind':'chat', 'steer_active':True}
        assert client.post(base+'/messages', json=payload, headers=auth(OPERATOR)).status_code in (400, 422)
        payload.update(kind='supplement', reply_to=initial['message_id'])
        assert client.post(base+'/messages', json=payload, headers=auth(tokens[lead['member_id']])).status_code in (403, 422)



def test_multi_recipient_fallback_and_goal_mismatch(tmp_path):
    model = ClosingModel()
    with TestClient(create_scheduled_app(tmp_path, model)) as client:
        base, lead, tokens, initial, first, second = prepare_goals(client, model)
        wait_until(lambda: not client.app.state.runtime.active)
        members = client.get(base, headers=auth(OPERATOR)).json()['members']
        chosen = [member['member_id'] for member in members[:2]]
        receipt = send(client, base, OPERATOR, request_id=key(), text='two targets', kind='supplement',
                       reply_to=initial['message_id'], mentions=chosen, steer_active=True)
        assert {x['member_id'] for x in receipt['active_input']} == set(chosen)
        assert all(x['status'] == 'fallback' for x in receipt['active_input'])
        response = client.post(base+'/messages', headers=auth(OPERATOR), json={
            'request_id':key(), 'text':'wrong scope', 'kind':'supplement', 'reply_to':initial['message_id'],
            'goal_id':second, 'steer_active':True})
        assert response.status_code == 422



def test_assigned_turn_queues_input_at_most_once(tmp_path):
    from test_collaboration import setup_team
    from test_goal_lifecycle import goal
    class Model(ClosingModel):
        def __init__(self):
            super().__init__(); self.started = threading.Event(); self.release = threading.Event()
        async def complete(self, messages, tools):
            text = str(messages[-1].get('content') or '')
            if 'WAIT_INITIAL' in text and not self.started.is_set():
                self.started.set(); assert await asyncio.to_thread(self.release.wait, 8)
            return {'role':'assistant', 'content':'done'}
    model = Model()
    with TestClient(create_scheduled_app(tmp_path, model)) as client:
        base, lead, others, tokens = setup_team(client)
        initial = send(client, base, OPERATOR, request_id=key(), text='WAIT_INITIAL', kind='assignment')
        assert model.started.wait(8)
        goal(client, base, tokens[lead['member_id']], initial)
        payload = dict(request_id=key(), text='boundary input', kind='supplement', reply_to=initial['message_id'], steer_active=True, mentions=[lead['member_id'], others[0]['member_id']])
        try:
            receipt = send(client, base, OPERATOR, **payload)
            assert receipt == send(client, base, OPERATOR, **payload)
            assert {x['member_id']: x['status'] for x in receipt['active_input']} == {lead['member_id']: 'queued', others[0]['member_id']: 'fallback'}
        finally:
            model.release.set()
        def counts():
            db = client.app.state.runtime.db
            return db.execute("SELECT count(*) FROM events WHERE type='turn.input_applied' AND payload LIKE ?", ('%'+receipt['message_id']+'%',)).fetchone()[0]
        wait_until(lambda: client.portal.call(counts) == 1)


def test_inflight_tool_receipt_preserved_before_active_input(tmp_path):
    import json
    from collaboration_runtime.api import create_app
    from test_collaboration import setup_team, Sandbox, bootstrap
    from test_goal_lifecycle import goal
    from test_goal_close_runtime import call
    class BlockingSandbox(Sandbox):
        def __init__(self):
            super().__init__(); self.started = threading.Event(); self.release = threading.Event(); self.calls = 0
        def run(self, *args, **kwargs):
            self.calls += 1; self.started.set()
            assert self.release.wait(8)
            return {'exit_code':0, 'stdout':'once', 'stderr':''}
    class Model:
        def __init__(self):
            self.calls = 0
            self.followup = None

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {'role':'assistant','content':None,'tool_calls':[
                    call('run_command','one',{'command':'first'}), call('run_command','two',{'command':'second'})]}
            self.followup = messages[-3:]
            return {'role':'assistant','content':'done'}
    sandbox, model = BlockingSandbox(), Model()
    with TestClient(create_app(model=model, sandbox=sandbox, state_dir=tmp_path, bootstrap=bootstrap,
                               operator_token=OPERATOR, schedule=True)) as client:
        base, lead, others, tokens = setup_team(client)
        initial = send(client, base, OPERATOR, request_id=key(), text='do work', kind='assignment')
        assert sandbox.started.wait(8)
        goal(client, base, tokens[lead['member_id']], initial)
        try:
            receipt = send(client, base, OPERATOR, request_id=key(), text='change', kind='supplement',
                           reply_to=initial['message_id'], steer_active=True)
            assert receipt['active_input'][0]['status'] == 'queued'
        finally:
            sandbox.release.set()
        def results():
            db = client.app.state.runtime.db
            rows = db.execute("SELECT call_id, result FROM execution_receipts WHERE call_id IN ('one','two') ORDER BY position").fetchall()
            return [dict(r) for r in rows] if len(rows)==2 and all(r['result'] for r in rows) else None
        rows = wait_until(lambda: client.portal.call(results))
        assert sandbox.calls == 2
        assert all(json.loads(row['result'])['ok'] for row in rows)
        wait_until(lambda: client.portal.call(lambda: client.app.state.runtime.db.execute(
            "SELECT count(*) FROM events WHERE type='turn.input_applied' AND payload LIKE ?", ('%'+receipt['message_id']+'%',)).fetchone()[0]) == 1)
        wait_until(lambda: model.followup)
        assert [message['tool_call_id'] for message in model.followup[:2]] == ['one', 'two']
        assert model.followup[2] == {'role': 'user', 'content': 'change'}


def test_structured_pending_survives_checkpoint_reopen(tmp_path):
    import sqlite3
    from collaboration_runtime.journal import ExecutionJournal
    path = tmp_path / 'checkpoint.db'
    entry = {'text':'supplement', 'message_id':'m1', 'event_id':7, 'session_id':'s1', 'team_id':'t1'}
    db = sqlite3.connect(path)
    journal = ExecutionJournal(db)
    journal.create('turn', [])
    journal.queue_input('turn', entry)
    journal.queue_input('turn', 'old plain input')
    db.commit(); db.close()
    db = sqlite3.connect(path)
    journal = ExecutionJournal(db)
    generation = journal.next_generation('turn')
    assert journal.drain_inputs('turn', 1) == []
    assert journal.drain_inputs('turn', generation) == [entry, 'old plain input']
    db.commit(); db.close()


def test_pending_supplement_resumes_same_turn_after_runtime_restart(tmp_path):
    from collaboration_runtime.local_auth import Principal
    from test_collaboration import events, setup_team
    from test_goal_lifecycle import goal

    class WaitingModel:
        def __init__(self):
            self.started = threading.Event()

        async def complete(self, messages, tools):
            self.started.set()
            await asyncio.Event().wait()

    original = WaitingModel()
    with TestClient(create_scheduled_app(tmp_path, original)) as client:
        base, lead, _, tokens = setup_team(client)
        source = send(client, base, OPERATOR, request_id=key(), text="Start", kind="assignment")
        assert original.started.wait(8)
        goal(client, base, tokens[lead["member_id"]], source)
        receipt = send(client, base, OPERATOR, request_id=key(), text="Resume with this",
                       kind="supplement", reply_to=source["message_id"], steer_active=True)
        assert receipt["active_input"][0]["status"] == "queued"

    class ResumingModel:
        def __init__(self):
            self.inputs = []

        async def complete(self, messages, tools):
            self.inputs.append(messages[-1]["content"])
            return {"role": "assistant", "content": "Resumed"}

    resumed = ResumingModel()
    with TestClient(create_scheduled_app(tmp_path, resumed)) as client:
        cursor = client.portal.call(client.app.state.collaboration.cursor, base.rsplit("/", 1)[-1],
                                    Principal("local-operator"), 0)

        def applied():
            return [data["message"] for kind, data, _ in events(client, base, OPERATOR, after=cursor)
                    if kind == "supplement.status" and data["message"]["id"] == receipt["message_id"]
                    and data["message"]["active_input"][0]["status"] == "applied"]

        wait_until(applied)
        wait_until(lambda: resumed.inputs)
        assert resumed.inputs == ["Resume with this"]
        assert len(applied()) == 1
        status = client.get(f"{base}/messages/{receipt['message_id']}/deliveries", headers=auth(OPERATOR)).json()
        assert len(status) == 1 and status[0]["status"] == "completed"


def test_goal_cancel_event_reports_queued_input_cancelled(tmp_path):
    from test_collaboration import setup_team
    from test_goal_lifecycle import goal, terminate
    class Model(ClosingModel):
        def __init__(self):
            super().__init__(); self.started = threading.Event(); self.release = threading.Event()
        async def complete(self, messages, tools):
            if 'WAIT_CANCEL' in str(messages[-1].get('content') or '') and not self.started.is_set():
                self.started.set(); await asyncio.to_thread(self.release.wait, 8)
            return {'role':'assistant','content':'done'}
    model = Model()
    with TestClient(create_scheduled_app(tmp_path, model)) as client:
        base, lead, others, tokens = setup_team(client)
        source = send(client, base, OPERATOR, request_id=key(), text='WAIT_CANCEL', kind='assignment')
        assert model.started.wait(8)
        gid = goal(client, base, tokens[lead['member_id']], source)['goal_id']
        receipt = send(client, base, OPERATOR, request_id=key(), text='pending', kind='supplement',
                       reply_to=source['message_id'], steer_active=True)
        try:
            response = terminate(client, base, OPERATOR, gid, 1, 'cancelled', reason='test')
            assert response.status_code == 201, response.text
            def events():
                service = client.app.state.collaboration
                from collaboration_runtime.local_auth import Principal
                actor = Principal('local-operator')
                return service.events(base.rsplit('/',1)[-1], actor, 0)[0]
            terminal = [data for typ, seq, data in client.portal.call(events) if typ == 'goal.cancelled'][-1]
            assert terminal['supplement_statuses'] == [{'id':receipt['message_id'], 'active_input':[
                {'member_id':lead['member_id'], 'status':'cancelled'}]}]
            late = client.post(f"{base}/messages", headers=auth(OPERATOR), json={
                "request_id": key(), "text": "late", "kind": "supplement",
                "reply_to": source["message_id"], "steer_active": True,
            })
            assert late.status_code == 409
        finally:
            model.release.set()
