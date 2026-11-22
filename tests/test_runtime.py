"""HTTP-level checks for the asynchronous collaboration runtime."""
import copy
import json
import threading
import time

from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app


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
        if data["status"] in ("completed", "failed"):
            return data
        time.sleep(0.02)
    raise AssertionError("turn did not reach a terminal state")


def test_steer_during_tool_execution_and_isolated_event_stream(tmp_path):
    model = ControlledModel()
    sandbox = ControlledSandbox()
    with TestClient(create_app(model=model, sandbox=sandbox, state_dir=tmp_path)) as client:
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
