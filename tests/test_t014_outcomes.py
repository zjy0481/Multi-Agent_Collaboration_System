"""Exercise scheduling, rework, and report counts in one team."""

import json

import pytest
from fastapi.testclient import TestClient

from test_collaboration import OPERATOR, auth, send, setup_team, snapshot
from test_delegation import create, key, ok, terms
from test_evaluation_reports import aggregate
from test_goal_close_runtime import call, create_scheduled_app, wait_until
from test_goal_lifecycle import accept_task, acceptance, assignment, goal, lifecycle
from test_task_results import decision, publish, register, review, submission, submit


class ScheduledOutcomesModel:
    identity = {"mode": "mock", "endpoint": None, "model": "t014-outcomes"}

    def __init__(self):
        self.closures = {}
        self.activation = None
        self.confirmed = {}

    async def complete(self, messages, tools):
        last = messages[-1]
        if last["role"] == "tool":
            if last["tool_call_id"].startswith("close-"):
                assert tools == []
                self.confirmed[last["tool_call_id"]] = json.loads(last["content"])
            return {"role": "assistant", "content": "Recorded"}

        payload = json.loads(last["content"])
        message = payload["message"]
        text = message["text"]
        if text in self.closures:
            calls = [call("close_goal", f"close-{text}", self.closures[text])]
        elif text == "T014: clarify the unregistered assignment":
            calls = [call("ask_human", "question-before-registration", {
                "request_id": "t014-question", "source_message_id": message["id"],
                "text": "What is the required evidence for this assignment?", "private": True,
            })]
        elif payload["delivery"]["purpose"] == "goal.terminal" and self.activation:
            args, self.activation = self.activation, None
            calls = [call("activate_goal", "activate-next", args)]
        else:
            return {"role": "assistant", "content": "Received"}
        return {"role": "assistant", "content": None, "tool_calls": calls}


def reviewed_output(client, base, tokens, task_id, owner, reviewer, version, outcome="approved", revision=1):
    task_url = f"{base}/tasks/{task_id}"
    delivered = ok(submit(client, task_url, tokens[owner["member_id"]], submission(version, revision)))
    return ok(review(client, task_url, tokens[reviewer["member_id"]], delivered["submission_id"],
                     decision(outcome, revision=revision)))


def test_scheduled_queue_rework_and_reports_from_the_same_assignments(tmp_path):
    model = ScheduledOutcomesModel()
    with TestClient(create_scheduled_app(tmp_path, model)) as client:
        base, lead, others, tokens = setup_team(client)
        owner, reviewer, outsider = others
        lead_id = lead["member_id"]
        lead_token = tokens[lead_id]
        first_source = assignment(client, base, "First deliverable")
        second_source = assignment(client, base, "Revision with dependent and independent work")
        first = goal(client, base, lead_token, first_source)["goal_id"]
        second = goal(client, base, lead_token, second_source)
        assert second["status"] == "queued"
        second_id = second["goal_id"]
        task_id, version = accept_task(client, base, tokens, first, lead_id, lead, owner, reviewer)
        ok(acceptance(client, base, lead_token, first, 1, [("Verified first result", task_id)]))
        model.activation = {"request_id": key(), "goal_id": second_id, "expected_revision": 1}
        model.closures["first"] = {"request_id": key(), "goal_id": first, "expected_revision": 2,
                                   "outcome": "completed", "final_version_ids": [version["version_id"]]}
        send(client, base, OPERATOR, request_id=key(), kind="supplement", reply_to=first_source["message_id"], text="first")
        wait_until(lambda: lifecycle(client, base, lead_token, second_id)["goal"]["status"] == "active")
        assert model.confirmed["close-first"]["result"]["status"] == "completed"

        # An unregistered assignment must retain its own question scope while another goal is active.
        third_source = assignment(client, base, "T014: clarify the unregistered assignment")

        def third_question():
            return next((q for q in snapshot(client, base, OPERATOR)["questions"]
                         if q["source_message_id"] == third_source["message_id"]), None)

        question = wait_until(third_question)
        before = ok(client.post(f"{base}/assignments/{third_source['message_id']}/terminate", headers=auth(OPERATOR),
                                json={"request_id": key(), "reason": "User withdrew the third assignment"}))
        late = send(client, base, OPERATOR, request_id=key(), reply_to=question["question_id"], text="Late reply")
        assert lifecycle(client, base, lead_token, second_id)["goal"]["status"] == "active"
        assert not client.portal.call(lambda: client.app.state.runtime.db.execute(
            "SELECT 1 FROM message_deliveries WHERE message_id=? AND purpose='clarification.resume'",
            (late["message_id"],),
        ).fetchone())

        upstream_terms = terms(owner["member_id"], reviewer["member_id"], deliverables=["Upstream result"])
        upstream = register(client, base, lead_id, tokens, second_id, upstream_terms)
        first_version = publish(client, base, owner, [lead, reviewer], b"draft input")
        dependent = register(client, base, lead_id, tokens, second_id, terms(
            reviewer["member_id"], lead_id, deliverables=["Dependent result"], dependency_ids=[upstream],
            input_version_ids=[first_version["version_id"]],
        ))
        independent = register(client, base, lead_id, tokens, second_id, terms(
            reviewer["member_id"], lead_id, deliverables=["Independent result"],
        ))
        original = reviewed_output(client, base, tokens, upstream, owner, reviewer, first_version)
        for task in (dependent, independent):
            material = publish(client, base, reviewer, [lead], task.encode())
            reviewed_output(client, base, tokens, task, reviewer, lead, material)
        register(client, base, lead_id, tokens, second_id, upstream_terms, task_id=upstream, base_revision=1)
        returned = reviewed_output(client, base, tokens, upstream, owner, reviewer, first_version, "returned", revision=2)
        replacement = publish(client, base, owner, [lead, reviewer], b"reworked input", first_version["artifact_id"])
        approved = reviewed_output(client, base, tokens, upstream, owner, reviewer, replacement, revision=2)
        draft = create(client, base, lead_token, second_id, upstream_terms)
        assert ok(client.get(f"{base}/tasks/{dependent}", headers=auth(lead_token)))["status"] == "needs_recheck"
        assert ok(client.get(f"{base}/tasks/{independent}", headers=auth(lead_token)))["status"] == "accepted"

        ok(acceptance(client, base, lead_token, second_id, 1, [("Dependent result", dependent)]))
        model.closures["second"] = {"request_id": key(), "goal_id": second_id, "expected_revision": 2,
                                    "outcome": "failed", "reason": "Dependent review is stale",
                                    "evidence": ["The input version changed after the review."]}
        send(client, base, OPERATOR, request_id=key(), kind="supplement", reply_to=second_source["message_id"], text="second")
        wait_until(lambda: model.confirmed.get("close-second"))
        assert model.confirmed["close-second"]["result"]["status"] == "failed"

        ids = [source["message_id"] for source in (first_source, second_source, third_source)]
        read = ok(aggregate(client, base, lead_token, [*ids, ids[0]]))
        finished, failed, cancelled = read["goals"]
        assert read["assignment_ids"] == ids and read["goal_completion_rate"] == pytest.approx(1 / 3)
        assert [item["status"] for item in read["goals"]] == ["completed", "failed", "cancelled"]
        assert {item["version_id"] for item in finished["references"]["versions"] if item.get("final")} == {version["version_id"]}
        assert failed["counts"] == {"registered_tasks": 3, "accepted_tasks": 2}
        assert failed["subtask_completion_rate"] == pytest.approx(2 / 3)
        assert set(failed["accepted_task_ids"]) == {upstream, independent}
        assert draft["proposal_id"] not in failed["task_ids"]
        assert [item["review_id"] for item in failed["references"]["reviews"] if item["task_id"] == upstream] == [
            original["review_id"], returned["review_id"], approved["review_id"],
        ]
        assert failed["human_waits"] == []
        assert cancelled["terminal"]["terminal_id"] == before["terminal_id"]
        wait, = cancelled["human_waits"]
        assert wait["question_id"] == question["question_id"]
        assert wait["started_at"] <= wait["ended_at"] <= cancelled["terminal"]["server_time"]
        assert ok(aggregate(client, base, tokens[outsider["member_id"]], ids))["goals"][1]["references"]["reviews"] == []
