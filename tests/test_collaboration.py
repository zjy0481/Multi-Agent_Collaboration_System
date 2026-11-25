"""Acceptance tests for durable, permission-filtered team collaboration."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.teams import LeadDecision

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "test-operator-token"


class Model:
    async def complete(self, messages, tools):
        raise AssertionError("unexpected model call")


class Sandbox:
    def prepare(self, workspace_id):
        pass


async def bootstrap(roles):
    return LeadDecision(role_id="lead", reason="Coordinates")


def app(path):
    return create_app(model=Model(), sandbox=Sandbox(), state_dir=path,
                      bootstrap=bootstrap, operator_token=OPERATOR, schedule=False)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def setup_team(client):
    roster = (ROOT / "test samples" / "scenario_1_software_engineering 2" / "01_role_initialization.md").read_text(encoding="utf-8")
    response = client.post("/v1/teams", headers=auth(OPERATOR), json={"request_id": "team", "roster": roster})
    assert response.status_code == 201, response.text
    team = response.json()
    base = f"/v1/teams/{team['team_id']}"
    lead = next(m for m in team["members"] if m["member_id"] == team["lead_member_id"])
    others = [m for m in team["members"] if m["member_id"] != lead["member_id"]]
    tokens = {}
    for member in [lead, *others]:
        response = client.post(f"{base}/members/{member['member_id']}/credential", headers=auth(OPERATOR))
        assert response.is_success, response.text
        tokens[member["member_id"]] = response.json()["token"]
    return base, lead, others, tokens


def send(client, base, token, **payload):
    response = client.post(f"{base}/messages", headers=auth(token), json=payload)
    assert response.is_success, response.text
    return response.json()


def snapshot(client, base, token):
    response = client.get(f"{base}/snapshot", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def events(client, base, token, *, after=None, last=None):
    headers = auth(token)
    if last is not None:
        headers["Last-Event-ID"] = str(last)
    params = {"follow": "false"}
    if after is not None:
        params["after"] = str(after)
    response = client.get(f"{base}/events", headers=headers, params=params)
    assert response.status_code == 200, response.text
    result = []
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        lines = [line for line in block.splitlines() if ":" in line]
        fields = {}
        for line in lines:
            key, value = line.split(":", 1)
            fields[key] = value.lstrip()
        if "event" in fields:
            result.append((fields["event"], json.loads(fields.get("data", "{}")), fields.get("id")))
    return result


def test_routing_replay_and_restart(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        target = others[0]
        lead_token, target_token = tokens[lead["member_id"]], tokens[target["member_id"]]
        payload = {"request_id": "default", "text": "Please coordinate"}
        with ThreadPoolExecutor(max_workers=2) as workers:
            attempts = [workers.submit(send, client, base, OPERATOR, **payload) for _ in range(2)]
            first, replay = [attempt.result() for attempt in attempts]
        assert replay == first
        assert client.post(f"{base}/messages", headers=auth(OPERATOR), json={**payload, "text": "changed"}).status_code == 409
        marked = send(client, base, OPERATOR, request_id="mention", text="Please investigate", mentions=[target["member_id"], target["member_id"]])
        progress = send(client, base, OPERATOR, request_id="progress", text="Progress", kind="progress")
        marked_progress = send(client, base, OPERATOR, request_id="progress-mention", text="Review this update",
                               kind="progress", mentions=[target["member_id"]])
        lead_items = client.get(f"{base}/deliveries", headers=auth(lead_token)).json()
        target_items = client.get(f"{base}/deliveries", headers=auth(target_token)).json()
        assert sum(item["message_id"] == first["message_id"] for item in lead_items) == 1
        assert sum(item["message_id"] == marked["message_id"] for item in target_items) == 1
        assert sum(item["message_id"] == marked_progress["message_id"] for item in target_items) == 1
        assert not any(item["message_id"] == progress["message_id"] for item in lead_items + target_items)
        assert all(item["status"] == "pending" for item in lead_items + target_items)
        assert all(item["session_id"] == target["session_id"] for item in target_items if item["message_id"] == marked["message_id"])
    with TestClient(app(tmp_path)) as client:
        restored = client.get(f"{base}/deliveries", headers=auth(target_token))
        assert restored.status_code == 200
        assert sum(item["message_id"] == marked["message_id"] and item["status"] == "pending" for item in restored.json()) == 1
        assert sum(m["id"] == first["message_id"] for m in snapshot(client, base, OPERATOR)["messages"]) == 1


def test_private_projection_and_sse_resume(tmp_path, monkeypatch):
    monkeypatch.setattr("collaboration_runtime.collaboration.EVENT_BATCH_SIZE", 1)
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        target, outsider = others[:2]
        target_token, outsider_token = tokens[target["member_id"]], tokens[outsider["member_id"]]
        cursor = snapshot(client, base, outsider_token)["cursor"]
        target_cursor = snapshot(client, base, target_token)["cursor"]
        public = send(client, base, OPERATOR, request_id="public", text="Public")
        private = send(client, base, OPERATOR, request_id="private", text="Secret", recipient_id=target["member_id"])
        assert client.get(f"{base}/messages/{private['message_id']}", headers=auth(outsider_token)).status_code == 404
        assert client.get(f"{base}/messages/{private['message_id']}", headers=auth(target_token)).status_code == 200
        assert any(d["message_id"] == private["message_id"] for d in client.get(f"{base}/deliveries", headers=auth(target_token)).json())
        assert private["message_id"] not in {m["id"] for m in snapshot(client, base, outsider_token)["messages"]}
        observed = events(client, base, outsider_token, after=cursor)
        ids = {data["message"]["id"] for kind, data, _ in observed if kind == "message.created"}
        assert public["message_id"] in ids and private["message_id"] not in ids
        assert [kind for kind, _, _ in events(client, base, target_token, after=cursor)] == ["stream.reset"]
        roster = (ROOT / "test samples" / "scenario_1_software_engineering 2" / "01_role_initialization.md").read_text(encoding="utf-8")
        other_team = client.post("/v1/teams", headers=auth(OPERATOR), json={"request_id": "other-team", "roster": roster})
        assert other_team.status_code == 201
        other_base = f"/v1/teams/{other_team.json()['team_id']}"
        operator_cursor = snapshot(client, base, OPERATOR)["cursor"]
        assert [kind for kind, _, _ in events(client, other_base, OPERATOR, after=operator_cursor)] == ["stream.reset"]
        target_events = events(client, base, target_token, after=target_cursor)
        assert private["message_id"] in {data["message"]["id"] for kind, data, _ in target_events if kind == "message.created"}
        assert client.post(f"{base}/messages", headers=auth(target_token), json={"request_id": "leak", "text": "Reply", "reply_to": private["message_id"]}).status_code == 403
        assert client.post(f"{base}/messages", headers=auth(outsider_token), json={"request_id": "unreadable", "text": "Reply", "reply_to": private["message_id"]}).status_code == 404
        for invalid_cursor in ("invalid-cursor", "0.é"):
            reset = events(client, base, outsider_token, after=invalid_cursor)
            assert [kind for kind, _, _ in reset] == ["stream.reset"]
        new_cursor = snapshot(client, base, outsider_token)["cursor"]
        later = send(client, base, OPERATOR, request_id="later", text="Later")
        resumed = events(client, base, outsider_token, after="invalid-cursor", last=new_cursor)
        assert later["message_id"] in {data["message"]["id"] for kind, data, _ in resumed if kind == "message.created"}


def test_goal_registration_and_identity(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        lead_token = tokens[lead["member_id"]]
        assert client.get(f"{base}/snapshot").status_code == 401
        assert client.get(f"{base}/snapshot", headers=auth("wrong")).status_code == 401
        assert client.post(f"{base}/members/{lead['member_id']}/credential", headers=auth(tokens[others[0]["member_id"]])).status_code == 403
        source = send(client, base, OPERATOR, request_id="assignment1", text="First goal", kind="assignment")
        source2 = send(client, base, OPERATOR, request_id="assignment2", text="Second goal", kind="assignment")
        url = f"{base}/goals"
        payload = {"request_id": "goal1", "source_message_id": source["message_id"]}
        assert client.post(url, headers=auth(tokens[others[0]["member_id"]]), json=payload).status_code == 403
        response = client.post(url, headers=auth(lead_token), json=payload)
        assert response.is_success, response.text
        goal = response.json()
        assert goal["status"] == "active"
        assert client.post(url, headers=auth(lead_token), json=payload).json() == goal
        replay = client.post(url, headers=auth(lead_token), json={**payload, "request_id": "goal1-again"})
        assert replay.is_success and replay.json()["goal_id"] == goal["goal_id"]
        queued = client.post(url, headers=auth(lead_token), json={"request_id": "goal2", "source_message_id": source2["message_id"]})
        assert queued.is_success and queued.json()["status"] == "queued"
        goals = snapshot(client, base, OPERATOR)["goals"]
        assert len([g for g in goals if g["source_message_id"] == source["message_id"]]) == 1
        login = client.post("/v1/auth/login", json={"token": lead_token})
        assert login.is_success and "httponly" in login.headers["set-cookie"].lower()
        assert client.get(f"{base}/snapshot").json()["actor_id"] == lead["member_id"]


def test_expired_cursor_and_private_goal_projection(tmp_path, monkeypatch):
    import collaboration_runtime.collaboration as collaboration

    monkeypatch.setattr(collaboration, "CURSOR_WINDOW", 2)
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        lead_token = tokens[lead["member_id"]]
        outsider_token = tokens[others[0]["member_id"]]
        stale = snapshot(client, base, outsider_token)["cursor"]
        private_source = send(client, base, OPERATOR, request_id="private-assignment",
                              text="Confidential assignment", kind="assignment", recipient_id=lead["member_id"])
        receipt = client.post(f"{base}/goals", headers=auth(lead_token),
                              json={"request_id": "private-goal", "source_message_id": private_source["message_id"]})
        assert receipt.is_success, receipt.text
        goal_id = receipt.json()["goal_id"]
        send(client, base, OPERATOR, request_id="one", text="One")
        assert [kind for kind, _, _ in events(client, base, outsider_token, after=stale)] == ["stream.reset"]
        outsider = snapshot(client, base, outsider_token)
        assert goal_id not in {goal["id"] for goal in outsider["goals"]}
        assert goal_id in {goal["id"] for goal in snapshot(client, base, lead_token)["goals"]}
        # A still-valid cursor taken before a later private goal must filter its event.
        cursor = outsider["cursor"]
        second = send(client, base, OPERATOR, request_id="private-assignment2",
                      text="Another confidential assignment", kind="assignment", recipient_id=lead["member_id"])
        result = client.post(f"{base}/goals", headers=auth(lead_token),
                             json={"request_id": "private-goal2", "source_message_id": second["message_id"]})
        assert result.is_success, result.text
        visible = events(client, base, outsider_token, after=cursor)
        assert all(kind != "goal.registered" for kind, _, _ in visible)
        assert all(kind != "message.created" or data["message"]["id"] != second["message_id"]
                   for kind, data, _ in visible)


def test_member_private_thread_and_reference_boundaries(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        first, second = others[:2]
        first_token, second_token = tokens[first["member_id"]], tokens[second["member_id"]]
        original = send(client, base, first_token, request_id="dm", text="Private discussion",
                        recipient_id=second["member_id"])
        reply = send(client, base, second_token, request_id="reply", text="Reply in thread",
                     recipient_id=first["member_id"], reply_to=original["message_id"])
        assert client.get(f"{base}/messages/{reply['message_id']}", headers=auth(first_token)).status_code == 200
        assert any(item["message_id"] == reply["message_id"] for item in
                   client.get(f"{base}/deliveries", headers=auth(first_token)).json())
        assert client.get(f"{base}/messages/{original['message_id']}", headers=auth(tokens[lead["member_id"]])).status_code == 404
        forbidden = client.post(f"{base}/messages", headers=auth(second_token), json={
            "request_id": "wrong-thread", "text": "Widen", "recipient_id": lead["member_id"],
            "reply_to": original["message_id"]})
        assert forbidden.status_code == 403
        unreadable = client.post(f"{base}/messages", headers=auth(tokens[lead["member_id"]]), json={
            "request_id": "unreadable-dm", "text": "Reply", "recipient_id": first["member_id"],
            "reply_to": original["message_id"]})
        assert unreadable.status_code == 404


def test_formal_model_tools_use_the_execution_identity(tmp_path):
    class ToolModel:
        action = None

        async def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                assert messages[-1]["tool_call_id"] == "original-call"
                return {"role": "assistant", "content": messages[-1]["content"]}
            schema = next(tool["function"]["parameters"] for tool in tools
                          if tool["function"]["name"] == "send_message")
            assert schema["properties"]["kind"]["enum"] == ["chat", "progress", "correction"]
            assert "required_inputs" not in schema["properties"]
            assert schema["additionalProperties"] is False
            name, arguments = self.action
            assert name in {tool["function"]["name"] for tool in tools}
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "original-call", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]}

    model = ToolModel()
    application = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path,
                             bootstrap=bootstrap, operator_token=OPERATOR, schedule=False)
    with TestClient(application) as client:
        base, lead, others, tokens = setup_team(client)
        outsider = others[0]
        lead_token, outsider_token = tokens[lead["member_id"]], tokens[outsider["member_id"]]
        source = send(client, base, OPERATOR, request_id="from-user", text="Private assignment",
                      kind="assignment", recipient_id=lead["member_id"])

        def run_tool(member, token, name, arguments):
            model.action = (name, arguments)
            endpoint = f"/v1/sessions/{member['session_id']}/turns"
            started = client.post(endpoint, headers=auth(token), json={"text": "Run the requested tool"})
            assert started.status_code == 202, started.text
            path = f"{endpoint}/{started.json()['turn_id']}"
            assert client.get(f"{path}/events", headers=auth(token)).status_code == 200
            return json.loads(client.get(path, headers=auth(token)).json()["reply"])

        spoof = client.post(f"/v1/sessions/{lead['session_id']}/turns",
                            headers=auth(outsider_token), json={"text": "Register a goal"})
        assert spoof.status_code == 404
        denied = run_tool(outsider, outsider_token, "get_message", {"message_id": source["message_id"]})
        assert denied == {"ok": False, "error": "Message not found", "status": 404}
        args = {"request_id": "model-goal", "source_message_id": source["message_id"]}
        result = run_tool(lead, lead_token, "register_goal", args)
        assert result["ok"] is True
        assert client.post(f"{base}/goals", headers=auth(lead_token), json=args).json() == result["result"]

        for kind in ("chat", "progress"):
            sent = run_tool(lead, lead_token, "send_message", {
                "request_id": f"model-{kind}", "text": "Update", "kind": kind,
                "recipient_id": outsider["member_id"],
            })
            assert sent["ok"] is True
        private_correction = run_tool(lead, lead_token, "send_message", {
            "request_id": "private-correction", "text": "Correction", "kind": "correction",
            "recipient_id": outsider["member_id"], "reply_to": source["message_id"],
        })
        assert private_correction["status"] == 403
        for extra in ({"kind": "assignment"}, {"kind": "supplement"}, {"required_inputs": []}):
            denied = run_tool(lead, lead_token, "send_message", {
                "request_id": "invalid-model", "text": "Invalid", **extra,
            })
            assert denied["status"] == 422


def test_operator_assignment_guidance_persists_and_is_delivery_scoped(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        token = tokens[lead['member_id']]
        payload = {'request_id': 'guided', 'text': 'Official assignment unchanged', 'kind': 'assignment',
                   'assignment_guidance': 'Use the provided workspace.'}
        first = send(client, base, OPERATOR, **payload)
        assert send(client, base, OPERATOR, **payload) == first
        assert client.post(f'{base}/messages', headers=auth(OPERATOR), json={
            **payload, 'assignment_guidance': 'Different guidance'}).status_code == 409
        assert client.post(f'{base}/messages', headers=auth(OPERATOR), json={
            **payload, 'request_id': 'bad-kind', 'kind': 'chat'}).status_code == 403
        assert client.post(f'{base}/messages', headers=auth(token), json={
            **payload, 'request_id': 'bad-member'}).status_code == 403
        assert client.post(f'{base}/messages', headers=auth(OPERATOR), json={
            **payload, 'request_id': 'too-long', 'assignment_guidance': 'x' * 4001}).status_code == 422
        message = client.get(f"{base}/messages/{first['message_id']}", headers=auth(token)).json()
        assert message['text'] == payload['text'] and 'assignment_guidance' not in message
        delivery = next(d for d in client.get(f'{base}/deliveries', headers=auth(token)).json()
                        if d['message_id'] == first['message_id'])
        def execution():
            actor = client.app.state.auth.member(lead['session_id'])
            return client.app.state.collaboration.execution_input(dict(delivery), actor)
        actual = client.portal.call(execution)
        assert actual['assignment_guidance'] == payload['assignment_guidance']
        assert actual['message']['text'] == payload['text']
        forged = {**delivery, 'event_id': delivery['event_id'] + 99999}
        def forged_execution():
            actor = client.app.state.auth.member(lead['session_id'])
            return client.app.state.collaboration.execution_input(forged, actor)
        assert 'assignment_guidance' not in client.portal.call(forged_execution)
        old = {'request_id': 'old', 'text': 'Old assignment', 'kind': 'assignment'}
        old_receipt = send(client, base, OPERATOR, **old)
    with TestClient(app(tmp_path)) as client:
        assert send(client, base, OPERATOR, **payload) == first
        assert send(client, base, OPERATOR, **old) == old_receipt
        def restored_execution():
            actor = client.app.state.auth.member(lead['session_id'])
            return client.app.state.collaboration.execution_input(dict(delivery), actor)
        assert client.portal.call(restored_execution)['assignment_guidance'] == payload['assignment_guidance']
