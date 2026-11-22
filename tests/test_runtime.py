"""HTTP-level checks for the asynchronous collaboration runtime."""
import copy
import json
import threading
import time

from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from test_collaboration import OPERATOR, Sandbox, auth, bootstrap, send, setup_team, snapshot


class ControlledModel:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append(copy.deepcopy(messages))
        if len(self.calls) == 1:
            return {
                "role": "assistant",
                "content": None,
                "reasoning_content": "PRIVATE_REASONING_SENTINEL",
                "tool_calls": [
                    {"id": "call-a", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "first"})}},
                ],
            }
        if len(self.calls) == 2:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-b", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "second"})}},
                ],
            }
        return {"role": "assistant", "content": "Applied the revised requirement."}


class ControlledSandbox:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.commands = []

    def run(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        if len(self.commands) == 1:
            self.started.set()
            if not self.release.wait(10):
                raise TimeoutError("test did not release sandbox")
        return {"exit_code": 0, "stdout": f"result-{len(self.commands)}", "stderr": ""}

    def publish(self, *args, **kwargs):
        return {"version_id": "artifact-v1"}

    def list_artifacts(self, *args, **kwargs):
        return []


def wait_for_terminal(client, role, turn_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{role}/turns/{turn_id}")
        assert response.status_code == 200
        data = response.json()
        if data["status"] != "running":
            return data
        time.sleep(0.02)
    raise AssertionError("turn did not reach a terminal state")


def test_steer_during_tool_execution_and_isolated_event_stream(tmp_path):
    model = ControlledModel()
    sandbox = ControlledSandbox()
    with TestClient(create_app(model=model, sandbox=sandbox, state_dir=tmp_path, operator_token='test-operator-token')) as client:
        client.headers.update({"Authorization": "Bearer test-operator-token"})
        try:
            started = client.post("/v1/sessions/developer/turns", json={"text": "Implement the task"})
            assert started.status_code == 202
            assert started.json()["status"] == "running"
            turn_id = started.json()["turn_id"]
            assert sandbox.started.wait(5), "first tool did not start"

            duplicate = client.post("/v1/sessions/developer/turns", json={"text": "another"})
            assert duplicate.status_code == 409
            queued = client.post(f"/v1/sessions/developer/turns/{turn_id}/steer", json={"text": "Revised requirement: include cancellations"})
            assert queued.status_code == 202
            assert queued.json() == {"turn_id": turn_id, "status": "queued"}
            assert client.get(f"/v1/sessions/tester/turns/{turn_id}").status_code in (403, 404)
        finally:
            sandbox.release.set()

        result = wait_for_terminal(client, "developer", turn_id)
        assert result["status"] == "completed", result
        assert len(sandbox.commands) == 2
        assert len(model.calls) == 3
        next_messages = model.calls[1]
        tool_messages = [m for m in next_messages if m.get("role") == "tool"]
        assert [m.get("tool_call_id") for m in tool_messages] == ["call-a"]
        assert json.loads(tool_messages[0]["content"])["result"]["stdout"] == "result-1"
        assert "Revised requirement: include cancellations" in json.dumps(next_messages, ensure_ascii=False)
        assert any(m.get("reasoning_content") == "PRIVATE_REASONING_SENTINEL" for m in next_messages)
        assert [m["tool_call_id"] for m in model.calls[2] if m["role"] == "tool"] == ["call-a", "call-b"]

        events = client.get(f"/v1/sessions/developer/turns/{turn_id}/events")
        assert events.status_code == 200
        assert "text/event-stream" in events.headers["content-type"]
        body = events.text
        for event in ("turn.started", "turn.input_queued", "turn.input_applied", "tool.started", "tool.completed", "turn.completed"):
            assert event in body
        assert body.count("event: model.completed") == 3
        assert "PRIVATE_REASONING_SENTINEL" not in body
        assert client.get(f"/v1/sessions/tester/turns/{turn_id}/events").status_code in (403, 404)


def test_model_question_yields_slot_and_preserves_tool_call_history(tmp_path):
    class AskingModel:
        calls = 0
        work_id = None

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                assert "ask_human" in {tool["function"]["name"] for tool in tools}
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "ask", "type": "function", "function": {"name": "ask_human", "arguments": json.dumps({
                        "request_id": "question", "source_message_id": self.work_id, "text": "Which rule should I use?",
                    })}},
                    {"id": "skip", "type": "function", "function": {"name": "send_message", "arguments": json.dumps({
                        "request_id": "unexpected", "text": "The work is done.",
                    })}},
                ]}
            results = [m for m in messages if m["role"] == "tool"]
            assert [m["tool_call_id"] for m in results] == ["ask", "skip"]
            assert json.loads(results[0]["content"])["ok"]
            assert not json.loads(results[1]["content"])["ok"]
            assert messages[-1]["content"] == "Use rule A"
            return {"role": "assistant", "content": "I will use rule A."}

    model = AskingModel()
    application = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path,
                             bootstrap=bootstrap, operator_token=OPERATOR)
    with TestClient(application) as client:
        base, lead, _, tokens = setup_team(client)
        client.headers.update(auth(tokens[lead["member_id"]]))
        work = send(client, base, OPERATOR, request_id="work", text="Do the work", kind="assignment")
        model.work_id = work["message_id"]
        session = lead["session_id"]
        endpoint = f"/v1/sessions/{session}/turns"
        started = client.post(endpoint, json={"text": "Review the assignment"})
        assert started.status_code == 202
        turn_id = started.json()["turn_id"]
        assert wait_for_terminal(client, session, turn_id)["status"] == "waiting_human"
        assert model.calls == 1

        state = snapshot(client, base, OPERATOR)
        assert not state["goals"]
        assert len(state["messages"]) == 2
        question_id = state["questions"][0]["question_id"]
        answer = send(client, base, OPERATOR, request_id="answer", text="Use rule A", reply_to=question_id)
        ready = client.get(f"{base}/deliveries").json()
        assert [item["message_id"] for item in ready] == [answer["message_id"]]

        # A controlled claimant starts the next turn with the delivered input.
        resumed = client.post(endpoint, json={"text": "Use rule A"})
        assert resumed.status_code == 202
        result = wait_for_terminal(client, session, resumed.json()["turn_id"])
        assert result["status"] == "completed", result
