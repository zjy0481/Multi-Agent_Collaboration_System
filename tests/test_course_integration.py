"""Opt-in T013 real course-model and Docker causal tool-call check."""
import json
import os
import secrets
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.model import load_model
from collaboration_runtime.sandbox import DockerSandbox
from test_collaboration import OPERATOR, auth

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_COURSE_TESTS") != "1", reason="explicit live course opt-in required")

ROSTER = """# Task workers

| Agent ID | Role |
|---|---|
| `worker` | Data Worker |

## Data Worker (worker)

```text
You read files and report exact text from files when requested. You have no managerial, team coordination, or delegation responsibilities.
```
"""


def test_live_course_bootstrap_and_real_docker_tool_causality(tmp_path):
    model = load_model()
    assert model.identity["mode"] == "course"
    identity = model.identity
    sandbox = DockerSandbox(tmp_path / "sandbox", image="collaboration-sandbox:local")
    workspace_ids = []
    app = create_app(model=model, sandbox=sandbox, state_dir=tmp_path / "state", operator_token=OPERATOR, schedule=False)
    try:
        with TestClient(app) as client:
            response = client.post("/v1/teams", headers=auth(OPERATOR), json={"request_id": "live-team", "roster": ROSTER})
            assert response.status_code == 201, "Live bootstrap did not create the team"
            team = response.json()
            workspace_ids = [member["workspace_id"] for member in team["members"]]
            assert team["generated_lead"] is True
            worker = next(member for member in team["members"] if member["role_id"] == "worker")
            value = "T013_" + secrets.token_hex(16)
            sandbox.write_bytes(worker["workspace_id"], "proof.txt", value.encode("ascii"), mode="create")
            base = f"/v1/teams/{team['team_id']}"
            credential = client.post(f"{base}/members/{worker['member_id']}/credential", headers=auth(OPERATOR))
            assert credential.status_code == 200
            token = credential.json()["token"]
            prompt = ("Read the exact contents of proof.txt using read_file. Then use send_message to post "
                      "the exact file contents to the group as kind progress with no mentions. "
                      "After posting, give a short final reply. Do not guess the file contents.")
            start = client.post(f"/v1/sessions/{worker['session_id']}/turns", headers=auth(token), json={"text": prompt})
            assert start.status_code == 202
            turn_id = start.json()["turn_id"]
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                result = client.get(f"/v1/sessions/{worker['session_id']}/turns/{turn_id}", headers=auth(token)).json()
                if result["status"] != "running":
                    break
                time.sleep(0.2)
            assert result["status"] == "completed", "Live model turn did not complete"
            def inspect():
                db = client.app.state.runtime.db
                return [dict(row) for row in db.execute(
                    "SELECT step, position, call_id, name, arguments, result, state FROM execution_receipts WHERE turn_id=? ORDER BY step,position", (turn_id,))]
            receipts = client.portal.call(inspect)
            read = next((r for r in receipts if r["name"] == "read_file" and r["state"] == "completed"), None)
            post = next((r for r in receipts if r["name"] == "send_message" and r["state"] == "completed"), None)
            assert read is not None and post is not None, "Required tools were not both completed"
            assert read["call_id"] and post["call_id"] and read["call_id"] != post["call_id"]
            assert read["step"] < post["step"], "The model must consume the read result in a later decision"
            read_result, post_result = json.loads(read["result"]), json.loads(post["result"])
            assert read_result["ok"] and post_result["ok"]
            assert read_result["result"]["content"] == value
            post_args = json.loads(post["arguments"])
            if isinstance(post_args, str):
                post_args = json.loads(post_args)
            assert value in post_args["text"] and post_args.get("kind") == "progress" and not post_args.get("mentions")
            snapshot = client.get(f"{base}/snapshot", headers=auth(OPERATOR))
            assert snapshot.status_code == 200
            assert any(message["id"] == post_result["result"]["message_id"] and message["sender_id"] == worker["member_id"] and message["kind"] == "progress" and value in message["text"] for message in snapshot.json()["messages"])
            print("course integration verified:", identity["mode"], identity["model"], urlsplit(identity["endpoint"]).hostname,
                  "generated Lead; Docker file read; model posted read value; distinct tool calls in later steps")
    finally:
        if workspace_ids:
            sandbox.cleanup(*workspace_ids)
