"""Regression coverage for persisted yielded delivery recovery and goal closure."""
from fastapi.testclient import TestClient

from collaboration_runtime.scheduler import DeliveryScheduler
from test_collaboration import OPERATOR, app, send, setup_team
from test_goal_lifecycle import assignment, goal, terminate


def _claim_and_yield(client, lead):
    state = client.app.state
    scheduler = DeliveryScheduler(state.runtime, state.collaboration, state.workspace)

    def prepare():
        session, turn_id, _, _ = scheduler._claim_one()
        assert session == lead["session_id"]
        db = state.runtime.db
        db.execute("UPDATE turns SET status='yielded' WHERE id=?", (turn_id,))
        db.commit()
        delivery = db.execute("SELECT status FROM message_deliveries d JOIN scheduled_turns s "
                              "USING(event_id, session_id, purpose) WHERE s.turn_id=?", (turn_id,)).fetchone()
        assert delivery["status"] == "running"
        return turn_id, state.runtime.journal.checkpoint(turn_id)["generation"]

    return client.portal.call(prepare)


def test_yielded_delivery_survives_restart_and_reopens(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, _, tokens = setup_team(client)
        source = assignment(client, base, "Persisted work")
        goal(client, base, tokens[lead["member_id"]], source)
        turn_id, generation = _claim_and_yield(client, lead)

    with TestClient(app(tmp_path)) as client:
        state = client.app.state
        scheduler = DeliveryScheduler(state.runtime, state.collaboration, state.workspace)

        def reclaim():
            assert state.runtime.db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()[0] == "yielded"
            claimed = scheduler._claim_one()
            assert claimed[1] == turn_id
            assert state.runtime.db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()[0] == "running"
            return state.runtime.journal.checkpoint(turn_id)["generation"]

        assert client.portal.call(reclaim) == generation + 1


def test_goal_termination_cancels_yielded_delivery(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, _, tokens = setup_team(client)
        source = assignment(client, base, "Work to cancel")
        registered = goal(client, base, tokens[lead["member_id"]], source)
        turn_id, generation = _claim_and_yield(client, lead)
        response = terminate(client, base, OPERATOR, registered["goal_id"], 1, "cancelled")
        assert response.status_code == 201, response.text
        state = client.app.state
        scheduler = DeliveryScheduler(state.runtime, state.collaboration, state.workspace)

        def inspect():
            db = state.runtime.db
            status = db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()[0]
            delivery = db.execute("SELECT status FROM message_deliveries d JOIN scheduled_turns s "
                                  "USING(event_id, session_id, purpose) WHERE s.turn_id=?", (turn_id,)).fetchone()[0]
            next_item = scheduler._claim_one()
            return status, delivery, state.runtime.journal.checkpoint(turn_id)["generation"], next_item

        status, delivery, after, next_item = client.portal.call(inspect)
        assert status == "interrupted"
        assert delivery == "cancelled"
        assert after > generation
        assert next_item is None or next_item[1] != turn_id


def test_new_priority_traffic_does_not_starve_yielded_chat(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, _, tokens = setup_team(client)
        state = client.app.state
        scheduler = DeliveryScheduler(state.runtime, state.collaboration, state.workspace)
        source = assignment(client, base, "An active goal")
        registered = goal(client, base, tokens[lead["member_id"]], source)

        def complete(turn_id):
            with state.runtime.db:
                state.runtime._write_finish(turn_id, "completed")

        initial = client.portal.call(scheduler._claim_one)
        client.portal.call(complete, initial[1])
        message = send(client, base, OPERATOR, request_id="ordinary", text="Ordinary work",
                       goal_id=registered["goal_id"])
        ordinary = client.portal.call(scheduler._claim_one)
        old = send(client, base, OPERATOR, request_id="old", text="Already waiting",
                   kind="supplement", reply_to=source["message_id"])

        def yield_chat():
            with state.runtime.db:
                state.runtime.db.execute("UPDATE turns SET status='yielded' WHERE id=?", (ordinary[1],))
                state.runtime._event(ordinary[1], "turn.delivery_yielded", queued_through=old["event_id"])

        client.portal.call(yield_chat)
        send(client, base, OPERATOR, request_id="new", text="Later correction",
             kind="correction", reply_to=message["message_id"], mentions=[lead["member_id"]])
        priority = client.portal.call(scheduler._claim_one)
        assert priority[1] != ordinary[1]
        client.portal.call(complete, priority[1])
        resumed = client.portal.call(scheduler._claim_one)
        assert resumed[1] == ordinary[1]


def test_displaced_formal_delivery_has_one_bounded_safe_continuation(tmp_path):
    import json
    from test_collaboration import send
    from test_delivery_priority import run_one
    from test_delegation import context
    from test_scheduler import tool
    from test_workspace import MemorySandbox

    class Model:
        count = 0

        async def complete(self, messages, tools):
            self.count += 1
            if self.count == 1:
                return tool("publish_artifact", "publish-once", {"path": "result.txt", "reader_session_ids": []})
            return tool("list_artifacts", f"list-{self.count}", {})

    with TestClient(app(tmp_path)) as client:
        base, lead, _, _, goal_id = context(client)
        state = client.app.state
        queue = DeliveryScheduler(state.runtime, state.collaboration, state.workspace)
        queue.runtime.max_steps = 1
        queue.runtime.model = Model()
        workspace = state.workspace
        workspace.sandbox = MemorySandbox()
        workspace.sandbox.prepare(client.portal.call(workspace.actor, lead["session_id"]).workspace_id)
        assert client.portal.call(workspace.execute, lead["session_id"], "create_file",
                                  json.dumps({"path": "result.txt", "content": "once"}))["ok"]
        target = client.portal.call(queue._claim_one)
        ordinary = send(client, base, OPERATOR, request_id="older-work", text="Earlier ordinary work",
                        goal_id=goal_id)

        def displace():
            db = queue.db
            with db:
                from uuid import uuid4
                old_id = uuid4().hex
                db.execute("INSERT INTO turns(id,role,status) VALUES (?,?,'yielded')", (old_id, lead["session_id"]))
                queue.runtime.journal.create(old_id, [{"role": "user", "content": "earlier"}])
                db.execute("INSERT INTO scheduled_turns(turn_id,event_id,session_id,purpose) VALUES (?,?,?,?)",
                           (old_id, ordinary["event_id"], lead["session_id"], "message"))
                db.execute("UPDATE message_deliveries SET status='running' WHERE event_id=? AND session_id=?",
                           (ordinary["event_id"], lead["session_id"]))
                db.execute("UPDATE turns SET status='yielded' WHERE id=?", (old_id,))
                event = db.execute("SELECT event_id FROM scheduled_turns WHERE turn_id=?", (target[1],)).fetchone()[0]
                queue.runtime._event(old_id, "turn.delivery_yielded", event_id=event, queued_through=event)
            return old_id

        old_id = client.portal.call(displace)

        async def launch_target():
            queue.runtime.launch(*target)
            await queue.runtime.active[target[0]].task

        client.portal.call(launch_target)
        assert client.portal.call(run_one, queue) == target[1]
        assert client.portal.call(run_one, queue) == target[1]

        def inspect():
            db = queue.db
            return (db.execute("SELECT status FROM turns WHERE id=?", (target[1],)).fetchone()[0],
                    db.execute("SELECT count(*) FROM artifact_versions").fetchone()[0],
                    [json.loads(x[0])["source"] for x in db.execute(
                        "SELECT payload FROM events WHERE turn_id=? AND type='model.retry_requested' ORDER BY seq",
                        (target[1],))], queue._claim_one()[1])

        status, versions, sources, next_turn = client.portal.call(inspect)
        assert (status, versions) == ("failed", 1)
        assert sources == ["automatic", "automatic_displaced"]
        assert next_turn == old_id
