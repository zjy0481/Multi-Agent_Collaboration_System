"""T006 scheduler acceptance using HTTP, SQLite, and deterministic local doubles."""
import asyncio
import json
import threading
import time

from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from test_collaboration import OPERATOR, auth, bootstrap, send, setup_team, snapshot
from test_delegation import action, context, create, key, ok, terms


class Sandbox:
    def __init__(self):
        self.commands = []

    def prepare(self, workspace_id):
        pass

    def run(self, workspace_id, command, **kwargs):
        self.commands.append((workspace_id, command))
        return {"exit_code": 0, "stdout": "value-from-first" if command == "first" else command, "stderr": "", "timed_out": False}


def application(path, model, sandbox):
    return create_app(model=model, sandbox=sandbox, state_dir=path, bootstrap=bootstrap,
                      operator_token=OPERATOR, schedule=True)


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for scheduler")


def turns(client, session, token):
    response = client.get(f"/v1/sessions/{session}/turns", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def tool(name, call_id, args):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}
    }]}


def current_delivery(messages):
    for message in reversed(messages):
        if message.get("role") == "user":
            try:
                payload = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and "delivery" in payload:
                return payload
    raise AssertionError("Missing structured delivery input")


def test_proposal_confirmation_yields_after_receipts_and_task_runs(tmp_path):
    class Model:
        def __init__(self):
            self.confirmation_calls = 0

        async def complete(self, messages, tools):
            delivery = current_delivery(messages)
            proposal = delivery.get("proposal", {})
            if delivery.get("proposal_action") == "assign_task" and messages[-1]["role"] == "user":
                return tool("assign_task", "assign", {"request_id": key(), "proposal_id": proposal["id"],
                                                     "expected_version": proposal["version"]})
            if delivery["delivery"]["purpose"] == "delegation.proposal" and not delivery.get("proposal_action"):
                self.confirmation_calls += 1
                wrong_version = tool("confirm_proposal", "stale", {"request_id": key(), "proposal_id": proposal["id"],
                                                                   "expected_version": proposal["version"] + 1})
                if messages[-1]["role"] == "user":
                    return wrong_version
                if messages[-1].get("tool_call_id") == "stale":
                    assert not json.loads(messages[-1]["content"])["ok"]
                    decision = tool("confirm_proposal", "confirm", {"request_id": key(), "proposal_id": proposal["id"],
                                                                     "expected_version": proposal["version"]})
                    decision["tool_calls"] += tool("list_participants", "roster", {})["tool_calls"]
                    return decision
            return {"role": "assistant", "content": "Finished"}

    model = Model()
    with TestClient(application(tmp_path, model, Sandbox())) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        lead_id, owner_id, reviewer_id = (member["member_id"] for member in (lead, owner, reviewer))
        record = create(client, base, tokens[lead_id], goal, terms(owner_id, reviewer_id))
        proposal = record["proposal_id"]
        # Confirmations from either arrival order can activate the proposer notification.
        view = ok(client.get(f"/v1/proposals/{proposal}", headers=auth(tokens[lead_id])))
        if lead_id not in view["confirmation_ids"]:
            ok(action(client, proposal, tokens[lead_id], "confirm"))

        def completed_turns():
            db = client.app.state.runtime.db
            return [dict(row) for row in db.execute("""
                SELECT t.status,t.history,s.purpose
                FROM scheduled_turns s JOIN turns t ON t.id=s.turn_id
                WHERE s.session_id=? AND t.status='completed'
                ORDER BY t.rowid
            """, (owner["session_id"],))]

        def task_finished():
            rows = client.portal.call(completed_turns)
            return rows if any(row["purpose"] == "task.assigned" for row in rows) else None

        received = wait_until(task_finished)
        proposal_turn, = [row for row in received if row["purpose"] == "delegation.proposal"]
        history = json.loads(proposal_turn["history"])
        tool_messages = [row for row in history if row["role"] == "tool"]
        assert [row["tool_call_id"] for row in tool_messages] == ["stale", "confirm", "roster"]
        assert all(json.loads(row["content"])["ok"] for row in tool_messages[1:])
        assert "Proposal confirmed" in history[-1]["content"]
        assert model.confirmation_calls == 4  # Two parties each correct the stale attempt.


def test_serial_per_session_parallel_across_sessions_and_tool_receipts(tmp_path):
    class Model:
        def __init__(self):
            self.first_started = threading.Event()
            self.release = threading.Event()
            self.seen = []
            self.errors = []

        async def complete(self, messages, tools):
            delivery = current_delivery(messages)
            text = delivery["message"]["text"]
            self.seen.append(text)
            if text == "hold first":
                self.first_started.set()
                if not await asyncio.to_thread(self.release.wait, 8):
                    raise AssertionError("test did not release first turn")
                return {"role": "assistant", "content": "finished first"}
            if text == "tool sequence":
                seen = [m for m in messages if m.get("role") == "tool"]
                if not seen:
                    return tool("run_command", "original-a", {"command": "first"})
                if len(seen) == 1:
                    assert seen[0]["tool_call_id"] == "original-a"
                    assert "value-from-first" in seen[0]["content"]
                    return tool("run_command", "original-b", {"command": "value-from-first"})
                assert [m["tool_call_id"] for m in seen[-2:]] == ["original-a", "original-b"]
                return {"role": "assistant", "content": "both tools done"}
            return {"role": "assistant", "content": "independent complete"}

    model, sandbox = Model(), Sandbox()
    with TestClient(application(tmp_path, model, sandbox)) as client:
        base, lead, others, tokens = setup_team(client)
        other = others[0]
        try:
            send(client, base, OPERATOR, request_id="first", text="hold first", mentions=[lead["member_id"]])
            assert model.first_started.wait(5)
            send(client, base, OPERATOR, request_id="second", text="tool sequence", mentions=[lead["member_id"]])
            send(client, base, OPERATOR, request_id="parallel", text="other session", mentions=[other["member_id"]])
            wait_until(lambda: any(t["status"] == "completed" for t in turns(client, other["session_id"], tokens[other["member_id"]])))
            assert "tool sequence" not in model.seen
        finally:
            model.release.set()
        all_turns = wait_until(lambda: (lambda ts: ts if len([t for t in ts if t["status"] == "completed"]) >= 2 else None)(
            turns(client, lead["session_id"], tokens[lead["member_id"]])))
        assert len([t for t in all_turns if t["status"] == "completed"]) == 2
        assert [cmd for _, cmd in sandbox.commands] == ["first", "value-from-first"]
        receipts = client.portal.call(lambda: client.app.state.runtime.db.execute("SELECT * FROM execution_receipts").fetchall())
        assert {"original-a", "original-b"} <= {str(value) for row in receipts for value in row}


def test_clarification_answer_automatically_resumes(tmp_path):
    class Model:
        def __init__(self):
            self.resumed = threading.Event()
            self.delivery = None

        async def complete(self, messages, tools):
            payload = current_delivery(messages)
            if payload["delivery"]["purpose"] == "clarification.resume":
                self.delivery = payload
                self.resumed.set()
                return {"role": "assistant", "content": "Applied the answer"}
            return tool("ask_human", "ask-id", {"request_id": "question", "source_message_id": payload["message"]["id"], "text": "Which rule?"})

    model = Model()
    with TestClient(application(tmp_path, model, Sandbox())) as client:
        base, lead, _, tokens = setup_team(client)
        work = send(client, base, OPERATOR, request_id="work", text="Do work")
        question = wait_until(lambda: next(iter(snapshot(client, base, OPERATOR)["questions"]), None))
        answer = send(client, base, OPERATOR, request_id="answer", text="Rule A", reply_to=question["question_id"])
        assert model.resumed.wait(8)
        assert model.delivery["source_message"]["id"] == work["message_id"]
        assert [item["id"] for item in model.delivery["answers"]] == [answer["message_id"]]
        wait_until(lambda: any(t["status"] == "completed" for t in turns(client, lead["session_id"], tokens[lead["member_id"]])))


def test_restart_resumes_receipt_without_repeating_effect(tmp_path):
    class PausedModel:
        def __init__(self):
            self.receipt_seen = threading.Event()

        async def complete(self, messages, tools):
            current_delivery(messages)
            results = [m for m in messages if m.get("role") == "tool"]
            if not results:
                return tool("run_command", "persisted-call", {"command": "first"})
            assert results[-1]["tool_call_id"] == "persisted-call"
            self.receipt_seen.set()
            await asyncio.Event().wait()

    class ResumeModel:
        def __init__(self):
            self.continued = threading.Event()

        async def complete(self, messages, tools):
            results = [m for m in messages if m.get("role") == "tool"]
            assert any(m["tool_call_id"] == "persisted-call" and "value-from-first" in m["content"] for m in results)
            self.continued.set()
            return {"role": "assistant", "content": "continued after restart"}

    sandbox, first_model = Sandbox(), PausedModel()
    with TestClient(application(tmp_path, first_model, sandbox)) as client:
        base, lead, _, tokens = setup_team(client)
        send(client, base, OPERATOR, request_id="restart", text="restart work")
        assert first_model.receipt_seen.wait(8)
        original = turns(client, lead["session_id"], tokens[lead["member_id"]])
        assert len(original) == 1
        turn_id = original[0]["turn_id"]
        assert [command for _, command in sandbox.commands] == ["first"]
    second_model = ResumeModel()
    with TestClient(application(tmp_path, second_model, sandbox)) as client:
        assert second_model.continued.wait(8)
        def completed():
            items = turns(client, lead["session_id"], tokens[lead["member_id"]])
            return next((t for t in items if t["turn_id"] == turn_id and t["status"] == "completed"), None)
        wait_until(completed)
        assert [command for _, command in sandbox.commands] == ["first"]


def test_unknown_tool_outcome_is_not_repeated_after_restart(tmp_path):
    class BlockingSandbox(Sandbox):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def run(self, workspace_id, command, **kwargs):
            self.commands.append((workspace_id, command))
            self.started.set()
            if not self.release.wait(8):
                raise TimeoutError("test did not release tool")
            return {"exit_code": 0, "stdout": "late result", "stderr": "", "timed_out": False}

    class Model:
        async def complete(self, messages, tools):
            return tool("run_command", "unknown-action", {"command": "perform-side-effect"})

    sandbox = BlockingSandbox()
    timer = None
    try:
        with TestClient(application(tmp_path, Model(), sandbox)) as client:
            base, lead, _, tokens = setup_team(client)
            send(client, base, OPERATOR, request_id="unknown-work", text="Run action")
            assert sandbox.started.wait(8)
            turn_id = wait_until(lambda: next(iter(turns(client, lead["session_id"], tokens[lead["member_id"]])), None))["turn_id"]
            timer = threading.Timer(0.5, sandbox.release.set)
            timer.start()
        with TestClient(application(tmp_path, Model(), sandbox)) as client:
            def reconciled():
                return next((t for t in turns(client, lead["session_id"], tokens[lead["member_id"]])
                             if t["turn_id"] == turn_id and t["status"] == "needs_reconciliation"), None)
            wait_until(reconciled)
            assert [command for _, command in sandbox.commands] == ["perform-side-effect"]
    finally:
        sandbox.release.set()
        if timer is not None:
            timer.join()
