import json
from types import SimpleNamespace

from collaboration_runtime.delegation_tools import DelegationTools
from collaboration_runtime.teams import TeamNotFound


class Auth:
    def member(self, session_id):
        return SimpleNamespace(team_id="team")


class Service:
    def snapshot(self, team_id, actor):
        return {"legacy": True}

    def active_goals(self, team_id, actor):
        return []

    def compact_snapshot(self, team_id, actor, goal_id=None, limit=50):
        return {"goal_id": goal_id, "limit": limit}


def test_validation_feedback_is_bounded_and_value_free():
    tool = DelegationTools(Service(), Auth(), None)
    body = {"request_id": "r", "task_id": "t", "submission_id": "s", "expected_revision": 1,
            "decision": "approved", "findings": [{"criterion": 1, "evidence": "secret value", "requirement": "secret"}],
            "private_secret_key": "secret"}
    result = tool.execute("session", "review_result", json.dumps(body))
    assert result["error"] == "Invalid delegation tool arguments" and result["status"] == 422
    assert {"path": ["findings", 0, "met"], "code": "missing"} in result["errors"]
    assert {"path": ["findings", 0, "requirement"], "code": "extra_forbidden"} in result["errors"]
    assert "secret" not in json.dumps(result)
    malformed = tool.execute("session", "review_result", '{"secret')
    assert malformed["errors"][0]["code"] == "json_invalid"
    assert "secret" not in json.dumps(malformed)


def test_compact_inference_preserves_legacy_and_explicit_modes():
    tool = DelegationTools(Service(), Auth(), None)
    assert tool.execute("session", "list_delegation", "{}")['result'] == {"legacy": True, "active_goals": []}
    assert tool.execute("session", "list_delegation", '{"compact": true, "limit": 3}')['result']['limit'] == 3
    assert tool.execute("session", "list_delegation", '{"limit": 3}')['result'] == {"goal_id": None, "limit": 3}
    assert tool.execute("session", "list_delegation", '{"goal_id": "goal"}')['result'] == {"goal_id": "goal", "limit": 50}
    assert tool.execute("session", "list_delegation", '{"goal_id": null}')['result'] == {"goal_id": None, "limit": 50}
    assert tool.execute("session", "list_delegation", '{"compact": false}')['result'] == {"legacy": True, "active_goals": []}
    assert tool.execute("session", "list_delegation", '{"compact": false, "limit": 50}')['status'] == 422
    assert tool.execute("session", "list_delegation", '{"compact": false, "goal_id": null}')['status'] == 422


def test_get_task_not_found_has_uniform_recovery_guidance():
    class MissingTaskService(Service):
        def get_task(self, team_id, actor, task_id, revision):
            raise TeamNotFound("Task not found")

    tool = DelegationTools(MissingTaskService(), Auth(), None)
    results = [tool.execute("session", "get_task", json.dumps(args)) for args in (
        {"task_id": "missing"}, {"task_id": "inaccessible"},
        {"task_id": "visible", "revision": 999},
    )]
    assert all(result == results[0] for result in results)
    assert results[0]["status"] == 404
    assert "list_delegation" in results[0]["next_action"]
    assert "Lead" in results[0]["next_action"] and "Repeating" in results[0]["next_action"]
    conflict = tool.execute("session", "list_delegation", '{"compact": false, "limit": 3}')
    assert conflict["status"] == 422 and "compact=true or omit compact" in conflict["error"]
    schema = next(item["function"] for item in tool.tools_for("session") if item["function"]["name"] == "list_delegation")
    assert "omit compact" in schema["parameters"]["properties"]["compact"]["description"]
