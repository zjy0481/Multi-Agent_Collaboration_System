"""Opt-in test of delegation tools using the configured course model."""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.model import CourseModel
from test_collaboration import OPERATOR, Sandbox, auth, bootstrap
from test_delegation import action, confirm_all, context, create, key, ok, terms
from test_scheduler import wait_until


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_DELEGATION_TESTS") != "1", reason="explicit live course opt-in required",
)


def run_turn(client, member, token, prompt, expected_tools):
    endpoint = f"/v1/sessions/{member['session_id']}/turns"
    turn = ok(client.post(endpoint, headers=auth(token), json={"text": prompt}))
    path = f"{endpoint}/{turn['turn_id']}"

    def finished():
        state = ok(client.get(path, headers=auth(token)))
        return state if state["status"] != "running" else None

    state = wait_until(finished, timeout=180)
    assert state["status"] == "completed", {"status": state["status"], "error": state["error"]}
    stream = client.get(f"{path}/events", headers=auth(token))
    assert stream.is_success
    events = [json.loads(line.removeprefix("data: ")) for line in stream.text.splitlines() if line.startswith("data: ")]
    assert any(event["type"] == "model.started" and event["mode"] == "course" for event in events)
    completed = [event for event in events if event["type"] == "tool.completed"]
    observed = [event["name"] for event in completed]
    assert observed == expected_tools, observed
    assert all(event["result"]["ok"] for event in completed), [event["result"] for event in completed]
    started = {(event["name"], event["tool_call_id"]) for event in events if event["type"] == "tool.started"}
    assert started == {(event["name"], event["tool_call_id"]) for event in completed}
    assert len(started) == len(completed)
    print(f"{member['role_id']} completed: {', '.join(observed)} ({len(completed)} matched tool receipts)")
    return completed[-1]["result"]["result"]


def exact_tool(name, arguments):
    return (f"Call the {name} tool once with exactly these JSON arguments: {json.dumps(arguments)}. "
            "After it responds, summarize the actual result briefly. Do not use any other tool.")


def test_live_course_model_negotiates_and_registers(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLABORATION_GIT_TARGETS_DIR", raising=False)
    config = os.environ.get("DELEGATION_COURSE_ENV_FILE")
    model = CourseModel.from_env(Path(config) if config else None)
    application = create_app(model=model, sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
                             operator_token=OPERATOR, search=object(), schedule=False)
    with TestClient(application) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer = others[:2]
        lead_id, owner_id, reviewer_id = (member["member_id"] for member in (lead, owner, reviewer))
        lead_token = tokens[lead_id]
        candidate = {"request_id": key(), "scope_id": key(), "terms": terms(
            owner_id, reviewer_id, deliverables=["a synthetic checklist"],
            acceptance_criteria=["the checklist has two items"],
        )}
        draft = run_turn(
            client, lead, lead_token,
            "First call list_delegation with {} to locate the active goal. Then call create_proposal once using "
            f"its active_goals[0].id as goal_id together with these other arguments: {json.dumps(candidate)}. "
            "After both tools respond, summarize the actual result briefly. Use no other tools.",
            ["list_delegation", "create_proposal"],
        )
        proposal = draft["proposal_id"]
        assert draft["version"] == 1 and set(draft["pending_member_ids"]) == {lead_id, owner_id, reviewer_id}
        for member_id in (lead_id, reviewer_id):
            ok(action(client, proposal, tokens[member_id], "confirm"))
        confirmed = run_turn(
            client, owner, tokens[owner_id],
            exact_tool("confirm_proposal", {"proposal_id": proposal, "request_id": key(), "expected_version": 1}),
            ["confirm_proposal"],
        )
        assert confirmed["pending_member_ids"] == []
        assigned = run_turn(
            client, lead, lead_token,
            exact_tool("assign_task", {"proposal_id": proposal, "request_id": key(), "expected_version": 1}),
            ["assign_task"],
        )
        assert assigned["revision"] == 1
        task_id = assigned["task_id"]
        revised_terms = {**candidate["terms"], "acceptance_criteria": ["the checklist has three items"]}
        revision = create(client, base, lead_token, goal, revised_terms, task_id=task_id, base_revision=1)
        confirm_all(client, revision["proposal_id"], tokens)
        updated = run_turn(
            client, lead, lead_token,
            exact_tool("update_task", {"proposal_id": revision["proposal_id"], "request_id": key(), "expected_version": 1}),
            ["update_task"],
        )
        assert updated["task_id"] == task_id and updated["revision"] == 2
        endpoint = f"{base}/tasks/{task_id}"
        history = ok(client.get(endpoint, headers=auth(lead_token), params={"revision": 1}))
        current = ok(client.get(endpoint, headers=auth(lead_token)))
        assert history["terms"] == candidate["terms"] | {"parent_task_id": None}
        assert current["terms"] == revised_terms | {"parent_task_id": None}
        print(f"Course mode: {model.identity['model']} at {model.identity['endpoint']}; task registered and revised to 2")
