"""Opt-in test for the operator-approved live search endpoint."""

import json
import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from test_collaboration import app, setup_team


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SEARCH_TESTS") != "1",
    reason="Requires explicit opt-in and an operator-approved search endpoint",
)
def test_live_search_records_reopen_with_the_same_sources(tmp_path):
    with TestClient(app(tmp_path)) as client:
        _, lead, other_members, _ = setup_team(client)
        session = lead["session_id"]
        workspace = client.app.state.workspace
        assert workspace.search is not None, "Configure an approved COURSE_SEARCH_URL before live retrieval"
        outcome = client.portal.call(
            workspace.execute, session, "search_web",
            json.dumps({"query": "site:sqlite.org SQLite transaction documentation", "limit": 3}),
        )
        assert outcome["ok"], outcome["error"] if not outcome["ok"] else ""
        record = outcome["result"]
        assert record["results"], "The approved provider returned no usable sources"
        assert all(result["title"] and urlsplit(result["url"]).hostname for result in record["results"])

    with TestClient(app(tmp_path)) as client:
        workspace = client.app.state.workspace
        args = json.dumps({"search_id": record["search_id"]})
        recovered = client.portal.call(workspace.execute, session, "get_search_result", args)
        assert recovered == {"ok": True, "result": record}
        denied = client.portal.call(workspace.execute, other_members[0]["session_id"], "get_search_result", args)
        assert not denied["ok"]
        print("live search sources:", sorted({urlsplit(result["url"]).hostname for result in record["results"]}))
