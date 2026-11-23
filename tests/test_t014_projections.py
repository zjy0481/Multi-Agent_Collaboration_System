"""T014: compare the same evidence across participant projections and recovery."""
import json
from functools import partial

import httpx
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.search import SearchClient
from test_collaboration import OPERATOR, auth, bootstrap, events, send, setup_team, snapshot
from test_workspace import MemorySandbox
from test_scheduler import current_delivery, wait_until
from test_experience import key, ok, record, second_goal, activate_goal, retrieve


def test_cross_participant_projection_and_recovery(tmp_path):
    allowed, restricted = 'T014_ALLOWED_EVIDENCE', 'T014_RESTRICTED_EVIDENCE'
    sandbox = MemorySandbox()

    class RecordingModel:
        def __init__(self):
            self.inputs = []

        async def complete(self, messages, tools):
            self.inputs.append(json.loads(json.dumps(messages)))
            return {'role': 'assistant', 'content': 'Recorded.'}

    model = RecordingModel()

    def search_result(request):
        marker = restricted if request.url.params['q'] == 'restricted evidence' else allowed
        return httpx.Response(200, json={'results': [
            {'title': marker, 'url': 'https://example.org/evidence', 'content': marker, 'engine': 'fixture'},
        ]})

    def application():
        search = SearchClient('https://search.example.test/search', transport=httpx.MockTransport(
            search_result,
        ))
        return create_app(model=model, sandbox=sandbox, state_dir=tmp_path, bootstrap=bootstrap,
                          operator_token=OPERATOR, search=search, schedule=True)

    with TestClient(application()) as client:
        base, lead, others, tokens = setup_team(client)
        recipient, outsider = others[:2]
        recipient_token, outsider_token = tokens[recipient['member_id']], tokens[outsider['member_id']]
        cursors = {token: snapshot(client, base, token)['cursor'] for token in (recipient_token, outsider_token)}
        assignment = send(client, base, OPERATOR, request_id='t014-assignment', text='Review evidence', kind='assignment')
        goal = ok(client.post(f'{base}/goals', headers=auth(tokens[lead['member_id']]), json={
            'request_id': key(), 'source_message_id': assignment['message_id']}))['goal_id']
        private = send(client, base, OPERATOR, request_id='t014-private', text=restricted,
                       recipient_id=recipient['member_id'], reply_to=assignment['message_id'])
        public = send(client, base, OPERATOR, request_id='t014-public', text=allowed,
                      mentions=[recipient['member_id'], outsider['member_id']], reply_to=assignment['message_id'])
        wait_until(lambda: sum(current_delivery(x)['message']['text'] == allowed for x in model.inputs) >= 2)
        wait_until(lambda: any(current_delivery(x)['message']['text'] == restricted for x in model.inputs))
        private_input = next(x for x in model.inputs if current_delivery(x)['message']['text'] == restricted)
        assert current_delivery(private_input)['delivery']['session_id'] == recipient['session_id']
        inputs = [x for x in model.inputs if current_delivery(x)['message']['text'] == allowed]
        for model_input in inputs:
            assert allowed in json.dumps(model_input)
        outsider_input = next(x for x in inputs if current_delivery(x)['delivery']['session_id'] == outsider['session_id'])
        assert restricted not in json.dumps(outsider_input)
        for token, permitted in ((recipient_token, True), (outsider_token, False)):
            response = client.get(f'{base}/evaluation/report', headers=auth(token),
                                  params={'assignment_ids': assignment['message_id']})
            report = json.dumps(ok(response))
            assert allowed in report
            assert (restricted in report) is permitted
        for token, can_read_private in ((recipient_token, True), (outsider_token, False)):
            projected = json.dumps(snapshot(client, base, token))
            stream = json.dumps(events(client, base, token, after=cursors[token]))
            for projection in (projected, stream):
                assert allowed in projection
                assert (restricted in projection) is can_read_private
            response = client.get(f"{base}/messages/{private['message_id']}", headers=auth(token))
            assert response.status_code == (200 if can_read_private else 404)

        store = client.app.state.workspace.artifacts
        team_id = base.rsplit('/', 1)[-1]
        def publish(content, readers):
            return client.portal.call(partial(store.publish, team_id=team_id,
                author_session_id=lead['session_id'], source_path=content + '.txt',
                content=content.encode(), reader_session_ids=[m['session_id'] for m in readers]))
        visible = publish(allowed, [recipient, outsider])
        hidden = publish(restricted, [recipient])
        workspace = client.app.state.workspace
        def call(member, tool, **args):
            return client.portal.call(workspace.execute, member['session_id'], tool, json.dumps(args))
        for member, can_read_private in ((recipient, True), (outsider, False)):
            assert call(member, 'read_artifact', version_id=visible['version_id'])['result']['content'] == allowed
            result = call(member, 'read_artifact', version_id=hidden['version_id'])
            assert result['ok'] is can_read_private
            if can_read_private:
                assert result['result']['content'] == restricted
        # Handoffs carry lessons only to recipients who can read their source versions.
        lead_token = tokens[lead['member_id']]
        next_goal = second_goal(client, base, lead, lead_token)
        lessons = []
        for marker, source in ((allowed, visible), (restricted, hidden)):
            payload = record(goal, source)
            payload.update(title=marker, text=marker)
            lessons.append(ok(client.post(f'{base}/lessons', headers=auth(lead_token), json=payload))['lesson_id'])
        for member, lesson_ids, versions in ((recipient, lessons, [visible, hidden]),
                                              (outsider, lessons[:1], [visible])):
            ok(client.post(f'{base}/lesson-handoffs', headers=auth(lead_token), json={
                'request_id': key(), 'source_goal_id': goal, 'target_goal_id': next_goal,
                'lesson_ids': lesson_ids, 'artifact_version_ids': [v['version_id'] for v in versions],
                'recipient_id': member['member_id'], 'purpose': 'Continue with the shared evidence.'}))
        activate_goal(client, base, lead_token, goal, next_goal)
        for token, permitted in ((recipient_token, True), (outsider_token, False)):
            evidence = json.dumps(ok(retrieve(client, base, token, next_goal)))
            assert allowed in evidence
            assert (restricted in evidence) is permitted

        shared_search = call(outsider, 'search_web', query='shared evidence', limit=1)
        assert shared_search['ok'] and allowed in json.dumps(shared_search['result'])
        found = call(recipient, 'search_web', query='restricted evidence', limit=1)
        assert found['ok'] and restricted in json.dumps(found['result'])
        search_id = found['result']['search_id']
        assert call(recipient, 'get_search_result', search_id=search_id)['result'] == found['result']
        assert not call(outsider, 'get_search_result', search_id=search_id)['ok']

    # Durable messages and events retain the same participant-specific projection.
    with TestClient(application()) as client:
        for token, can_read_private in ((recipient_token, True), (outsider_token, False)):
            for projection in (snapshot(client, base, token), events(client, base, token, after=cursors[token])):
                serialized = json.dumps(projection)
                assert allowed in serialized
                assert (restricted in serialized) is can_read_private
        for member, permitted in ((recipient, True), (outsider, False)):
            resumed = client.portal.call(client.app.state.workspace.execute, member['session_id'],
                                         'get_search_result', json.dumps({'search_id': search_id}))
            assert resumed['ok'] is permitted
            if permitted:
                assert resumed['result'] == found['result']
            else:
                assert restricted not in json.dumps(resumed)
        assert public['message_id'] != private['message_id']
