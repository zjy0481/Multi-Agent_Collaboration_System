"""Offline regression for scheduled goal closure and receipt preservation."""
import json
import asyncio
import threading
import time
from uuid import uuid4

import pytest

from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from test_collaboration import OPERATOR, Sandbox, auth, bootstrap, send, setup_team
from test_goal_lifecycle import accept_task, acceptance, goal, lifecycle


def key():
    return uuid4().hex


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for scheduled goal transition")


def call(name, call_id, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


class ClosingModel:
    """A deterministic double: only the test's trigger can request closure."""
    def __init__(self):
        self.outcome = "completed"
        self.goal_id = None
        self.next_goal_id = None
        self.final_version = None
        self.requested_close = threading.Event()
        self.allow_close = threading.Event()
        self.allow_close.set()
        self.saw_close_receipt = threading.Event()
        self.saw_activation = threading.Event()
        self.confirmation = None

    async def complete(self, messages, tools):
        last = messages[-1]
        if last.get("role") == "tool":
            if last.get("tool_call_id") == "close-call":
                self.confirmation = (messages, tools)
                self.saw_close_receipt.set()
            if last.get("tool_call_id") == "activate-call":
                self.saw_activation.set()
            return {"role": "assistant", "content": "Done"}
        text = str(last.get("content") or "")
        try:
            delivery = json.loads(text).get("delivery", {})
        except (ValueError, AttributeError):
            delivery = {}
        if delivery.get("purpose") == "goal.terminal" and self.next_goal_id:
            return {"role": "assistant", "content": None, "tool_calls": [call("activate_goal", "activate-call", {
                "request_id": "activate-next", "goal_id": self.next_goal_id, "expected_revision": 1,
            })]}
        if "CLOSE_GOAL_TRIGGER" in text:
            self.requested_close.set()
            assert await asyncio.to_thread(self.allow_close.wait, 8)
            return {"role": "assistant", "content": None, "tool_calls": [
                call("close_goal", "close-call", {
                    "request_id": "close-from-supplement", "goal_id": self.goal_id,
                    "expected_revision": 2, "outcome": self.outcome,
                    **({"final_version_ids": [self.final_version]} if self.outcome == "completed" else
                       {"reason": "Unable to finish", "evidence": ["Execution failed"]}),
                }),
                call("send_message", "after-close-side-effect", {
                    "request_id": "after-close", "text": "SIDE_EFFECT_MUST_NOT_EXIST", "kind": "progress",
                }),
            ]}
        return {"role": "assistant", "content": "Ready"}


def create_scheduled_app(tmp_path, model):
    return create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path,
                      bootstrap=bootstrap, operator_token=OPERATOR, schedule=True)


def prepare_goals(client, model):
    base, lead, others, tokens = setup_team(client)
    lead_token = tokens[lead["member_id"]]
    initial = send(client, base, OPERATOR, request_id=key(), text="First assignment", kind="assignment")
    first = goal(client, base, lead_token, initial)["goal_id"]
    second_source = send(client, base, OPERATOR, request_id=key(), text="Next assignment", kind="assignment")
    second = goal(client, base, lead_token, second_source)["goal_id"]
    task_id, version = accept_task(client, base, tokens, first, lead["member_id"], lead, others[0], others[1])
    updated = acceptance(client, base, lead_token, first, 1, [("verified result", task_id)])
    assert updated.status_code == 201, updated.text
    model.goal_id = first
    model.next_goal_id = second
    model.final_version = version["version_id"]
    return base, lead, tokens, initial, first, second


def test_scheduled_supplement_close_preserves_receipt_and_activates_next_goal(tmp_path):
    model = ClosingModel()
    with TestClient(create_scheduled_app(tmp_path, model)) as client:
        base, lead, tokens, initial, first, second = prepare_goals(client, model)
        lead_token = tokens[lead["member_id"]]
        def idle():
            response = client.get(f"{base}/deliveries", headers=auth(lead_token))
            return response.status_code == 200 and not response.json() and not client.app.state.runtime.active.get(lead["session_id"])
        wait_until(idle)
        model.allow_close.clear()
        supplement = send(client, base, OPERATOR, request_id=key(), text="CLOSE_GOAL_TRIGGER private original detail",
                          kind="supplement", reply_to=initial["message_id"])
        try:
            assert model.requested_close.wait(8)
            queued = send(client, base, OPERATOR, request_id=key(), text="Queued under the closing goal",
                          kind="supplement", reply_to=initial["message_id"])
        finally:
            model.allow_close.set()
        assert model.saw_close_receipt.wait(8), "the model must see its original close tool receipt"
        assert model.saw_activation.wait(8), "terminal notification must drive queued goal activation"
        assert lifecycle(client, base, lead_token, first)["goal"]["status"] == "completed"
        assert lifecycle(client, base, lead_token, second)["goal"]["status"] == "active"
        def inspect():
            db = client.app.state.runtime.db
            turn = db.execute("""SELECT t.* FROM turns t JOIN scheduled_turns s ON s.turn_id=t.id
                JOIN message_deliveries d ON d.event_id=s.event_id AND d.session_id=s.session_id AND d.purpose=s.purpose
                WHERE d.message_id=?""", (supplement["message_id"],)).fetchone()
            if turn is None or turn["status"] == "running":
                return None
            receipts = [dict(r) for r in db.execute("SELECT * FROM execution_receipts WHERE turn_id=? ORDER BY step,position", (turn["id"],))]
            return dict(turn), receipts
        turn, receipts = wait_until(lambda: client.portal.call(inspect))
        assert turn["status"] == "completed"
        close = next(item for item in receipts if item["call_id"] == "close-call")
        skipped = next(item for item in receipts if item["call_id"] == "after-close-side-effect")
        assert close["state"] == skipped["state"] == "completed"
        assert json.loads(close["result"])["ok"] is True
        assert json.loads(skipped["result"])["ok"] is False
        history = json.loads(turn["history"])
        assert {m.get("tool_call_id") for m in history if m.get("role") == "tool"} >= {"close-call", "after-close-side-effect"}
        messages, tools = model.confirmation
        assert tools == []
        assert json.loads(messages[-1]["content"]) == json.loads(close["result"])
        assert messages[-1]["tool_call_id"] == "close-call"
        assert any(m.get("role") == "assistant" and any(c.get("id") == "close-call" for c in m.get("tool_calls", [])) for m in messages)
        serialized = json.dumps(messages)
        assert "after-close-side-effect" not in serialized and "private original detail" not in serialized
        snapshot = client.get(f"{base}/snapshot", headers=auth(OPERATOR)).json()
        assert all(m["text"] != "SIDE_EFFECT_MUST_NOT_EXIST" for m in snapshot["messages"])
        def queued_delivery():
            return tuple(client.app.state.runtime.db.execute("""
                SELECT d.status, s.turn_id FROM message_deliveries d
                LEFT JOIN scheduled_turns s USING (event_id, session_id, purpose)
                WHERE d.message_id = ?
            """, (queued["message_id"],)).fetchone())
        assert client.portal.call(queued_delivery) == ("cancelled", None)


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_restart_during_close_confirmation_reuses_receipt_once(tmp_path, outcome):
    class PausedConfirmation(ClosingModel):
        async def complete(self, messages, tools):
            if messages[-1].get("role") == "tool" and messages[-1].get("tool_call_id") == "close-call":
                self.saw_close_receipt.set()
                await asyncio.Event().wait()
            return await super().complete(messages, tools)

    first_model = PausedConfirmation()
    first_model.outcome = outcome
    with TestClient(create_scheduled_app(tmp_path, first_model)) as client:
        base, lead, tokens, initial, first, second = prepare_goals(client, first_model)
        lead_token = tokens[lead["member_id"]]
        def idle():
            pending = client.get(f"{base}/deliveries", headers=auth(lead_token))
            return pending.status_code == 200 and not pending.json() and not client.app.state.runtime.active.get(lead["session_id"])
        wait_until(idle)
        supplement = send(client, base, OPERATOR, request_id=key(), text="CLOSE_GOAL_TRIGGER restart sentinel",
                          kind="supplement", reply_to=initial["message_id"])
        assert first_model.saw_close_receipt.wait(8)
        def original():
            db = client.app.state.runtime.db
            row = db.execute("""SELECT s.turn_id FROM scheduled_turns s JOIN message_deliveries d
                ON d.event_id=s.event_id AND d.session_id=s.session_id AND d.purpose=s.purpose WHERE d.message_id=?""",
                (supplement["message_id"],)).fetchone()
            return row[0] if row else None
        turn_id = wait_until(lambda: client.portal.call(original))

        def original_receipt():
            row = client.app.state.runtime.db.execute(
                "SELECT result FROM execution_receipts WHERE turn_id=? AND call_id='close-call'", (turn_id,),
            ).fetchone()
            return row[0] if row else None

        receipt = wait_until(lambda: client.portal.call(original_receipt))
        assert json.loads(receipt)["result"]["status"] == outcome
        assert client.post(
            f"/v1/sessions/{lead['session_id']}/turns/{turn_id}/steer",
            headers=auth(lead_token), json={"text": "Start another tool"},
        ).status_code == 409
    recovery = ClosingModel()
    recovery.goal_id = first
    recovery.next_goal_id = second
    with TestClient(create_scheduled_app(tmp_path, recovery)) as client:
        assert recovery.saw_close_receipt.wait(8)
        messages, tools = recovery.confirmation
        assert tools == []
        assert messages[-1]["tool_call_id"] == "close-call"
        assert json.loads(messages[-1]["content"]) == json.loads(receipt)
        def recovered():
            r = client.get(f"/v1/sessions/{lead['session_id']}/turns/{turn_id}", headers=auth(lead_token))
            return r.json() if r.status_code == 200 and r.json()["status"] == "completed" else None
        wait_until(recovered)
        wait_until(lambda: lifecycle(client, base, lead_token, second)["goal"]["status"] == "active")
        def counts():
            db = client.app.state.runtime.db
            terminal = db.execute("SELECT COUNT(*) FROM goal_terminal_facts WHERE goal_id=?", (first,)).fetchone()[0]
            receipts = db.execute("SELECT COUNT(*) FROM execution_receipts WHERE turn_id=? AND call_id='close-call' AND state='completed'", (turn_id,)).fetchone()[0]
            activations = db.execute("SELECT COUNT(*) FROM collaboration_events WHERE team_id=? AND type='goal.activated' AND object_id=?", (base.rsplit('/',1)[-1], second)).fetchone()[0]
            skipped = db.execute("""SELECT result FROM execution_receipts
                WHERE turn_id=? AND call_id='after-close-side-effect'""", (turn_id,)).fetchone()
            notifications = db.execute("""SELECT COUNT(*) FROM message_deliveries
                WHERE purpose='goal.terminal' AND session_id=?""", (lead["session_id"],)).fetchone()[0]
            return terminal, receipts, activations, json.loads(skipped[0])["ok"], notifications
        assert client.portal.call(counts) == (1, 1, 1, False, 1)
        assert lifecycle(client, base, lead_token, first)["goal"]["status"] == outcome
        snapshot = client.get(f"{base}/snapshot", headers=auth(OPERATOR)).json()
        assert all(m["text"] != "SIDE_EFFECT_MUST_NOT_EXIST" for m in snapshot["messages"])


@pytest.mark.parametrize("replay", [False, True])
def test_manual_close_preserves_receipt_and_skips_remaining_tools(tmp_path, replay):
    model = ClosingModel()
    app = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path,
                     bootstrap=bootstrap, operator_token=OPERATOR, schedule=False)
    with TestClient(app) as client:
        base, lead, tokens, _, first, _ = prepare_goals(client, model)
        token = tokens[lead["member_id"]]
        if replay:
            closed = client.post(f"{base}/goals/{first}/terminate", headers=auth(token), json={
                "request_id": "close-from-supplement", "expected_revision": 2, "outcome": "completed",
                "final_version_ids": [model.final_version],
            })
            assert closed.status_code == 201, closed.text
        endpoint = f"/v1/sessions/{lead['session_id']}/turns"
        started = client.post(endpoint, headers=auth(token), json={"text": "CLOSE_GOAL_TRIGGER"})
        assert started.status_code == 202, started.text
        turn_id = started.json()["turn_id"]

        def finished():
            response = client.get(f"{endpoint}/{turn_id}", headers=auth(token))
            return response.json() if response.status_code == 200 and response.json()["status"] != "running" else None

        turn = wait_until(finished)
        assert turn["status"] == "completed", turn
        assert model.saw_close_receipt.wait(8)

        def receipts():
            return [dict(row) for row in client.app.state.runtime.db.execute(
                "SELECT * FROM execution_receipts WHERE turn_id=? ORDER BY step,position", (turn_id,))]

        rows = client.portal.call(receipts)
        close = next(row for row in rows if row["call_id"] == "close-call")
        skipped = next(row for row in rows if row["call_id"] == "after-close-side-effect")
        assert json.loads(close["result"])["ok"] is True
        if replay:
            assert json.loads(close["result"])["result"] == closed.json()
        assert json.loads(skipped["result"])["ok"] is False
        messages, tools = model.confirmation
        assert tools == []
        assert messages[-1]["tool_call_id"] == "close-call"
        assert json.loads(messages[-1]["content"]) == json.loads(close["result"])
        assert lifecycle(client, base, token, first)["goal"]["status"] == "completed"
        snapshot = client.get(f"{base}/snapshot", headers=auth(OPERATOR)).json()
        assert all(m["text"] != "SIDE_EFFECT_MUST_NOT_EXIST" for m in snapshot["messages"])
        inbox = client.get(f"{base}/deliveries", headers=auth(token)).json()
        assert [item["message_id"] for item in inbox if item["purpose"] == "goal.terminal"] == [
            json.loads(close["result"])["result"]["notification_message_id"],
        ]
