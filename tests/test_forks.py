"""T007 authorization, fixed history and independent assistant execution."""
import asyncio
import json
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.fork_messages import ForkMessageInput
from collaboration_runtime.local_auth import OPERATOR_ID
from collaboration_runtime.scheduler import DeliveryScheduler
from test_clarifications import ask
from test_collaboration import OPERATOR, Sandbox, auth, bootstrap, send, snapshot
from test_delegation import action, confirm_all, context, create, ok, terms
from test_scheduler import tool


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


class DeliveryModel:
    def __init__(self):
        self.inputs = []

    async def complete(self, messages, tools):
        self.inputs.append(messages[-1]["content"])
        return {"role": "assistant", "content": "completed"}


@pytest.fixture
def fork_delivery(tmp_path):
    model = DeliveryModel()
    app = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                     operator_token=OPERATOR, schedule=False)
    with TestClient(app) as client:
        runtime = app.state.runtime
        scheduler = DeliveryScheduler(runtime, app.state.collaboration, app.state.workspace)
        base, lead, others, tokens, goal = context(client)
        lead_token = tokens[lead["member_id"]]
        contact = others[0]
        task_terms = terms(contact["member_id"], others[1]["member_id"])
        draft = create(client, base, lead_token, goal, task_terms)
        confirm_all(client, draft["proposal_id"], tokens)
        task = ok(action(client, draft["proposal_id"], lead_token, "assign"))["task_id"]
        anchor = start_turn(client, lead, lead_token, "delivery anchor")
        assert terminal(client, lead["session_id"], lead_token, anchor)["status"] == "completed"

        def publish(content, readers, artifact_id=None):
            return app.state.workspace.artifacts.publish(
                team_id=base.rsplit("/", 1)[-1], author_session_id=lead["session_id"],
                source_path="fixture.txt", content=content, reader_session_ids=readers, artifact_id=artifact_id,
            )

        artifact = client.portal.call(publish, b"shared first version", [contact["session_id"]])
        newer = client.portal.call(publish, b"private second version", [], artifact["artifact_id"])
        helper = ok(client.post(f"/v1/sessions/{lead['session_id']}/forks", headers=auth(lead_token), json={
            "request_id": uuid4().hex, "from_turn_id": anchor, "goal_id": goal, "task_id": task,
            "expected_revision": 1, "purpose": "delivery test", "version_ids": [artifact["version_id"]],
            "contact_session_ids": [contact["session_id"]],
        }))["session_id"]

        async def drain(claim):
            while job := claim():
                runtime.launch(*job)
                await runtime.active[job[0]].task

        client.portal.call(drain, scheduler._claim_one)
        client.portal.call(drain, scheduler._claim_assistant)
        model.inputs.clear()
        yield client, runtime, scheduler, model, drain, base, lead_token, contact, tokens, goal, task, task_terms, helper, artifact, newer


def post_fork_delivery(client, runtime, helper, contact, version_id):
    return client.portal.call(runtime.fork_messages.post, helper, helper, ForkMessageInput(
        request_id=uuid4().hex, text="formal fork delivery", recipient_session_id=contact["session_id"],
        version_ids=[version_id],
    ))


@pytest.mark.parametrize("condition", ["revoked", "goal_ended", "revised", "material_unavailable"])
@pytest.mark.parametrize("recoverable", [False, True], ids=["pending", "recoverable"])
def test_formal_fork_delivery_stops_when_scope_ends(fork_delivery, condition, recoverable):
    (client, runtime, scheduler, model, drain, base, lead_token, contact, tokens,
     goal, task, task_terms, helper, artifact, _) = fork_delivery
    message = post_fork_delivery(client, runtime, helper, contact, artifact["version_id"])
    if recoverable:
        job = client.portal.call(scheduler._claim_one)
        assert job and job[0] == contact["session_id"]
        client.portal.call(lambda: runtime._finish(job[1], "recoverable", error="fixture restart"))

    def persist(sql, value):
        with runtime.db:
            runtime.db.execute(sql, (value,))

    if condition == "revoked":
        ok(client.post(f"/v1/forks/{helper}/revoke", headers=auth(lead_token), json={"request_id": uuid4().hex}))
    elif condition == "goal_ended":
        client.portal.call(persist, "UPDATE team_goals SET status = 'cancelled' WHERE id = ?", goal)
    elif condition == "revised":
        draft = create(client, base, lead_token, goal, {**task_terms, "deliverables": ["updated"]},
                       task_id=task, base_revision=1)
        confirm_all(client, draft["proposal_id"], tokens)
        ok(action(client, draft["proposal_id"], lead_token, "update"))
    else:
        client.portal.call(persist, "UPDATE artifact_versions SET reader_session_ids = '[]' WHERE version_id = ?",
                           artifact["version_id"])

    ordinary = send(client, base, lead_token, request_id=uuid4().hex, text="ordinary delivery",
                    recipient_id=contact["member_id"])
    for _ in range(2):
        client.portal.call(drain, scheduler._claim_one)
    inputs = [json.loads(text)["message"] for text in model.inputs]
    assert not any(item["id"] == message["message_id"] for item in inputs)
    assert any(item["id"] == ordinary["message_id"] and "version_ids" not in item for item in inputs)
    delivery, turn = client.portal.call(lambda: tuple(runtime.db.execute("""
        SELECT d.status, t.status FROM message_deliveries d
        LEFT JOIN scheduled_turns s USING (event_id, session_id, purpose)
        LEFT JOIN turns t ON t.id = s.turn_id
        WHERE d.message_id = ?
    """, (message["message_id"],)).fetchone()))
    assert delivery == "failed"
    assert turn == ("interrupted" if recoverable else None)


@pytest.mark.parametrize("legacy_recovery", [False, True], ids=["fresh", "legacy-recovery"])
def test_fork_recipients_receive_only_pinned_authorized_versions(fork_delivery, legacy_recovery):
    (client, runtime, scheduler, model, drain, _, _, contact, tokens,
     _, _, _, helper, artifact, newer) = fork_delivery
    sent = post_fork_delivery(client, runtime, helper, contact, artifact["version_id"])
    url = f"/v1/forks/{helper}/messages"
    token = tokens[contact["member_id"]]
    denied = client.post(url, headers=auth(token), json={"request_id": uuid4().hex,
                         "text": "unshared version", "version_ids": [newer["version_id"]]})
    assert denied.status_code == 404, denied.text
    received = ok(client.post(url, headers=auth(token), json={"request_id": uuid4().hex,
                             "text": "assistant delivery", "version_ids": [artifact["version_id"]]}))
    if legacy_recovery:
        def recover_legacy_delivery():
            _, turn_id, _, _ = scheduler._claim_one()
            state = runtime.journal.checkpoint(turn_id)
            history = state["history"]
            payload = json.loads(history[0]["content"])
            payload["message"].pop("version_ids", None)
            history[0]["content"] = json.dumps(payload)
            with runtime.db:
                assert runtime.journal.advance(turn_id, state["generation"], state["step"], state["phase"], history)
                runtime._write_finish(turn_id, "recoverable", error="fixture restart")

        client.portal.call(recover_legacy_delivery)
    client.portal.call(drain, scheduler._claim_one)
    client.portal.call(drain, scheduler._claim_assistant)
    formal_input, assistant_input = map(json.loads, model.inputs)
    assert formal_input["message"]["id"] == sent["message_id"]
    assert formal_input["message"]["version_ids"] == [artifact["version_id"]]
    assert assistant_input["message_id"] == received["message_id"]
    assert assistant_input["version_ids"] == [artifact["version_id"]]
    assert all(newer["version_id"] not in text for text in model.inputs)


def test_claimed_fork_delivery_rechecks_scope_before_model(fork_delivery):
    (client, runtime, scheduler, model, _, _, lead_token, contact, _,
     _, _, _, helper, artifact, _) = fork_delivery
    post_fork_delivery(client, runtime, helper, contact, artifact["version_id"])
    job = client.portal.call(scheduler._claim_one)
    assert job and job[0] == contact["session_id"]
    ok(client.post(f"/v1/forks/{helper}/revoke", headers=auth(lead_token), json={"request_id": uuid4().hex}))

    async def run_claimed():
        runtime.launch(*job)
        await runtime.active[job[0]].task

    client.portal.call(run_claimed)
    assert not model.inputs
    row = client.portal.call(lambda: runtime.db.execute("SELECT status FROM turns WHERE id = ?", (job[1],)).fetchone())
    assert row["status"] == "interrupted"


def test_private_fork_delivery_cannot_be_used_to_ask_human(fork_delivery):
    (client, runtime, scheduler, model, drain, base, _, contact, _,
     _, _, _, helper, artifact, _) = fork_delivery
    original = post_fork_delivery(client, runtime, helper, contact, artifact["version_id"])

    async def ask_once(messages, tools):
        message = messages[-1]
        if message["role"] == "tool":
            model.inputs.append(json.loads(message["content"]))
            return {"role": "assistant", "content": "Private thread kept private"}
        return tool("ask_human", "private-fork-question", {"request_id": uuid4().hex,
                    "source_message_id": original["message_id"], "text": "Generic question"})

    model.complete = ask_once
    client.portal.call(drain, scheduler._claim_one)
    assert len(model.inputs) == 1 and model.inputs[0]["status"] == 403
    assert client.get(f"{base}/messages/{original['message_id']}", headers=auth(OPERATOR)).status_code == 404
    assert not snapshot(client, base, OPERATOR)["questions"]


def seed_persisted_fork_question(client, runtime, contact, message):
    """Build compatibility state in the isolated test DB, not through the private-message API."""
    def persist():
        chat = runtime.collaboration
        actor = chat.auth.member(contact["session_id"])
        with runtime.db:
            question = chat._create_message(actor.team_id, actor, recipient=OPERATOR_ID,
                                            text="Generic fixture question", kind="question",
                                            mentions=[], reply_to=message["message_id"])
            runtime.db.execute("""
                INSERT INTO human_questions(question_id, asker_id, source_message_id, work_event_id,
                                            session_id, work_purpose)
                VALUES (?, ?, ?, ?, ?, 'fork.message')
            """, (question["message_id"], actor.id, message["message_id"], message["event_id"], actor.session_id))
            runtime.db.execute("""
                UPDATE message_deliveries SET status = 'blocked'
                WHERE event_id = ? AND session_id = ? AND purpose = 'fork.message'
            """, (message["event_id"], actor.session_id))
        return question["message_id"]

    return client.portal.call(persist)


@pytest.mark.parametrize("second_state", ["valid", "legacy-recovery", "revoked"])
def test_persisted_fork_clarification_keeps_origin_across_answers(fork_delivery, second_state):
    (client, runtime, scheduler, model, drain, base, lead_token, contact, tokens,
     _, _, _, helper, artifact, newer) = fork_delivery
    original = post_fork_delivery(client, runtime, helper, contact, artifact["version_id"])
    first_question = seed_persisted_fork_question(client, runtime, contact, original)
    first_answer = send(client, base, OPERATOR, request_id=uuid4().hex, text="First answer", reply_to=first_question)

    async def ask_again(messages, tools):
        payload = json.loads(messages[-1]["content"])
        model.inputs.append(payload)
        return tool("ask_human", "second-fork-question", {"request_id": uuid4().hex,
                    "source_message_id": payload["message"]["id"], "text": "Generic follow-up question"})

    complete = model.complete
    model.complete = ask_again
    client.portal.call(drain, scheduler._claim_one)
    model.complete = complete
    second_question, = [question for question in snapshot(client, base, OPERATOR)["questions"]
                        if question["source_message_id"] == first_answer["message_id"]]
    second_answer = send(client, base, OPERATOR, request_id=uuid4().hex, text="Second answer",
                         reply_to=second_question["question_id"])
    if second_state == "revoked":
        ok(client.post(f"/v1/forks/{helper}/revoke", headers=auth(lead_token), json={"request_id": uuid4().hex}))
    elif second_state == "legacy-recovery":
        def recover_legacy_continuation():
            _, turn_id, _, _ = scheduler._claim_one()
            checkpoint = runtime.journal.checkpoint(turn_id)
            history = checkpoint["history"]
            payload = json.loads(history[0]["content"])
            payload.pop("fork_message", None)
            history[0]["content"] = json.dumps(payload)
            with runtime.db:
                assert runtime.journal.advance(turn_id, checkpoint["generation"], checkpoint["step"],
                                               checkpoint["phase"], history)
                runtime._write_finish(turn_id, "recoverable", error="fixture restart")

        client.portal.call(recover_legacy_continuation)
    client.portal.call(drain, scheduler._claim_one)
    assert len(model.inputs) == (1 if second_state == "revoked" else 2)
    expected = {"id": original["message_id"], "version_ids": [artifact["version_id"]]}
    first_input = model.inputs[0]
    assert first_input["fork_message"] == expected
    assert first_input["source_message"]["id"] == original["message_id"]
    if second_state != "revoked":
        second_input = json.loads(model.inputs[1])
        assert second_input["fork_message"] == expected
        assert second_input["source_message"]["id"] == first_answer["message_id"]
        assert second_input["message"]["id"] == second_answer["message_id"]
    assert newer["version_id"] not in json.dumps(model.inputs)
    token = tokens[contact["member_id"]]
    assert ok(client.get(f"{base}/messages/{second_answer['message_id']}", headers=auth(token)))["text"] == "Second answer"


@pytest.mark.parametrize("boundary", ["late-answer", "pending", "recoverable", "claimed", "model-result"])
def test_persisted_fork_clarification_checks_scope_at_execution_boundaries(fork_delivery, boundary):
    (client, runtime, scheduler, model, drain, base, lead_token, contact, _,
     _, _, _, helper, artifact, _) = fork_delivery
    original = post_fork_delivery(client, runtime, helper, contact, artifact["version_id"])
    question = seed_persisted_fork_question(client, runtime, contact, original)

    def revoke():
        ok(client.post(f"/v1/forks/{helper}/revoke", headers=auth(lead_token), json={"request_id": uuid4().hex}))

    if boundary == "late-answer":
        revoke()
    answer = send(client, base, OPERATOR, request_id=uuid4().hex, text="Recorded answer", reply_to=question)
    if boundary in {"recoverable", "claimed", "model-result"}:
        claim = client.portal.call(scheduler._claim_one)
        assert claim and claim[0] == contact["session_id"]
    if boundary == "recoverable":
        client.portal.call(lambda: runtime._finish(claim[1], "recoverable", error="fixture restart"))
    if boundary in {"pending", "recoverable", "claimed"}:
        revoke()

    started = threading.Event()
    release = threading.Event()

    async def finish_with_tool(messages, tools):
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "Unexpected tool ran"}
        model.inputs.append(messages[-1]["content"])
        started.set()
        assert await asyncio.to_thread(release.wait, 8)
        return tool("send_message", "late-fixture-tool", {"request_id": uuid4().hex, "text": "Late fixture message"})

    async def run_claimed():
        runtime.launch(*claim)
        await runtime.active[claim[0]].task

    if boundary == "model-result":
        model.complete = finish_with_tool
        running = client.portal.start_task_soon(run_claimed)
        try:
            assert started.wait(8)
            revoke()
        finally:
            release.set()

        running.result(timeout=8)
    elif boundary == "claimed":
        client.portal.call(run_claimed)
    else:
        client.portal.call(drain, scheduler._claim_one)

    assert len(model.inputs) == (1 if boundary == "model-result" else 0)
    history = snapshot(client, base, OPERATOR)
    assert any(item["id"] == answer["message_id"] for item in history["messages"])
    assert all(item["text"] != "Late fixture message" for item in history["messages"])
    delivery, turn = client.portal.call(lambda: tuple(runtime.db.execute("""
        SELECT d.status, t.status FROM message_deliveries d
        LEFT JOIN scheduled_turns s USING (event_id, session_id, purpose)
        LEFT JOIN turns t ON t.id = s.turn_id
        WHERE d.message_id = ?
    """, (answer["message_id"],)).fetchone()))
    assert delivery == ("interrupted" if boundary in {"claimed", "model-result"} else "failed")
    assert turn == ("interrupted" if boundary in {"recoverable", "claimed", "model-result"} else None)


@pytest.mark.parametrize("condition", ["valid", "revoked", "revoked-recovery", "material-unavailable", "broken-link"])
def test_persisted_fork_corrections_keep_original_scope(fork_delivery, condition):
    (client, runtime, scheduler, model, drain, base, lead_token, contact, tokens,
     _, _, _, helper, artifact, newer) = fork_delivery
    original = post_fork_delivery(client, runtime, helper, contact, artifact["version_id"])
    question_id = seed_persisted_fork_question(client, runtime, contact, original)
    answer = send(client, base, OPERATOR, request_id=uuid4().hex, text="Answer", reply_to=question_id)
    client.portal.call(drain, scheduler._claim_one)
    model.inputs.clear()

    first = send(client, base, OPERATOR, request_id=uuid4().hex, text="First correction", reply_to=answer["message_id"])
    client.portal.call(drain, scheduler._claim_one)
    first_input, = map(json.loads, model.inputs)
    assert first_input["message"]["id"] == first["message_id"]
    model.inputs.clear()

    def revoke():
        ok(client.post(f"/v1/forks/{helper}/revoke", headers=auth(lead_token), json={"request_id": uuid4().hex}))

    def persist(sql, value):
        with runtime.db:
            runtime.db.execute(sql, (value,))

    if condition == "revoked":
        revoke()
    second = send(client, base, OPERATOR, request_id=uuid4().hex, text="Second correction", reply_to=first["message_id"])
    correction_id = second["message_id"]
    if condition == "revoked-recovery":
        claim = client.portal.call(scheduler._claim_one)
        assert claim and claim[0] == contact["session_id"]
        client.portal.call(lambda: runtime._finish(claim[1], "recoverable", error="fixture restart"))
        revoke()
    elif condition == "material-unavailable":
        client.portal.call(persist, "UPDATE artifact_versions SET reader_session_ids = '[]' WHERE version_id = ?",
                           artifact["version_id"])
    elif condition == "broken-link":
        client.portal.call(persist, "DELETE FROM clarification_corrections WHERE message_id = ?", correction_id)

    client.portal.call(drain, scheduler._claim_one)
    if condition == "valid":
        second_input, = map(json.loads, model.inputs)
        assert second_input["message"]["id"] == correction_id
        for payload in (first_input, second_input):
            assert payload["fork_message"] == {"id": original["message_id"], "version_ids": [artifact["version_id"]]}
            assert newer["version_id"] not in json.dumps(payload)
    else:
        assert not model.inputs

    for token in (OPERATOR, tokens[contact["member_id"]]):
        assert ok(client.get(f"{base}/messages/{correction_id}", headers=auth(token)))["text"] == "Second correction"
    for member_id, token in tokens.items():
        if member_id != contact["member_id"]:
            assert client.get(f"{base}/messages/{correction_id}", headers=auth(token)).status_code == 404
    deliveries = client.portal.call(lambda: [tuple(row) for row in runtime.db.execute("""
        SELECT d.session_id, d.status, t.status FROM message_deliveries d
        LEFT JOIN scheduled_turns s USING (event_id, session_id, purpose)
        LEFT JOIN turns t ON t.id = s.turn_id
        WHERE d.message_id = ? AND d.purpose = 'clarification.correction'
    """, (correction_id,))])
    expected = ("completed", "completed") if condition == "valid" else (
        "failed", "interrupted" if condition == "revoked-recovery" else None)
    assert deliveries == [(contact["session_id"], *expected)]


def test_ordinary_group_correction_runs_for_original_asker_and_citing_member(fork_delivery):
    (client, runtime, scheduler, model, drain, base, lead_token, contact, _,
     _, _, _, _, _, _) = fork_delivery
    lead = client.portal.call(runtime.collaboration.auth.resolve, lead_token)
    work = send(client, base, OPERATOR, request_id=uuid4().hex, text="Group work", mentions=[contact["member_id"]])
    question = ask(client, contact, uuid4().hex, work["message_id"], "Group clarification")
    answer = send(client, base, OPERATOR, request_id=uuid4().hex, text="Group answer",
                  reply_to=question["question"]["question_id"])
    send(client, base, lead_token, request_id=uuid4().hex, text="Citing the group answer", reply_to=answer["message_id"])
    first = send(client, base, OPERATOR, request_id=uuid4().hex, text="Group correction", reply_to=answer["message_id"])
    second = send(client, base, OPERATOR, request_id=uuid4().hex, text="Corrected again", reply_to=first["message_id"])
    client.portal.call(drain, scheduler._claim_one)

    correction_inputs = [payload for payload in map(json.loads, model.inputs)
                         if payload["delivery"]["purpose"] == "clarification.correction"]
    for correction in (first, second):
        recipients = [payload["delivery"]["session_id"] for payload in correction_inputs
                      if payload["message"]["id"] == correction["message_id"]]
        assert sorted(recipients) == sorted([contact["session_id"], lead.session_id])
    assert all("fork_message" not in payload for payload in correction_inputs)


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
