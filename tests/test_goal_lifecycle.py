"""T009 goal acceptance, terminal facts, scope closure and queue activation."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import asyncio
import json
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from test_clarifications import ask
from test_collaboration import Model, OPERATOR, Sandbox, app, auth, bootstrap, send, setup_team, snapshot
from test_delegation import confirm_all, context, create, ok, terms
from test_task_results import decision, publish, review, submission, submit


HOST_TOKEN = "test-host-only-token"


def key():
    return uuid4().hex


def assignment(client, base, text):
    return send(client, base, OPERATOR, request_id=key(), text=text, kind="assignment")


def goal(client, base, token, source, prerequisites=()):
    return ok(client.post(f"{base}/goals", headers=auth(token), json={
        "request_id": key(), "source_message_id": source["message_id"],
        "prerequisite_goal_ids": list(prerequisites),
    }))


def register(client, base, dispatcher_id, tokens, goal_id, owner_id, reviewer_id):
    draft = create(client, base, tokens[dispatcher_id], goal_id, terms(owner_id, reviewer_id))
    confirm_all(client, draft["proposal_id"], tokens)
    response = client.post(f"/v1/proposals/{draft['proposal_id']}/assign", headers=auth(tokens[dispatcher_id]),
                           json={"request_id": key(), "expected_version": 1})
    return ok(response)["task_id"]


def accept_task(client, base, tokens, goal_id, dispatcher_id, lead, owner, reviewer):
    task_id = register(client, base, dispatcher_id, tokens, goal_id, owner["member_id"], reviewer["member_id"])
    endpoint = f"{base}/tasks/{task_id}"
    version = publish(client, base, owner, [lead, reviewer], b"first accepted result")
    submitted = ok(submit(client, endpoint, tokens[owner["member_id"]], submission(version)))
    ok(review(client, endpoint, tokens[reviewer["member_id"]], submitted["submission_id"], decision()))
    return task_id, version


def acceptance(client, base, token, goal_id, revision, required):
    return client.post(f"{base}/goals/{goal_id}/acceptance", headers=auth(token), json={
        "request_id": key(), "expected_revision": revision,
        "required_deliveries": [{"name": name, "task_id": task_id} for name, task_id in required],
    })


def terminate(client, base, token, goal_id, revision, outcome, **extra):
    return client.post(f"{base}/goals/{goal_id}/terminate", headers=auth(token), json={
        "request_id": key(), "expected_revision": revision, "outcome": outcome, **extra,
    })


def activate(client, base, token, goal_id, revision=1):
    return client.post(f"{base}/goals/{goal_id}/activate", headers=auth(token), json={
        "request_id": key(), "expected_revision": revision,
    })


def lifecycle(client, base, token, goal_id):
    response = client.get(f"{base}/goals/{goal_id}", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def deliveries(client, base, token):
    return ok(client.get(f"{base}/deliveries", headers=auth(token)))


def test_acceptance_revision_required_results_blockers_freeze_and_operator_read(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal_id = context(client)
        lead_id = lead["member_id"]
        owner, reviewer = others[:2]
        first, first_version = accept_task(client, base, tokens, goal_id, lead_id, lead, owner, reviewer)
        second = register(client, base, lead_id, tokens, goal_id, owner["member_id"], reviewer["member_id"])

        denied = acceptance(client, base, tokens[owner["member_id"]], goal_id, 1,
                            [("first", first), ("second", second)])
        assert denied.status_code == 403
        updated = acceptance(client, base, tokens[lead_id], goal_id, 1,
                             [("accepted result", first), ("pending result", second)])
        assert updated.status_code == 201, updated.text
        assert updated.json()["revision"] == 2
        replay = updated = client.post(f"{base}/goals/{goal_id}/acceptance", headers=auth(tokens[lead_id]), json={
            "request_id": "not-the-same", "expected_revision": 1,
            "required_deliveries": [{"name": "accepted result", "task_id": first},
                                    {"name": "pending result", "task_id": second}],
        })
        assert replay.status_code == 409

        pending = terminate(client, base, tokens[lead_id], goal_id, 2, "completed",
                            final_version_ids=[first_version["version_id"]])
        assert pending.status_code == 409
        assert "acceptance" in pending.text.lower() or "required" in pending.text.lower()

        second_endpoint = f"{base}/tasks/{second}"
        second_version = publish(client, base, owner, [lead, reviewer], b"second accepted result")
        submitted = ok(submit(client, second_endpoint, tokens[owner["member_id"]], submission(second_version)))
        ok(review(client, second_endpoint, tokens[reviewer["member_id"]], submitted["submission_id"], decision()))
        missing = terminate(client, base, tokens[lead_id], goal_id, 2, "completed",
                            final_version_ids=[first_version["version_id"]])
        assert missing.status_code == 409
        assert "final" in missing.text.lower() or "version" in missing.text.lower()

        owner_notice = next(item for item in deliveries(client, base, tokens[owner["member_id"]])
                            if item["purpose"] in {"task.assigned", "task.updated"})
        question = ask(client, owner, key(), owner_notice["message_id"], "Is the final format acceptable?")
        blocked = terminate(client, base, tokens[lead_id], goal_id, 2, "completed",
                            final_version_ids=[first_version["version_id"], second_version["version_id"]])
        assert blocked.status_code == 409
        assert "question" in blocked.text.lower() or "block" in blocked.text.lower()
        answer = send(client, base, OPERATOR, request_id=key(), text="Use the published format",
                      reply_to=question["question"]["question_id"])

        close_body = {
            "request_id": "model-close-once", "expected_revision": 2, "outcome": "completed",
            "final_version_ids": [first_version["version_id"], second_version["version_id"]],
        }
        collaboration = client.app.state.collaboration
        result = client.portal.call(collaboration.execute_tool, lead["session_id"], "close_goal",
                                    json.dumps({**close_body, "goal_id": goal_id}))
        assert result["ok"], result
        receipt = result["result"]
        assert receipt["status"] == "completed"
        assert receipt["terminal_id"]
        assert receipt["frozen_summary"]["required_deliveries"] == 2
        assert receipt["frozen_summary"]["accepted_deliveries"] == 2
        assert client.post(f"{base}/goals/{goal_id}/terminate", headers=auth(tokens[lead_id]),
                           json=close_body).json() == receipt
        conflict = terminate(client, base, tokens[lead_id], goal_id, 2, "completed",
                             final_version_ids=[first_version["version_id"], second_version["version_id"]])
        assert conflict.status_code == 409

        state = lifecycle(client, base, tokens[lead_id], goal_id)
        assert state["goal"]["status"] == "completed"
        assert state["acceptance"]["revision"] == 2
        assert state["terminal"]["outcome"] == "completed"
        assert state["terminal"]["actor_kind"] == "lead"
        assert state["terminal"]["server_time"]
        assert {item["task_id"] for item in state["terminal"]["frozen"]["required_deliveries"]} == {first, second}
        assert set(state["terminal"]["frozen"]["accepted_task_ids"]) >= {first, second}
        lead_terminal = [item for item in deliveries(client, base, tokens[lead_id])
                         if item["message_id"] == receipt["notification_message_id"]]
        assert len(lead_terminal) == 1
        assert not [item for item in deliveries(client, base, tokens[owner["member_id"]])
                    if item["purpose"] == "clarification.resume" and item["message_id"] == answer["message_id"]
                    and item["status"] == "pending"]

        # Frozen final versions are the only artifacts the local user can read directly.
        allowed = client.get(f"/v1/artifacts/{first_version['artifact_id']}/versions/{first_version['version']}",
                             headers=auth(OPERATOR))
        assert allowed.status_code == 200 and allowed.content == b"first accepted result"
        private = publish(client, base, owner, [], b"not a frozen final version")
        denied_artifact = client.get(f"/v1/artifacts/{private['artifact_id']}/versions/{private['version']}",
                                     headers=auth(OPERATOR))
        assert denied_artifact.status_code == 404
        terminal_id = receipt["terminal_id"]

    with TestClient(app(tmp_path)) as recovered:
        restored = lifecycle(recovered, base, tokens[lead_id], goal_id)
        assert restored["goal"]["status"] == "completed"
        assert restored["terminal"]["terminal_id"] == terminal_id
        assert restored["terminal"]["frozen"]["raw_counts"]["accepted_tasks"] == 2
        assert any(item["id"] == goal_id and item["status"] == "completed"
                   for item in snapshot(recovered, base, tokens[lead_id])["goals"])
        allowed = recovered.get(f"/v1/artifacts/{first_version['artifact_id']}/versions/{first_version['version']}",
                                headers=auth(OPERATOR))
        assert allowed.status_code == 200 and allowed.content == b"first accepted result"


def test_registered_terminal_outcomes_permissions_and_concurrent_cancellation(tmp_path):
    application = create_app(model=Model(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                             operator_token=OPERATOR, host_token=HOST_TOKEN, schedule=False)
    with TestClient(application) as client:
        base, lead, _, tokens = setup_team(client)
        lead_token = tokens[lead["member_id"]]
        failed_goal = goal(client, base, lead_token, assignment(client, base, "Will fail"))
        cancelled_goal = goal(client, base, lead_token, assignment(client, base, "Will be cancelled"))
        timed_goal = goal(client, base, lead_token, assignment(client, base, "Will time out"))
        assert failed_goal["status"] == "active"
        assert cancelled_goal["status"] == timed_goal["status"] == "queued"

        no_evidence = terminate(client, base, lead_token, failed_goal["goal_id"], 1, "failed",
                                reason="Unrecoverable dependency failure")
        assert no_evidence.status_code == 422
        operator_close = terminate(client, base, OPERATOR, failed_goal["goal_id"], 1, "completed",
                                   final_version_ids=[])
        assert operator_close.status_code in (403, 422)
        lead_cancel = terminate(client, base, lead_token, failed_goal["goal_id"], 1, "cancelled")
        assert lead_cancel.status_code == 403
        failed = terminate(client, base, lead_token, failed_goal["goal_id"], 1, "failed",
                           reason="Required dependency is permanently unavailable",
                           evidence=["Provider returned a terminal configuration error"])
        assert failed.status_code == 201, failed.text
        assert failed.json()["status"] == "failed"
        fact = lifecycle(client, base, lead_token, failed_goal["goal_id"])["terminal"]
        assert (fact["outcome"], fact["actor_kind"], fact["reason"]) == (
            "failed", "lead", "Required dependency is permanently unavailable")
        assert fact["evidence"] == ["Provider returned a terminal configuration error"]

        activated = activate(client, base, lead_token, cancelled_goal["goal_id"])
        assert activated.status_code == 201, activated.text
        with ThreadPoolExecutor(max_workers=2) as workers:
            attempts = [workers.submit(terminate, client, base, OPERATOR, cancelled_goal["goal_id"], 1, "cancelled",
                                       reason="User withdrew the task")
                        for _ in range(2)]
            statuses = sorted(attempt.result().status_code for attempt in attempts)
        assert statuses == [201, 409]
        cancelled = lifecycle(client, base, lead_token, cancelled_goal["goal_id"])["terminal"]
        assert (cancelled["outcome"], cancelled["actor_kind"], cancelled["reason"]) == (
            "cancelled", "user", "User withdrew the task")

        activated = activate(client, base, lead_token, timed_goal["goal_id"])
        assert activated.status_code == 201, activated.text
        team_id = base.rsplit("/", 1)[-1]
        timed = client.post(
            f"/v1/host/teams/{team_id}/goals/{timed_goal['goal_id']}/terminal-events",
            headers=auth(HOST_TOKEN),
            json={
                "event_key": key(), "expected_revision": 1, "outcome": "timed_out",
                "reason": "Goal exceeded its execution deadline",
                "evidence": ["Persistent scheduler deadline facts reached the configured limit"],
            },
        )
        assert timed.status_code == 201, timed.text
        assert timed.json()["status"] == "timed_out"
        timeout_fact = lifecycle(client, base, lead_token, timed_goal["goal_id"])["terminal"]
        assert (timeout_fact["outcome"], timeout_fact["actor_kind"]) == ("timed_out", "host")
        assert timeout_fact["server_time"] and timeout_fact["evidence"]


def test_pre_registration_termination_blocks_registration_and_late_answer_reopens_nothing(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, _, tokens = setup_team(client)
        lead_token = tokens[lead["member_id"]]
        source = assignment(client, base, "Clarify before registration")
        question = ask(client, lead, key(), source["message_id"], "Which acceptance rule applies?")
        termination = client.post(f"{base}/assignments/{source['message_id']}/terminate",
                                  headers=auth(OPERATOR), json={
                                      "request_id": key(),
                                      "reason": "The user cancelled before registration",
                                  })
        assert termination.status_code == 201, termination.text
        fact = termination.json()
        assert fact["status"] == "cancelled" and fact["goal_id"] is None
        assert fact["actor_kind"] == "user"
        read = client.get(f"{base}/assignments/{source['message_id']}/lifecycle", headers=auth(OPERATOR))
        assert read.status_code == 200 and read.json()["terminal"]["terminal_id"] == fact["terminal_id"]
        assert read.json()["goal"] is None

        registration = client.post(f"{base}/goals", headers=auth(lead_token), json={
            "request_id": key(), "source_message_id": source["message_id"],
        })
        assert registration.status_code == 409
        answer = send(client, base, OPERATOR, request_id=key(), text="Late answer after cancellation",
                      reply_to=question["question"]["question_id"])
        questions = {item["question_id"]: item for item in snapshot(client, base, OPERATOR)["questions"]}
        assert questions[question["question"]["question_id"]]["answer_message_id"] == answer["message_id"]
        assert not any(item["purpose"] == "clarification.resume" and item["message_id"] == answer["message_id"]
                       for item in deliveries(client, base, lead_token))
        assert read.json()["terminal"]["frozen"]["open_question_ids"] == [question["question"]["question_id"]]


def test_queue_prerequisites_earliest_eligible_activation_and_open_scope_cleanup(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        lead_id = lead["member_id"]
        lead_token = tokens[lead_id]
        source = assignment(client, base, "Active goal")
        first = goal(client, base, lead_token, source)
        waiting_source = assignment(client, base, "Waits for prerequisite")
        later_source = assignment(client, base, "Eligible next goal")
        prerequisite_source = assignment(client, base, "The prerequisite")
        waiting = goal(client, base, lead_token, waiting_source, prerequisites=[first["goal_id"]])
        later = goal(client, base, lead_token, later_source)
        prerequisite = goal(client, base, lead_token, prerequisite_source)
        # A later goal that depends on the queued prerequisite stays ineligible.
        linked = goal(client, base, lead_token, assignment(client, base, "Dependent goal"),
                      prerequisites=[prerequisite["goal_id"]])
        assert linked["status"] == "queued"

        assert terminate(client, base, OPERATOR, first["goal_id"], 1, "cancelled").status_code == 201
        not_ready = activate(client, base, lead_token, linked["goal_id"])
        assert not_ready.status_code == 409
        with ThreadPoolExecutor(max_workers=2) as workers:
            attempts = {
                goal_id: workers.submit(activate, client, base, lead_token, goal_id)
                for goal_id in (later["goal_id"], prerequisite["goal_id"])
            }
            responses = {goal_id: attempt.result() for goal_id, attempt in attempts.items()}
        assert responses[later["goal_id"]].status_code == 201, responses[later["goal_id"]].text
        assert responses[prerequisite["goal_id"]].status_code == 409
        assert activate(client, base, lead_token, waiting["goal_id"]).status_code == 409

        owner, reviewer = others[:2]
        draft = create(client, base, lead_token, later["goal_id"],
                       terms(owner["member_id"], reviewer["member_id"]))
        assert terminate(client, base, OPERATOR, later["goal_id"], 1, "cancelled").status_code == 201
        proposal = client.get(f"/v1/proposals/{draft['proposal_id']}", headers=auth(lead_token))
        assert proposal.status_code == 200
        assert proposal.json()["status"] == "scope_closed"
        assert activate(client, base, lead_token, prerequisite["goal_id"]).status_code == 201
        assert terminate(client, base, OPERATOR, prerequisite["goal_id"], 1, "cancelled").status_code == 201
        assert activate(client, base, lead_token, linked["goal_id"]).status_code == 409


def test_running_assistant_scope_is_stopped_and_late_tool_write_is_audited(tmp_path):
    from collaboration_runtime.fork_messages import ForkMessageInput
    from collaboration_runtime.scheduler import DeliveryScheduler
    from test_forks import terminal, start_turn

    class BlockingModel:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        async def complete(self, messages, tools):
            text = messages[-1].get("content") or ""
            if "BLOCK_ASSISTANT" in text:
                self.started.set()
                await asyncio.to_thread(self.release.wait, 8)
                return {"role": "assistant", "content": "late assistant completion"}
            return {"role": "assistant", "content": "completed"}

    model = BlockingModel()
    application = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                             operator_token=OPERATOR, schedule=False)
    with TestClient(application) as client:
        base, lead, others, tokens, goal_id = context(client)
        lead_token = tokens[lead["member_id"]]
        anchor = start_turn(client, lead, lead_token, "assistant goal anchor")
        assert terminal(client, lead["session_id"], lead_token, anchor)["status"] == "completed"
        helper = ok(client.post(f"/v1/sessions/{lead['session_id']}/forks", headers=auth(lead_token), json={
            "request_id": key(), "from_turn_id": anchor, "goal_id": goal_id,
            "purpose": "BLOCK_ASSISTANT", "scope_id": key(), "history_turn_ids": [anchor],
            "version_ids": [], "allowed_tools": ["send_fork_message"], "contact_session_ids": [],
        }))["session_id"]
        queued = client.portal.call(client.app.state.runtime.fork_messages.post, helper, helper,
                                    ForkMessageInput(request_id=key(), text="queued assistant result",
                                                     recipient_session_id=lead["session_id"]))
        runtime = client.app.state.runtime
        scheduler = DeliveryScheduler(runtime, client.app.state.collaboration, client.app.state.workspace)
        claim = client.portal.call(scheduler._claim_assistant)
        assert claim and claim[0] == helper
        client.portal.call(runtime.launch, *claim)
        try:
            assert model.started.wait(8), "assistant turn did not start"
            accepted = terminate(client, base, OPERATOR, goal_id, 1, "cancelled",
                                 reason="No more assistant work is required")
            assert accepted.status_code == 201, accepted.text
            state = client.portal.call(lambda: tuple(runtime.db.execute(
                "SELECT f.status, j.status FROM assistant_forks f JOIN assistant_jobs j USING(session_id) "
                "WHERE f.session_id = ? ORDER BY j.job_id DESC LIMIT 1", (helper,)).fetchone()))
            assert state == ("revoked", "interrupted")
            late = client.post(f"/v1/forks/{helper}/messages", headers=auth(lead_token), json={
                "request_id": key(), "text": "late tool output",
            })
            assert late.status_code == 409
            assert client.portal.call(client.app.state.goal_lifecycle.scope_is_open, goal_id) is False
            assert isinstance(queued, dict) and "message_id" in queued, queued
            assert queued["message_id"] in {item["id"] for item in
                                           snapshot(client, base, lead_token)["messages"]}
        finally:
            model.release.set()


def test_goal_scope_stops_recoverable_member_turn_and_preserves_other_work(tmp_path):
    from collaboration_runtime.scheduler import DeliveryScheduler

    class CompletingModel:
        def __init__(self):
            self.inputs = []

        async def complete(self, messages, tools):
            self.inputs.append(messages[-1].get("content") or "")
            return {"role": "assistant", "content": "completed"}

    model = CompletingModel()
    application = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                             operator_token=OPERATOR, schedule=False)
    with TestClient(application) as client:
        base, lead, others, tokens = setup_team(client)
        lead_token = tokens[lead["member_id"]]
        active_source = assignment(client, base, "Active scoped goal")
        active_goal = goal(client, base, lead_token, active_source)
        queued_source = assignment(client, base, "Queued independent goal")
        queued_goal = goal(client, base, lead_token, queued_source)
        assert active_goal["status"] == "active" and queued_goal["status"] == "queued"

        runtime = client.app.state.runtime
        scheduler = DeliveryScheduler(runtime, client.app.state.collaboration, client.app.state.workspace)

        async def drain(claim):
            while job := claim():
                runtime.launch(*job)
                await runtime.active[job[0]].task

        client.portal.call(drain, scheduler._claim_one)
        model.inputs.clear()

        scoped_member, independent_member = others[:2]
        scoped = send(client, base, OPERATOR, request_id=key(), text="Only for the active goal",
                      kind="supplement", reply_to=active_source["message_id"],
                      mentions=[scoped_member["member_id"]])
        other_goal = send(client, base, OPERATOR, request_id=key(), text="Only for the queued goal",
                          kind="supplement", reply_to=queued_source["message_id"],
                          mentions=[scoped_member["member_id"]])
        independent = send(client, base, OPERATOR, request_id=key(), text="Independent conversation",
                           recipient_id=independent_member["member_id"])

        claim = client.portal.call(scheduler._claim_one)
        assert claim and claim[0] == scoped_member["session_id"]
        scoped_turn = claim[1]
        claimed_message = client.portal.call(lambda: runtime.db.execute("""
            SELECT d.message_id FROM scheduled_turns s JOIN message_deliveries d
                USING (event_id, session_id, purpose)
            WHERE s.turn_id = ?
        """, (scoped_turn,)).fetchone()["message_id"])
        assert claimed_message == scoped["message_id"]
        client.portal.call(lambda: runtime._finish(scoped_turn, "recoverable", error="fixture restart"))

        def delivery_turn(message_id):
            return client.portal.call(lambda: tuple(runtime.db.execute("""
                SELECT d.status, t.status, c.generation
                FROM message_deliveries d
                LEFT JOIN scheduled_turns s USING (event_id, session_id, purpose)
                LEFT JOIN turns t ON t.id = s.turn_id
                LEFT JOIN execution_checkpoints c ON c.turn_id = t.id
                WHERE d.message_id = ?
            """, (message_id,)).fetchone()))

        assert delivery_turn(scoped["message_id"]) == ("running", "recoverable", 1)
        cancelled = terminate(client, base, OPERATOR, active_goal["goal_id"], 1, "cancelled",
                              reason="The user ended this goal")
        assert cancelled.status_code == 201, cancelled.text
        assert delivery_turn(scoped["message_id"]) == ("cancelled", "interrupted", 2)
        assert delivery_turn(other_goal["message_id"]) == ("pending", None, None)
        assert delivery_turn(independent["message_id"]) == ("pending", None, None)

        next_claim = client.portal.call(scheduler._claim_one)
        assert next_claim and next_claim[0] == scoped_member["session_id"]
        next_message = client.portal.call(lambda: runtime.db.execute("""
            SELECT d.message_id FROM scheduled_turns s JOIN message_deliveries d
                USING (event_id, session_id, purpose)
            WHERE s.turn_id = ?
        """, (next_claim[1],)).fetchone()["message_id"])
        assert next_message == other_goal["message_id"]
        assert model.inputs == []


def test_goal_scope_stops_running_member_turn_before_late_model_result(tmp_path):
    from collaboration_runtime.scheduler import DeliveryScheduler

    class BlockingModel:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        async def complete(self, messages, tools):
            text = messages[-1].get("content") or ""
            if "BLOCK_MEMBER" in text:
                self.started.set()
                await asyncio.to_thread(self.release.wait, 8)
                return {"role": "assistant", "content": "late member completion"}
            return {"role": "assistant", "content": "completed"}

    model = BlockingModel()
    application = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                             operator_token=OPERATOR, schedule=False)
    with TestClient(application) as client:
        base, lead, others, tokens = setup_team(client)
        lead_token = tokens[lead["member_id"]]
        source = assignment(client, base, "Member work will be stopped")
        target = goal(client, base, lead_token, source)
        member = others[0]
        runtime = client.app.state.runtime
        scheduler = DeliveryScheduler(runtime, client.app.state.collaboration, client.app.state.workspace)

        async def drain(claim):
            while job := claim():
                runtime.launch(*job)
                await runtime.active[job[0]].task

        client.portal.call(drain, scheduler._claim_one)
        message = send(client, base, OPERATOR, request_id=key(), text="BLOCK_MEMBER",
                       kind="supplement", reply_to=source["message_id"], mentions=[member["member_id"]])
        claim = client.portal.call(scheduler._claim_one)
        assert claim and claim[0] == member["session_id"]
        client.portal.call(runtime.launch, *claim)
        try:
            assert model.started.wait(8), "member turn did not start"
            cancelled = terminate(client, base, OPERATOR, target["goal_id"], 1, "cancelled",
                                  reason="Stop this goal")
            assert cancelled.status_code == 201, cancelled.text
        finally:
            model.release.set()

        deadline = time.monotonic() + 8
        while client.portal.call(lambda: member["session_id"] in runtime.active):
            assert time.monotonic() < deadline, "terminated member turn did not stop"
            time.sleep(0.02)

        state = client.portal.call(lambda: tuple(runtime.db.execute("""
            SELECT d.status, t.status, c.generation
            FROM message_deliveries d
            JOIN scheduled_turns s USING (event_id, session_id, purpose)
            JOIN turns t ON t.id = s.turn_id
            JOIN execution_checkpoints c ON c.turn_id = t.id
            WHERE d.message_id = ?
        """, (message["message_id"],)).fetchone()))
        assert state == ("cancelled", "interrupted", 2)
        assert not any(item["text"] == "late member completion"
                       for item in snapshot(client, base, lead_token)["messages"])


def test_host_terminal_event_requires_host_credential_and_is_idempotent(tmp_path):
    application = create_app(model=Model(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                             operator_token=OPERATOR, host_token=HOST_TOKEN, schedule=False)
    with TestClient(application) as client:
        base, lead, _, tokens = setup_team(client)
        lead_token = tokens[lead["member_id"]]
        spoof_source = assignment(client, base, "Public deadline spoof")
        spoof = client.post(f"{base}/goals", headers=auth(lead_token), json={
            "request_id": key(), "source_message_id": spoof_source["message_id"],
            "deadline_at": "2000-01-01T00:00:00+00:00",
        })
        assert spoof.status_code == 422
        target = goal(client, base, lead_token, assignment(client, base, "Host will fail this goal"))
        assert terminate(client, base, lead_token, target["goal_id"], 1, "timed_out").status_code == 422
        team_id = base.rsplit("/", 1)[-1]
        endpoint = f"/v1/host/teams/{team_id}/goals/{target['goal_id']}/terminal-events"
        body = {
            "event_key": "durable-host-fault-1", "expected_revision": 1,
            "outcome": "failed", "reason": "The execution host lost its durable workspace",
            "evidence": ["Workspace volume was removed and cannot be restored"],
        }

        assert client.post(endpoint, headers=auth(lead_token), json=body).status_code == 401
        assert client.post(endpoint, headers=auth(OPERATOR), json=body).status_code == 401
        assert client.post(
            f"{base}/goals/{target['goal_id']}/terminate",
            headers=auth(HOST_TOKEN),
            json={"request_id": key(), "expected_revision": 1, "outcome": "cancelled"},
        ).status_code == 401

        first = client.post(endpoint, headers=auth(HOST_TOKEN), json=body)
        assert first.status_code == 201, first.text
        receipt = first.json()
        assert (receipt["status"], receipt["actor_kind"]) == ("failed", "host")
        assert receipt["host_event_key"] == body["event_key"]
        replay = client.post(endpoint, headers=auth(HOST_TOKEN), json=body)
        assert replay.status_code == 201
        assert replay.json() == receipt
        changed = client.post(endpoint, headers=auth(HOST_TOKEN), json={
            **body, "evidence": ["Different evidence for the same host event"],
        })
        assert changed.status_code == 409

        fact = lifecycle(client, base, lead_token, target["goal_id"])["terminal"]
        assert (fact["outcome"], fact["actor_kind"], fact["host_event_key"]) == (
            "failed", "host", body["event_key"],
        )
        notices = client.portal.call(lambda: client.app.state.runtime.db.execute("""
            SELECT status FROM message_deliveries WHERE message_id = ?
        """, (receipt["notification_message_id"],)).fetchall())
        assert [row["status"] for row in notices] == ["pending"]

    recovered_app = create_app(model=Model(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                               operator_token=OPERATOR, host_token=HOST_TOKEN, schedule=False)
    with TestClient(recovered_app) as recovered:
        replay = recovered.post(endpoint, headers=auth(HOST_TOKEN), json=body)
        assert replay.status_code == 201
        assert replay.json() == receipt
        assert lifecycle(recovered, base, lead_token, target["goal_id"])["terminal"]["terminal_id"] == (
            receipt["terminal_id"]
        )


def test_scheduler_deadline_expiry_writes_trusted_timed_out_fact(tmp_path):
    class CountingModel:
        def __init__(self):
            self.inputs = []

        async def complete(self, messages, tools):
            self.inputs.append(messages[-1].get("content") or "")
            return {"role": "assistant", "content": "completed"}

    model = CountingModel()
    setup_app = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                           operator_token=OPERATOR, goal_timeout_seconds=0.05, schedule=False)
    with TestClient(setup_app) as client:
        base, lead, _, tokens = setup_team(client)
        lead_token = tokens[lead["member_id"]]
        expired = goal(client, base, lead_token, assignment(client, base, "Persisted deadline"))
        assert expired["status"] == "active"
        team_id = base.rsplit("/", 1)[-1]
    time.sleep(0.06)

    scheduled_app = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                               operator_token=OPERATOR, goal_timeout_seconds=0.05, schedule=True)
    with TestClient(scheduled_app) as client:
        deadline = time.monotonic() + 8
        state = None
        while time.monotonic() < deadline:
            response = client.get(f"{base}/goals/{expired['goal_id']}", headers=auth(lead_token))
            assert response.status_code == 200, response.text
            state = response.json()
            if state["terminal"] is not None:
                break
            time.sleep(0.02)
        assert state["terminal"] is not None, "scheduler did not expire the persisted deadline"
        assert (state["terminal"]["outcome"], state["terminal"]["actor_kind"]) == ("timed_out", "host")
        assert state["terminal"]["host_event_key"].startswith(f"deadline:{expired['goal_id']}:")
        assert "宿主策略 host_policy" in state["terminal"]["evidence"][0]
        assert not any("Persisted deadline" in item for item in model.inputs)
