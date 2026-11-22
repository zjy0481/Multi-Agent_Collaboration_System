"""T007 authorization, fixed history and independent assistant execution."""
import asyncio
import json
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from test_collaboration import OPERATOR, Sandbox, auth, bootstrap
from test_delegation import context


def wait_until(fn, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for fork or turn")


def application(path, model):
    return create_app(model=model, sandbox=Sandbox(), state_dir=path, bootstrap=bootstrap,
                      operator_token=OPERATOR, schedule=True)


def terminal(client, session, token, turn_id):
    def query():
        response = client.get(f"/v1/sessions/{session}/turns/{turn_id}", headers=auth(token))
        assert response.status_code == 200, response.text
        data = response.json()
        return data if data["status"] != "running" else None
    return wait_until(query)


class Model:
    def __init__(self):
        self.block_parent = threading.Event()
        self.release_parent = threading.Event()
        self.fork_seen = threading.Event()
        self.fork_messages = None

    async def complete(self, messages, tools):
        latest = messages[-1].get("content") or ""
        if "PARENT_HOLD" in latest:
            self.block_parent.set()
            if not await asyncio.to_thread(self.release_parent.wait, 8):
                raise AssertionError("parent turn was not released")
        elif "FORK_PURPOSE_7" in latest:
            self.fork_messages = json.dumps(messages, ensure_ascii=False)
            self.fork_seen.set()
        if "HISTORY_SECOND_SENTINEL" in latest:
            return {"role": "assistant", "content": "PRIVATE_PARENT_CHAT_SENTINEL"}
        return {"role": "assistant", "content": "completed"}


def start_turn(client, member, token, text):
    endpoint = f"/v1/sessions/{member['session_id']}/turns"
    response = client.post(endpoint, headers=auth(token), json={"text": text})
    assert response.status_code == 202, response.text
    return response.json()["turn_id"]


def test_fork_history_authorization_parallel_replay_and_revocation(tmp_path):
    model = Model()
    with TestClient(application(tmp_path, model)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_token = tokens[lead["member_id"]]
        # The original user assignment may already have been scheduled for the Lead.
        wait_until(lambda: not client.app.state.runtime.active.get(lead["session_id"]))
        anchor_turns = []
        for text in ("HISTORY_FIRST_SENTINEL", "HISTORY_SECOND_SENTINEL", "HISTORY_THIRD_SECRET"):
            turn_id = start_turn(client, lead, lead_token, text)
            assert terminal(client, lead["session_id"], lead_token, turn_id)["status"] == "completed"
            anchor_turns.append(turn_id)

        store = client.app.state.workspace.artifacts
        team_id = base.rsplit("/", 1)[-1]
        def publish(label):
            return store.publish(team_id=team_id, author_session_id=lead["session_id"],
                                 source_path=label + ".txt", content=label.encode(), reader_session_ids=[])
        allowed = client.portal.call(publish, "ALLOWED")
        denied = client.portal.call(publish, "UNSHARED")
        endpoint = f"/v1/sessions/{lead['session_id']}/forks"
        body = {"request_id": uuid4().hex, "from_turn_id": anchor_turns[1], "goal_id": goal,
                "purpose": "FORK_PURPOSE_7", "scope_id": "bounded-fork-test",
                "history_turn_ids": anchor_turns[:2], "version_ids": [allowed["version_id"]],
                "allowed_tools": ["read_artifact"], "contact_session_ids": [others[0]["session_id"]]}
        bad = client.post(endpoint, headers=auth(lead_token), json={**body, "request_id": uuid4().hex,
                                                                  "history_turn_ids": [anchor_turns[2]]})
        assert bad.status_code in (403, 409, 422), bad.text
        unpermitted = client.post(endpoint, headers=auth(tokens[others[1]["member_id"]]), json=body)
        assert unpermitted.status_code in (403, 404), unpermitted.text
        hold_id = start_turn(client, lead, lead_token, "PARENT_HOLD")
        try:
            assert model.block_parent.wait(5)
            response = client.post(endpoint, headers=auth(lead_token), json=body)
            assert response.is_success, response.text
            fork = response.json()
            helper = fork["session_id"]
            assert fork["from_turn_id"] == anchor_turns[1]
            assert model.fork_seen.wait(5), "assistant must run while parent is still active"
            assert "HISTORY_SECOND_SENTINEL" in model.fork_messages
            assert "HISTORY_THIRD_SECRET" not in model.fork_messages
            assert "PRIVATE_PARENT_CHAT_SENTINEL" not in model.fork_messages
            workspace = client.app.state.workspace
            def read(version):
                return client.portal.call(workspace.execute, helper, "read_artifact", json.dumps({"version_id": version}))
            assert read(allowed["version_id"])["ok"]
            assert not read(denied["version_id"])["ok"]
            handoff_body = {"request_id": uuid4().hex, "version_ids": [denied["version_id"]], "note": "later authorized material"}
            handoff = client.post(f"/v1/forks/{helper}/handoff", headers=auth(lead_token), json=handoff_body)
            assert handoff.is_success, handoff.text
            assert read(denied["version_id"])["ok"]
            visible = client.get(f"/v1/sessions/{helper}/turns", headers=auth(lead_token))
            assert visible.status_code == 200
            assistant_turn = wait_until(lambda: next(
                (turn for turn in client.get(f"/v1/sessions/{helper}/turns", headers=auth(lead_token)).json()
                 if turn["status"] == "completed"), None))
            assert assistant_turn["reply"] is None
            event_stream = client.get(f"/v1/sessions/{helper}/turns/{assistant_turn['turn_id']}/events",
                                      headers=auth(lead_token))
            assert event_stream.status_code == 200
            assert "turn.completed" in event_stream.text and "model.completed" not in event_stream.text
            assert client.get(f"/v1/sessions/{helper}/turns", headers=auth(tokens[others[0]["member_id"]])).status_code in (403, 404)
            outsider = client.post(f"/v1/forks/{helper}/messages", headers=auth(tokens[others[1]["member_id"]]),
                                   json={"request_id": uuid4().hex, "text": "not authorized"})
            assert outsider.status_code in (403, 404), outsider.text
            replay = client.post(endpoint, headers=auth(lead_token), json=body)
            assert replay.is_success and replay.json()["session_id"] == helper
        finally:
            model.release_parent.set()
        assert terminal(client, lead["session_id"], lead_token, hold_id)["status"] == "completed"
    with TestClient(application(tmp_path, model)) as client:
        replay = client.post(endpoint, headers=auth(lead_token), json=body)
        assert replay.is_success and replay.json()["session_id"] == helper
        revoked = client.post(f"/v1/forks/{helper}/revoke", headers=auth(lead_token), json={"request_id": uuid4().hex})
        assert revoked.is_success, revoked.text
        workspace = client.app.state.workspace
        result = client.portal.call(workspace.execute, helper, "read_artifact", json.dumps({"version_id": allowed["version_id"]}))
        assert not result["ok"]
        after = client.post(f"/v1/forks/{helper}/messages", headers=auth(lead_token),
                            json={"request_id": uuid4().hex, "text": "continue after revoke"})
        assert after.status_code in (403, 404, 409), after.text


def test_contact_private_answer_and_parent_result_decisions(tmp_path):
    class ContactModel(Model):
        def __init__(self):
            super().__init__()
            self.assistant_id = None
            self.contact_session = None
            self.answered = threading.Event()

        async def complete(self, messages, tools):
            last = messages[-1].get("content") or ""
            if messages[-1].get("role") == "tool" and messages[-1].get("tool_call_id") == "contact-reply":
                self.answered.set()
                return {"role": "assistant", "content": "answered"}
            if "DIRECT_QUESTION_MARKER" in last:
                return {"role": "assistant", "content": None, "tool_calls": [{"id": "contact-reply", "type": "function", "function": {
                    "name": "send_fork_message", "arguments": json.dumps({"assistant_id": self.assistant_id,
                        "request_id": "contact-reply", "recipient_session_id": self.contact_session,
                        "text": "DIRECT_ANSWER_MARKER", "version_ids": []})}}]}
            return {"role": "assistant", "content": "ready"}

    model = ContactModel()
    with TestClient(application(tmp_path, model)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_token = tokens[lead["member_id"]]
        wait_until(lambda: not client.app.state.runtime.active.get(lead["session_id"]))
        anchor = start_turn(client, lead, lead_token, "anchor for contact")
        assert terminal(client, lead["session_id"], lead_token, anchor)["status"] == "completed"
        contact = others[0]
        body = {"request_id": uuid4().hex, "from_turn_id": anchor, "goal_id": goal,
                "purpose": "contact test", "scope_id": "contact-scope", "history_turn_ids": [anchor],
                "version_ids": [], "allowed_tools": ["send_fork_message"], "contact_session_ids": [contact["session_id"]]}
        created = client.post(f"/v1/sessions/{lead['session_id']}/forks", headers=auth(lead_token), json=body)
        assert created.is_success, created.text
        helper = model.assistant_id = created.json()["session_id"]
        model.contact_session = contact["session_id"]
        message_url = f"/v1/forks/{helper}/messages"
        sent = client.post(message_url, headers=auth(tokens[contact["member_id"]]),
                           json={"request_id": "contact-query", "text": "DIRECT_QUESTION_MARKER"})
        assert sent.is_success, sent.text
        assert model.answered.wait(8), "assistant should respond after authorized contact message"
        def contact_thread():
            response = client.get(message_url, headers=auth(tokens[contact["member_id"]]))
            assert response.status_code == 200
            return response.json()
        thread = wait_until(lambda: (lambda rows: rows if any(m["text"] == "DIRECT_ANSWER_MARKER" for m in rows) else None)(contact_thread()))
        assert any(m["text"] == "DIRECT_ANSWER_MARKER" and m["recipient_session_id"] == contact["session_id"] for m in thread)
        parent_view = client.get(message_url, headers=auth(lead_token)).json()
        assert all(m["text"] not in {"DIRECT_QUESTION_MARKER", "DIRECT_ANSWER_MARKER"} for m in parent_view)
        def parent_result():
            return client.app.state.runtime.fork_messages.execute(helper, "send_fork_message", json.dumps({
                "assistant_id": helper, "request_id": "result-to-parent", "text": "RESULT_WITH_SOURCE", "version_ids": []}))
        result = client.portal.call(parent_result)
        assert result["ok"], result
        decision_url = f"/v1/forks/{helper}/decision"
        for choice in ("adopt", "return"):
            payload = {"request_id": f"decision-{choice}", "message_id": result["result"]["message_id"],
                       "decision": choice, "reason": "reviewed evidence"}
            decision = client.post(decision_url, headers=auth(lead_token), json=payload)
            assert decision.is_success, decision.text
            assert client.post(decision_url, headers=auth(lead_token), json=payload).json() == decision.json()
            assert decision.json()["message_id"] == result["result"]["message_id"]


def test_t004_scope_closes_after_registration_and_old_task_revision_fails(tmp_path):
    from test_delegation import action, confirm_all, create, terms

    with TestClient(application(tmp_path, Model())) as client:
        base, lead, others, tokens, goal = context(client)
        token = tokens[lead["member_id"]]
        wait_until(lambda: not client.app.state.runtime.active.get(lead["session_id"]))
        anchor = start_turn(client, lead, token, "anchor for delegation")
        assert terminal(client, lead["session_id"], token, anchor)["status"] == "completed"
        endpoint = f"/v1/sessions/{lead['session_id']}/forks"
        scope = uuid4().hex
        body = {"request_id": uuid4().hex, "from_turn_id": anchor, "goal_id": goal,
                "purpose": "help initial proposal", "scope_id": scope, "history_turn_ids": [anchor],
                "version_ids": [], "allowed_tools": [], "contact_session_ids": []}
        fork = client.post(endpoint, headers=auth(token), json=body)
        assert fork.is_success, fork.text
        helper = fork.json()["session_id"]
        draft = create(client, base, token, goal, terms(others[0]["member_id"], others[1]["member_id"]), scope_id=scope)
        pid = draft.get("proposal_id", draft.get("id"))
        confirm_all(client, pid, tokens)
        assigned = action(client, pid, token, "assign")
        assert assigned.is_success, assigned.text
        task_id = assigned.json()["task_id"]
        closed = client.post(f"/v1/forks/{helper}/messages", headers=auth(token),
                             json={"request_id": uuid4().hex, "text": "after registration"})
        assert closed.status_code == 409, closed.text
        task_body = {**body, "request_id": uuid4().hex, "task_id": task_id, "expected_revision": 1}
        task_body.pop("scope_id")
        task_fork = client.post(endpoint, headers=auth(token), json=task_body)
        assert task_fork.is_success, task_fork.text
        task_helper = task_fork.json()["session_id"]
        revised = create(client, base, token, goal, terms(others[0]["member_id"], others[1]["member_id"],
                         deliverables=["changed"]), task_id=task_id, base_revision=1)
        revised_id = revised.get("proposal_id", revised.get("id"))
        confirm_all(client, revised_id, tokens)
        updated = action(client, revised_id, token, "update")
        assert updated.is_success, updated.text
        stale = client.post(f"/v1/forks/{task_helper}/messages", headers=auth(token),
                            json={"request_id": uuid4().hex, "text": "old revision"})
        assert stale.status_code == 409, stale.text
        stale_create = client.post(endpoint, headers=auth(token), json={**task_body, "request_id": uuid4().hex})
        assert stale_create.status_code == 409, stale_create.text


def test_two_assistants_require_mutual_parent_authorization(tmp_path):
    with TestClient(application(tmp_path, Model())) as client:
        _, lead, others, tokens, goal = context(client)
        first, second, third = lead, others[0], others[1]
        wait_until(lambda: not client.app.state.runtime.active.get(first["session_id"]))
        helpers = []
        scope = uuid4().hex
        for member, contacts in ((first, [second["session_id"]]), (second, [first["session_id"]]), (third, [])):
            token = tokens[member["member_id"]]
            anchor = start_turn(client, member, token, "mutual contact anchor")
            assert terminal(client, member["session_id"], token, anchor)["status"] == "completed"
            body = {"request_id": uuid4().hex, "from_turn_id": anchor, "goal_id": goal,
                    "purpose": "mutual contact", "scope_id": scope, "history_turn_ids": [anchor],
                    "version_ids": [], "allowed_tools": ["send_fork_message"], "contact_session_ids": contacts}
            response = client.post(f"/v1/sessions/{member['session_id']}/forks", headers=auth(token), json=body)
            assert response.is_success, response.text
            helpers.append(response.json()["session_id"])
        def send_helper(target):
            return client.app.state.runtime.fork_messages.execute(helpers[0], "send_fork_message", json.dumps({
                "assistant_id": helpers[0], "request_id": uuid4().hex,
                "recipient_session_id": target, "text": "scope question", "version_ids": []}))
        assert client.portal.call(send_helper, helpers[1])["ok"]
        assert not client.portal.call(send_helper, helpers[2])["ok"]
