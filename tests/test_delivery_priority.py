"""Priority and sender-visible delivery state through the scheduler."""
from fastapi.testclient import TestClient

from collaboration_runtime.scheduler import DeliveryScheduler
from test_collaboration import OPERATOR, app, auth, send
from test_delegation import context, key, ok, terms
from test_task_results import decision, publish, register, review, submission, submit


def scheduler(client):
    state = client.app.state
    return DeliveryScheduler(state.runtime, state.collaboration, state.workspace)


async def run_one(queue):
    job = queue._claim_one()
    assert job
    queue.runtime.launch(*job)
    await queue.runtime.active[job[0]].task
    return job[1]


def test_goal_decisions_outrank_ordinary_chat_and_status_is_visible(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        queue = scheduler(client)

        class Reply:
            async def complete(self, messages, tools):
                return {"role": "assistant", "content": "done"}

        queue.runtime.model = Reply()
        client.portal.call(run_one, queue)
        normal = send(client, base, tokens[others[0]["member_id"]], request_id=key(), text="status",
                      mentions=[lead["member_id"]])
        original = client.portal.call(lambda: queue.db.execute("SELECT id FROM team_messages WHERE kind='assignment'").fetchone()[0])
        change = send(client, base, OPERATOR, request_id=key(), text="use approved versions",
                      kind="supplement", reply_to=original, mentions=[lead["member_id"]])
        owner, reviewer = others[:2]
        lid, oid, qid = (member["member_id"] for member in (lead, owner, reviewer))
        task = register(client, base, lid, tokens, goal, terms(oid, qid))
        endpoint = f"{base}/tasks/{task}"
        artifact = publish(client, base, owner, [lead, reviewer], b"approved")
        sent = ok(submit(client, endpoint, tokens[oid], submission(artifact)))
        approved = ok(review(client, endpoint, tokens[qid], sent["submission_id"], decision()))
        turn = client.portal.call(run_one, queue)
        first = client.portal.call(lambda: queue.db.execute("SELECT event_id FROM scheduled_turns WHERE turn_id=?", (turn,)).fetchone()[0])
        assert first == change["event_id"] and first != normal["event_id"]
        result = client.get(f"{base}/messages/{change['message_id']}/deliveries", headers=auth(OPERATOR))
        assert result.status_code == 200
        assert [d["status"] for d in result.json()] == ["completed"]
        approval_delivery = client.portal.call(lambda: queue.db.execute("""
            SELECT d.event_id FROM message_deliveries d JOIN delegation_notifications n USING (message_id)
            WHERE n.event_id=? AND d.session_id=? AND d.purpose='task.approved'
        """, (approved["event_id"], lead["session_id"])).fetchone()[0])
        for _ in range(30):
            client.portal.call(run_one, queue)
            statuses = client.portal.call(lambda: dict(queue.db.execute(
                "SELECT event_id, status FROM message_deliveries WHERE session_id=? AND event_id IN (?, ?)",
                (lead["session_id"], approval_delivery, normal["event_id"])).fetchall()))
            if statuses[approval_delivery] == "completed":
                break
        assert statuses == {approval_delivery: "completed", normal["event_id"]: "pending"}
        restricted = client.get(f"{base}/messages/{change['message_id']}/deliveries", headers=auth(tokens[qid]))
        assert restricted.status_code in (403, 404)
