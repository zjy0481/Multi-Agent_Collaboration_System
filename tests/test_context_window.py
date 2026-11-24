"""Model input bounds and persistent, authorized evidence."""
import json

from fastapi.testclient import TestClient

from collaboration_runtime.context_window import bounded_messages, size
from test_collaboration import OPERATOR, app, send
from test_delegation import context, key, ok, terms
from test_task_results import decision, publish, register, review, submission, submit
from test_delivery_priority import run_one, scheduler
from test_scheduler import tool


def test_history_budget_preserves_delivery_and_audit(tmp_path):
    captured = []

    class Model:
        async def complete(self, messages, tools):
            captured.append(messages)
            return {"role": "assistant", "content": "x" * 15000}

    with TestClient(app(tmp_path)) as client:
        base, lead, _, _, goal = context(client)
        queue = scheduler(client)
        queue.runtime.model = Model()
        queue.runtime.context_chars = 32000
        client.portal.call(run_one, queue)
        for i in range(4):
            send(client, base, OPERATOR, request_id=key(), text=f"current decision {i}", goal_id=goal,
                 mentions=[lead["member_id"]])
            client.portal.call(run_one, queue)
        final = captured[-1]
        assert len(json.dumps(final, ensure_ascii=False)) <= 32000
        assert "current decision 3" in str(final) and goal in str(final)
        assert client.portal.call(lambda: queue.db.execute("SELECT COUNT(*) FROM turns WHERE length(history)>15000").fetchone()[0]) == 5


def test_compacted_state_discloses_only_authorized_approval(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        lid, oid, qid = (member["member_id"] for member in (lead, owner, reviewer))
        task = register(client, base, lid, tokens, goal, terms(oid, qid))
        artifact = publish(client, base, owner, [reviewer], b"private evidence")
        endpoint = f"{base}/tasks/{task}"
        sent = ok(submit(client, endpoint, tokens[oid], submission(artifact)))
        approved = ok(review(client, endpoint, tokens[qid], sent["submission_id"], decision()))
        runtime = client.app.state.runtime

        def compact(member):
            return client.portal.call(runtime._context_state, base.rsplit("/", 1)[-1], member["session_id"], goal, set())

        authorized = compact(reviewer)["tasks"][0]
        assert authorized["approved_versions"] == [artifact["version_id"]]
        assert authorized["submission_id"] == sent["submission_id"] and authorized["review_id"] == approved["review_id"]
        masked = compact(lead)
        assert artifact["version_id"] not in str(masked) and sent["submission_id"] not in str(masked)


def test_latest_tool_group_survives_large_output_and_arguments():
    system = {"role": "system", "content": "instructions"}
    delivery = {"role": "user", "content": "current task"}
    steering = {"role": "user", "content": "use the approved edition"}
    decision = tool("get_task", "lookup", {"task_id": "x" * 8000})
    receipt = {"role": "tool", "tool_call_id": "lookup", "content": "ok " * 15000}
    messages = bounded_messages(system, [], [delivery, decision, receipt, steering], 2400, {"goal_id": "g"})
    assert size(messages) <= 2400 and messages[-1] == steering and delivery in messages
    assert messages[-3]["tool_calls"][0]["id"] == messages[-2]["tool_call_id"] == "lookup"
    assert "omitted_chars" in messages[-2]["content"]
    assert len(receipt["content"]) == 45000
