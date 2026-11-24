"""Diagnose only reader identities visible from the publishing session."""
import json

from fastapi.testclient import TestClient

from test_collaboration import app
from test_delegation import context, key
from test_workspace import MemorySandbox


def test_reader_errors_distinguish_visible_member_self_and_unknown(tmp_path):
    with TestClient(app(tmp_path)) as client:
        _, lead, others, _, _ = context(client)
        target = others[0]
        ws = client.app.state.workspace

        def publish(reader):
            return client.portal.call(ws.execute, lead["session_id"], "publish_artifact",
                                      json.dumps({"path": "report", "reader_session_ids": [reader]}))

        member = publish(target["member_id"])
        assert not member["ok"] and target["session_id"] in member["error"]
        assert "author" in publish(lead["session_id"])["error"].lower()
        assert "author" in publish(lead["member_id"])["error"].lower()
        arbitrary = key()
        assert arbitrary not in publish(arbitrary)["error"]
        assert "list_workspace_readers" in publish(arbitrary)["error"]
        assert client.portal.call(lambda: ws.db.execute("SELECT count(*) FROM artifact_versions").fetchone()[0]) == 0
        ws.sandbox = MemorySandbox()
        ws.sandbox.prepare(client.portal.call(ws.actor, lead["session_id"]).workspace_id)
        assert client.portal.call(ws.execute, lead["session_id"], "create_file",
                                  json.dumps({"path": "report", "content": "checked"}))["ok"]
        assert publish(target["session_id"])["ok"]
