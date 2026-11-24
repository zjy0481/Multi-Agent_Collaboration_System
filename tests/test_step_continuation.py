"""Bounded continuation of a journaled formal delivery."""
import json

from fastapi.testclient import TestClient

from test_collaboration import app, auth
from test_delegation import context
from test_delivery_priority import run_one, scheduler
from test_scheduler import tool
from test_workspace import MemorySandbox


def test_scoped_budget_continues_once_without_replaying_command(tmp_path):
    class Calls:
        count = 0

        async def complete(self, messages, tools):
            self.count += 1
            if self.count == 1:
                return tool("publish_artifact", "publish", {"path": "result.txt", "reader_session_ids": []})
            return tool("list_artifacts", "list", {})

    with TestClient(app(tmp_path)) as client:
        _, lead, _, tokens, goal = context(client)
        workspace = client.app.state.workspace
        workspace.sandbox = MemorySandbox()
        workspace.sandbox.prepare(client.portal.call(workspace.actor, lead["session_id"]).workspace_id)
        created = client.portal.call(workspace.execute, lead["session_id"], "create_file",
                                     json.dumps({"path": "result.txt", "content": "finished"}))
        assert created["ok"]
        queue = scheduler(client)
        queue.runtime.max_steps = 1
        queue.runtime.model = Calls()
        tid = client.portal.call(run_one, queue)
        first = client.get(f"/v1/sessions/{lead['session_id']}/turns/{tid}", headers=auth(tokens[lead["member_id"]])).json()
        assert first["status"] == "recoverable"
        assert first["budget"]["goal_id"] == goal and first["budget"]["step"] == 1
        assert client.portal.call(run_one, queue) == tid
        last = client.portal.call(queue.runtime.get_turn, lead["session_id"], tid)
        assert last["status"] == "failed" and last["budget"]["automatic_extensions"] == 1
        assert client.portal.call(lambda: queue.db.execute(
            "SELECT count(*) FROM execution_receipts WHERE turn_id=? AND state='completed'", (tid,)).fetchone()[0]) == 2
        assert client.portal.call(lambda: queue.db.execute("SELECT count(*) FROM artifact_versions").fetchone()[0]) == 1
