"""Run a guided handoff through the real delegation tools, receipts, and reviewer."""
import json
import threading

from fastapi.testclient import TestClient

from test_collaboration import OPERATOR, auth
from test_delegation import action, confirm_all, context, create, key, ok, terms
from test_scheduler import Sandbox, application, current_delivery, tool, wait_until


def test_candidate_request_preserves_current_batch_and_uses_published_versions(tmp_path):
    class GateSandbox(Sandbox):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def run(self, workspace_id, command, **kwargs):
            if command == "first":
                self.entered.set()
                assert self.release.wait(8)
            return super().run(workspace_id, command, **kwargs)

        def read_bytes(self, workspace_id, path):
            return path.encode()

    class Model:
        async def complete(self, messages, tools):
            payload = current_delivery(messages)
            delivery = payload["delivery"]
            last = messages[-1]
            if delivery["purpose"] == "task.assigned" and delivery["session_id"] == owner["session_id"]:
                if last["role"] == "user":
                    response = tool("run_command", "first", {"command": "first"})
                    response["tool_calls"] += tool("run_command", "second", {"command": "second"})["tool_calls"]
                    return response
            if delivery["purpose"] == "task.delivery_requested":
                request = payload["candidate_delivery"]
                assert request["task_id"] == task and request["revision"] == 1
                if last["role"] == "user":
                    assert "run_command" not in {item["function"]["name"] for item in tools}
                    assert [command for _, command in sandbox.commands] == ["first", "second"]
                    response = tool("run_command", "not-allowed", {"command": "unexpected"})
                    for path in ("draft.md", "check.txt"):
                        response["tool_calls"] += tool("publish_artifact", path, {
                            "path": path, "reader_session_ids": [reviewer["session_id"]],
                        })["tool_calls"]
                    return response
                denied, *publications = [json.loads(item["content"]) for item in messages[-3:]]
                assert not denied["ok"]
                args = {"task_id": task, "request_id": key(), "expected_revision": 1,
                        "version_ids": [result["result"]["version_id"] for result in publications],
                        "evidence": ["Read the representative check output"]}
                response = tool("submit_result", "wrong-task", {**args, "task_id": "1" * 32})
                response["tool_calls"] += tool("submit_result", "submit", args)["tool_calls"]
                return response
            if payload.get("review_request") and last["role"] == "user":
                submission = payload["review_request"]["submission"]
                return tool("review_result", "decision", {
                    "request_id": key(), "task_id": task, "expected_revision": 1,
                    "submission_id": submission["id"], "decision": "approved",
                    "findings": [{"criterion": 1, "met": True, "evidence": "Read both published files"}],
                })
            return {"role": "assistant", "content": "Completed the current turn."}

    sandbox = GateSandbox()
    with TestClient(application(tmp_path, Model(), sandbox)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        token = tokens[lead["member_id"]]
        proposal = create(client, base, token, goal, terms(owner["member_id"], reviewer["member_id"]))
        confirm_all(client, proposal["proposal_id"], tokens)
        task = ok(action(client, proposal["proposal_id"], token, "assign"))["task_id"]
        endpoint = f"{base}/tasks/{task}/delivery-requests"
        try:
            assert sandbox.entered.wait(8)
            requested = ok(client.post(endpoint, headers=auth(OPERATOR), json={
                "request_id": key(), "expected_revision": 1, "paths": ["draft.md", "check.txt"],
            }))
            assert requested["status"] == "queued"
        finally:
            sandbox.release.set()

        def approved():
            candidates = ok(client.get(f"{base}/delivery-requests", headers=auth(OPERATOR)))["tasks"]
            request = next(item["request"] for item in candidates if item["id"] == task)
            return request if request["status"] == "approved" else None

        receipt = wait_until(approved)
        assert receipt["id"] == requested["id"]
        assert receipt["submission_id"] and receipt["review_id"]
        assert [command for _, command in sandbox.commands] == ["first", "second"]
