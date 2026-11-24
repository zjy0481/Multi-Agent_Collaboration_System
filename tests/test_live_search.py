"""Opt-in test for the operator-approved live search endpoint."""

import json
import os
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.model import load_model
from test_collaboration import OPERATOR, Sandbox, app, auth, bootstrap, send, setup_team, snapshot


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SEARCH_TESTS") != "1",
    reason="Requires explicit opt-in and an operator-approved search endpoint",
)
def test_live_search_records_reopen_with_the_same_sources(tmp_path):
    with TestClient(app(tmp_path)) as client:
        _, lead, other_members, _ = setup_team(client)
        session = lead["session_id"]
        workspace = client.app.state.workspace
        assert workspace.search is not None, "Configure EXA_API_KEY or COURSE_SEARCH_URL before live retrieval"
        outcome = client.portal.call(
            workspace.execute, session, "search_web",
            json.dumps({"query": "site:sqlite.org SQLite transaction documentation", "limit": 3}),
        )
        assert outcome["ok"], outcome["error"] if not outcome["ok"] else ""
        record = outcome["result"]
        assert record["results"], "The approved provider returned no usable sources"
        if os.environ.get("EXA_API_KEY", "").strip():
            assert record["provider"] == "exa", record["fallback_reason"]
        assert all(result["title"] and urlsplit(result["url"]).hostname for result in record["results"])

    with TestClient(app(tmp_path)) as client:
        workspace = client.app.state.workspace
        args = json.dumps({"search_id": record["search_id"]})
        recovered = client.portal.call(workspace.execute, session, "get_search_result", args)
        assert recovered == {"ok": True, "result": record}
        denied = client.portal.call(workspace.execute, other_members[0]["session_id"], "get_search_result", args)
        assert not denied["ok"]
        print("live search provider:", record["provider"], "sources:",
              sorted({urlsplit(result["url"]).hostname for result in record["results"]}))


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_COURSE_SEARCH_TESTS") != "1",
    reason="Requires explicit opt-in for real course model and approved search",
)
def test_live_course_model_searches_retrieves_and_reports(tmp_path):
    model = load_model()
    identity = model.identity
    assert identity["mode"] == "course"
    application = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path,
                             bootstrap=bootstrap, operator_token=OPERATOR)
    with TestClient(application) as client:
        assert client.app.state.workspace.search is not None, "Configure EXA_API_KEY or COURSE_SEARCH_URL"
        base, lead, _, tokens = setup_team(client)
        prompt = (
            "Find the official SQLite transaction documentation. First call search_web with query "
            "'site:sqlite.org SQLite transaction documentation'. Then call get_search_result using the exact "
            "search_id returned by that call. Then call send_message with kind progress, no mentions, "
            "and include that search_id and one complete source URL returned by get_search_result. "
            "Finally give a short final reply. Do not invent a search ID or URL."
        )
        send(client, base, OPERATOR, request_id="course-search", text=prompt)
        history_url = f"/v1/sessions/{lead['session_id']}/turns"
        headers = auth(tokens[lead["member_id"]])
        deadline = time.monotonic() + 180
        done = None
        while time.monotonic() < deadline:
            response = client.get(history_url, headers=headers)
            assert response.status_code == 200
            done = next((turn for turn in response.json() if turn["status"] != "running"), None)
            if done:
                break
            time.sleep(0.2)
        assert done is not None and done["status"] == "completed", "Course search turn did not complete"
        def inspect():
            db = client.app.state.runtime.db
            return [dict(row) for row in db.execute("""
                SELECT step, call_id, name, arguments, result FROM execution_receipts
                WHERE turn_id = ? AND state = 'completed' ORDER BY step, position
            """, (done["turn_id"],))]

        receipts = client.portal.call(inspect)
        tool_names = ("search_web", "get_search_result", "send_message")
        found, reopened, posted = (next((row for row in receipts if row["name"] == name), None) for name in tool_names)
        assert found and reopened and posted, "Expected all three tools to complete"
        assert len({found["call_id"], reopened["call_id"], posted["call_id"]}) == 3
        assert all(row["call_id"] for row in (found, reopened, posted))
        assert found["step"] < reopened["step"] < posted["step"]
        search_result, reopen_result, post_result = (json.loads(row["result"]) for row in (found, reopened, posted))
        assert all(result["ok"] for result in (search_result, reopen_result, post_result))
        record = search_result["result"]
        assert record["results"], "Approved source returned no usable results"
        if os.environ.get("EXA_API_KEY", "").strip():
            assert record["provider"] == "exa", record["fallback_reason"]
        assert reopen_result["result"] == record
        reopen_args = json.loads(json.loads(reopened["arguments"]))
        assert reopen_args["search_id"] == record["search_id"]
        post_args = json.loads(json.loads(posted["arguments"]))
        source_urls = [item["url"] for item in record["results"]
                       if (urlsplit(item["url"]).hostname or "").split(".")[-2:] == ["sqlite", "org"]]
        assert record["search_id"] in post_args["text"]
        assert any(url in post_args["text"] for url in source_urls), "The progress must cite an actual SQLite source"
        assert post_args.get("kind") == "progress" and not post_args.get("mentions")
        messages = snapshot(client, base, OPERATOR)["messages"]
        message = next((message for message in messages if message["id"] == post_result["result"]["message_id"]), None)
        assert message and message["sender_id"] == lead["member_id"] and message["text"] == post_args["text"].strip()
        print("live course search verified:", identity["mode"], identity["model"], urlsplit(identity["endpoint"]).hostname,
              "search_web -> get_search_result -> group progress; distinct call IDs; source record and message match")
