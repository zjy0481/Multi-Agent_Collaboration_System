from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.scheduler import DeliveryScheduler
from test_collaboration import app, bootstrap, OPERATOR, Sandbox, send, setup_team
from test_goal_lifecycle import assignment, goal, key, terminate


def test_goal_scoped_agent_updates_close_and_new_assignment_precedes_unscoped_chat(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, members, tokens = setup_team(client)
        first = assignment(client, base, 'First assignment')
        first_goal = goal(client, base, tokens[lead['member_id']], first)
        scoped = send(client, base, tokens[members[0]['member_id']], request_id=key(),
                      text='First task update', mentions=[lead['member_id']], goal_id=first_goal['goal_id'])
        independent = send(client, base, tokens[members[0]['member_id']], request_id=key(),
                           text='Independent chat', mentions=[lead['member_id']])
        assert terminate(client, base, OPERATOR, first_goal['goal_id'], 1, 'cancelled').status_code == 201
        second = assignment(client, base, 'Second assignment')
        state = client.app.state
        scheduler = DeliveryScheduler(state.runtime, state.collaboration, state.workspace)

        def observe():
            statuses = dict(state.runtime.db.execute('SELECT message_id, status FROM message_deliveries WHERE message_id IN (?, ?, ?) AND session_id=?',
                (scoped['message_id'], independent['message_id'], second['message_id'], lead['session_id'])))
            _, turn, _, _ = scheduler._claim_one()
            claimed = state.runtime.db.execute('SELECT d.message_id FROM scheduled_turns s JOIN message_deliveries d USING (event_id, session_id, purpose) WHERE s.turn_id=?', (turn,)).fetchone()['message_id']
            return statuses, claimed

        statuses, claimed = client.portal.call(observe)
        assert statuses == {scoped['message_id']: 'cancelled', independent['message_id']: 'pending', second['message_id']: 'pending'}
        assert claimed == second['message_id']


def test_tool_message_inherits_scope_of_scheduled_work(tmp_path):
    import json

    with TestClient(app(tmp_path)) as client:
        base, lead, members, tokens = setup_team(client)
        source = assignment(client, base, 'Work to perform')
        registered = goal(client, base, tokens[lead['member_id']], source)
        service = client.app.state.collaboration
        def post():
            return service.execute_tool(lead['session_id'], 'send_message', json.dumps({
                'request_id': key(), 'text': 'Working on the assignment',
                'mentions': [members[0]['member_id']],
            }), work_message_id=source['message_id'])
        result = client.portal.call(post)
        assert result['ok']
        assert terminate(client, base, OPERATOR, registered['goal_id'], 1, 'cancelled').status_code == 201
        def status():
            return service.db.execute('SELECT status FROM message_deliveries WHERE message_id=?',
                                      (result['result']['message_id'],)).fetchone()['status']
        assert client.portal.call(status) == 'cancelled'


def test_goal_delivery_retains_its_history_and_excludes_previous_assignment(tmp_path):
    marker = 'previous assignment turn result'
    inputs = []

    class Model:
        async def complete(self, messages, tools):
            inputs.append(messages)
            text = marker if len(inputs) == 1 else 'handled current delivery'
            return {'role': 'assistant', 'content': text}

    application = create_app(model=Model(), sandbox=Sandbox(), state_dir=tmp_path,
                             bootstrap=bootstrap, operator_token=OPERATOR, schedule=False)
    with TestClient(application) as client:
        base, lead, _, tokens = setup_team(client)
        state = client.app.state
        scheduler = DeliveryScheduler(state.runtime, state.collaboration, state.workspace)

        async def handle_next_delivery():
            item = scheduler._claim_one()
            assert item[0] == lead['session_id']
            state.runtime.launch(*item)
            await state.runtime.active[item[0]].task

        first = assignment(client, base, 'First assignment')
        first_goal = goal(client, base, tokens[lead['member_id']], first)
        client.portal.call(handle_next_delivery)
        send(client, base, OPERATOR, request_id=key(), text='First assignment follow-up',
             kind='supplement', reply_to=first['message_id'])
        client.portal.call(handle_next_delivery)
        assert any(message['content'] == marker for message in inputs[1])

        assert terminate(client, base, OPERATOR, first_goal['goal_id'], 1, 'cancelled').status_code == 201
        assignment(client, base, 'Second assignment')
        client.portal.call(handle_next_delivery)
        assert all(message['content'] != marker for message in inputs[2])


def test_conflicting_reply_and_current_work_goal_are_rejected(tmp_path):
    import json
    from test_collaboration import auth

    with TestClient(app(tmp_path)) as client:
        base, lead, members, tokens = setup_team(client)
        first = assignment(client, base, 'First goal')
        first_goal = goal(client, base, tokens[lead['member_id']], first)
        second = assignment(client, base, 'Second goal')
        second_goal = goal(client, base, tokens[lead['member_id']], second)
        response = client.post(f'{base}/messages', headers=auth(tokens[lead['member_id']]), json={
            'request_id': key(), 'text': 'Conflicting reply', 'reply_to': first['message_id'],
            'goal_id': second_goal['goal_id'], 'mentions': [members[0]['member_id']],
        })
        assert response.status_code == 422
        assert 'same goal' in response.text
        def execute(goal_id, reply_to=None):
            args = {'request_id': key(), 'text': 'Conflicting work', 'goal_id': goal_id,
                    'mentions': [members[0]['member_id']]}
            if reply_to:
                args['reply_to'] = reply_to
            return client.app.state.collaboration.execute_tool(
                lead['session_id'], 'send_message', json.dumps(args), work_message_id=first['message_id'])
        result = client.portal.call(execute, second_goal['goal_id'])
        assert result['ok'] is False and result['status'] == 422
        result = client.portal.call(execute, first_goal['goal_id'], second['message_id'])
        assert result['ok'] is False and result['status'] == 422


def test_scheduled_send_message_inherits_goal_and_is_cancelled(tmp_path):
    import json

    class Model:
        sent = False
        recipient = None

        async def complete(self, messages, tools):
            if not self.sent:
                self.sent = True
                return {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'send', 'type': 'function', 'function': {
                        'name': 'send_message', 'arguments': json.dumps({
                            'request_id': key(), 'text': 'Scheduled progress',
                            'mentions': [self.recipient],
                        }),
                    },
                }]}
            return {'role': 'assistant', 'content': 'finished'}

    model = Model()
    application = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path,
                             bootstrap=bootstrap, operator_token=OPERATOR, schedule=False)
    with TestClient(application) as client:
        base, lead, members, tokens = setup_team(client)
        model.recipient = members[0]['member_id']
        state = client.app.state
        scheduler = DeliveryScheduler(state.runtime, state.collaboration, state.workspace)
        first = assignment(client, base, 'First goal')
        registered = goal(client, base, tokens[lead['member_id']], first)

        async def run_first():
            item = scheduler._claim_one()
            assert item[0] == lead['session_id']
            state.runtime.launch(*item)
            await state.runtime.active[item[0]].task

        client.portal.call(run_first)

        def inspect():
            db = state.collaboration.db
            row = db.execute('SELECT id, team_id FROM team_messages WHERE text=?',
                             ('Scheduled progress',)).fetchone()
            assert row is not None
            status = db.execute('SELECT status FROM message_deliveries WHERE message_id=?',
                                (row['id'],)).fetchone()['status']
            return row['id'], state.collaboration.goal_for_message(row['team_id'], row['id']), status

        message_id, scope, status = client.portal.call(inspect)
        assert scope == registered['goal_id']
        assert status == 'pending'
        second = assignment(client, base, 'Second goal')
        assert terminate(client, base, OPERATOR, registered['goal_id'], 1, 'cancelled').status_code == 201

        def statuses():
            return dict(state.collaboration.db.execute(
                'SELECT message_id, status FROM message_deliveries WHERE message_id IN (?, ?)',
                (message_id, second['message_id'])))

        assert client.portal.call(statuses) == {message_id: 'cancelled', second['message_id']: 'pending'}
