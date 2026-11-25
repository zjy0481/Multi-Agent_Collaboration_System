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
