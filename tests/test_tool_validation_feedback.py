import json
from types import SimpleNamespace

from collaboration_runtime.delegation_tools import DelegationTools


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


def test_compact_opt_in_preserves_legacy():
    tool = DelegationTools(Service(), Auth(), None)
    assert tool.execute("session", "list_delegation", "{}")['result'] == {"legacy": True, "active_goals": []}
    assert tool.execute("session", "list_delegation", '{"compact": true, "limit": 3}')['result']['limit'] == 3
    assert tool.execute("session", "list_delegation", '{"limit": 50}')['status'] == 422
