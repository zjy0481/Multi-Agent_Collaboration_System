"""Opt-in real course model test for a bounded assistant fork."""
import json
import os
import secrets
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.model import load_model
from test_collaboration import OPERATOR, Sandbox, auth, bootstrap
from test_delegation import context
from test_forks import start_turn, terminal, wait_until


pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_FORK_TESTS") != "1", reason="explicit live fork opt-in required")


def test_live_fork_reads_authorized_material_and_reports_to_parent(tmp_path):
    course = load_model()
    assert course.identity["mode"] == "course"
    early = "EARLY_" + secrets.token_hex(8)
    later = "LATER_PRIVATE_" + secrets.token_hex(8)
    value = "ARTIFACT_" + secrets.token_hex(8)
    purpose_marker = "LIVE_FORK_T007"

    class RoutedModel:
        identity = course.identity
        seen = []

        async def complete(self, messages, tools):
            if purpose_marker in (messages[0].get("content") or ""):
                self.seen.append(json.loads(json.dumps(messages)))
                return await course.complete(messages, tools)
            return {"role": "assistant", "content": "Fixture parent turn completed."}

        async def aclose(self):
            await course.aclose()

    model = RoutedModel()
    app = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                     operator_token=OPERATOR, schedule=True)
    with TestClient(app) as client:
        base, lead, _, tokens, goal = context(client)
        token = tokens[lead["member_id"]]
        wait_until(lambda: not app.state.runtime.active.get(lead["session_id"]))
        anchors = []
        for text in (f"The earlier fact is {early}.", "Second historical turn: retain the earlier fact.", later):
            turn = start_turn(client, lead, token, text)
            assert terminal(client, lead["session_id"], token, turn)["status"] == "completed"
            anchors.append(turn)
        team_id = base.rsplit("/", 1)[-1]
        store = app.state.workspace.artifacts
        artifact = client.portal.call(lambda: store.publish(team_id=team_id, author_session_id=lead["session_id"],
            source_path="authorized.txt", content=value.encode(), reader_session_ids=[]))
        purpose = (f"{purpose_marker}: First call read_artifact using the explicitly authorized version ID. "
                   "Then call send_fork_message to your parent, with assistant_id equal to your own session ID and a unique "
                   "request_id. In that message include the exact earlier fact from inherited history and the exact "
                   "artifact content returned by read_artifact. Do not guess either value. Then finish.")
        response = client.post(f"/v1/sessions/{lead['session_id']}/forks", headers=auth(token), json={
            "request_id": uuid4().hex, "from_turn_id": anchors[1], "goal_id": goal, "scope_id": "live-t007",
            "purpose": purpose, "history_turn_ids": anchors[:2], "version_ids": [artifact["version_id"]],
            "allowed_tools": ["read_artifact", "send_fork_message"], "contact_session_ids": [],
        })
        assert response.is_success, response.text
        helper = response.json()["session_id"]

        def finished():
            data = client.get(f"/v1/sessions/{helper}/turns", headers=auth(token))
            assert data.status_code == 200
            return next((item for item in data.json() if item["status"] != "running"), None)

        turn = wait_until(finished, timeout=120)
        assert turn["status"] == "completed", turn
        assert model.seen and early in json.dumps(model.seen[0])
        assert all(later not in json.dumps(messages) for messages in model.seen)
        rows = client.portal.call(lambda: [dict(row) for row in app.state.runtime.db.execute(
            "SELECT step, call_id, name, arguments, result, state FROM execution_receipts WHERE turn_id=? ORDER BY step,position",
            (turn["turn_id"],)).fetchall()])
        read = next(row for row in rows if row["name"] == "read_artifact" and row["state"] == "completed")
        post = next(row for row in rows if row["name"] == "send_fork_message" and row["state"] == "completed")
        read_result, post_result = json.loads(read["result"]), json.loads(post["result"])
        post_args = json.loads(json.loads(post["arguments"]))
        assert read["step"] < post["step"] and read["call_id"] != post["call_id"]
        assert read_result["ok"] and value in json.dumps(read_result)
        assert post_result["ok"] and early in post_args["text"] and value in post_args["text"]
        assert any(any(m.get("tool_call_id") == read["call_id"] and value in m.get("content", "")
                       for m in messages) for messages in model.seen[1:])
        replies = client.get(f"/v1/forks/{helper}/messages", headers=auth(token))
        assert replies.status_code == 200, replies.text
        assert any(m["message_id"] == post_result["result"]["message_id"] and early in m["text"] and value in m["text"]
                   for m in replies.json())
        assert later not in json.dumps(replies.json())
        print("live fork verified:", course.identity["mode"], course.identity["model"], course.identity["endpoint"],
              "selected early history; read_artifact -> send_fork_message; parent received; later input absent")
