"""T012 owner-scoped lessons, cross-goal handoffs, and observable adoption."""
from functools import partial
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.experience import ExperienceService, ExperienceTools, router
from collaboration_runtime.teams import LeadDecision
from test_collaboration import OPERATOR, Sandbox, app, auth, bootstrap, send, setup_team


def key():
    return uuid4().hex


def ok(response):
    assert response.is_success, response.text
    return response.json()


def second_goal(client, base, lead, token):
    source = send(client, base, OPERATOR, request_id=key(), text="Second goal", kind="assignment")
    return ok(client.post(
        f"{base}/goals",
        headers=auth(token),
        json={"request_id": key(), "source_message_id": source["message_id"]},
    ))["goal_id"]


def install(client, **options):
    service = client.portal.call(partial(
        ExperienceService,
        client.app.state.collaboration,
        client.app.state.workspace,
        client.app.state.runtime.forks,
        **options,
    ))
    client.app.state.experience = service
    client.app.state.experience_tools = ExperienceTools(
        service, client.app.state.auth, client.app.state.workspace,
    )
    client.app.include_router(router)
    return service


def publish(client, team_id, member, readers, content):
    return client.portal.call(partial(
        client.app.state.workspace.artifacts.publish,
        team_id=team_id,
        author_session_id=member["session_id"],
        source_path=f"{member['role_id']}-evidence.txt",
        content=content,
        reader_session_ids=[reader["session_id"] for reader in readers],
    ))


def record(goal, source):
    return {
        "request_id": key(),
        "goal_id": goal,
        "title": "Use the original booking record",
        "text": "Check the original booking record before calculating cancellation counts.",
        "sources": [{"kind": "artifact", "source_id": source["version_id"]}],
        "applies_when": "A later goal derives metrics from an earlier booking system.",
        "invalid_when": "The booking schema or cancellation definition changes.",
    }


def retrieve(client, base, token, goal, query=""):
    return client.post(
        f"{base}/lessons/search",
        headers=auth(token),
        json={"request_id": key(), "target_goal_id": goal, "query": query},
    )


def test_cross_goal_handoff_retrieval_use_and_observation_survive_restart(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        assignment = send(client, base, OPERATOR, request_id=key(), text="First goal", kind="assignment")
        first_goal = ok(client.post(
            f"{base}/goals",
            headers=auth(tokens[lead["member_id"]]),
            json={"request_id": key(), "source_message_id": assignment["message_id"]},
        ))["goal_id"]
        second = second_goal(client, base, lead, tokens[lead["member_id"]])
        author, recipient, outsider = others[:3]
        author_token = tokens[author["member_id"]]
        recipient_token = tokens[recipient["member_id"]]
        outsider_token = tokens[outsider["member_id"]]
        install(client)

        team_id = base.rsplit("/", 1)[-1]
        source = publish(client, team_id, author, [author, recipient], b"real booking records")
        payload = record(first_goal, source)
        lesson = ok(client.post(f"{base}/lessons", headers=auth(author_token), json=payload))
        lesson_id = lesson["lesson_id"]
        assert ok(client.post(f"{base}/lessons", headers=auth(author_token), json=payload)) == lesson
        assert client.post(
            f"{base}/lessons",
            headers=auth(author_token),
            json={**payload, "text": "changed"},
        ).status_code == 409

        author_view = ok(client.get(f"{base}/lessons/{lesson_id}", headers=auth(author_token)))
        assert author_view["access"] == "owner"
        assert author_view["sources"][0]["source_id"] == source["version_id"]
        assert client.get(
            f"{base}/lessons/{lesson_id}", headers=auth(outsider_token),
            params={"target_goal_id": second},
        ).status_code == 404
        before = ok(retrieve(client, base, recipient_token, second, "booking"))
        assert before["lessons"] == []

        handoff_body = {
            "request_id": key(),
            "source_goal_id": first_goal,
            "target_goal_id": second,
            "lesson_ids": [lesson_id],
            "artifact_version_ids": [source["version_id"]],
            "recipient_id": recipient["member_id"],
            "purpose": "Continue the dashboard with the original booking records.",
        }
        handoff = ok(client.post(
            f"{base}/lesson-handoffs",
            headers=auth(author_token),
            json=handoff_body,
        ))
        assert ok(client.post(
            f"{base}/lesson-handoffs", headers=auth(author_token), json=handoff_body,
        )) == handoff
        assert handoff["lesson_ids"] == [lesson_id]
        assert client.post(
            f"{base}/lesson-handoffs",
            headers=auth(recipient_token),
            json={
                "request_id": key(),
                "source_goal_id": first_goal,
                "target_goal_id": second,
                "lesson_ids": [lesson_id],
                "recipient_id": outsider["member_id"],
                "purpose": "unauthorized re-share",
            },
        ).status_code == 404

        search_body = {"request_id": key(), "target_goal_id": second, "query": "booking"}
        found = ok(client.post(f"{base}/lessons/search", headers=auth(recipient_token), json=search_body))
        assert ok(client.post(
            f"{base}/lessons/search", headers=auth(recipient_token), json=search_body,
        )) == found
        assert client.post(
            f"{base}/lessons/search",
            headers=auth(recipient_token),
            json={**search_body, "query": "different"},
        ).status_code == 409
        assert [item["id"] for item in found["lessons"]] == [lesson_id]
        assert found["lessons"][0]["access"] == "handoff"
        assert found["lessons"][0]["handoff"]["artifact_version_ids"] == [source["version_id"]]
        assert ok(client.get(
            f"{base}/lessons/{lesson_id}",
            headers=auth(recipient_token),
            params={"target_goal_id": second},
        ))["text"] == payload["text"]
        assert client.get(
            f"{base}/lessons/{lesson_id}",
            headers=auth(outsider_token),
            params={"target_goal_id": second},
        ).status_code == 404
        assert ok(retrieve(client, base, outsider_token, second, "booking"))["lessons"] == []

        action_source = publish(client, team_id, recipient, [recipient], b"dashboard calculation")
        outcomes = ("behavior_changed", "no_change", "invalidated")
        receipts = []
        for outcome in outcomes:
            retrieval = ok(retrieve(client, base, recipient_token, second, "original booking"))
            use_body = {
                "request_id": key(),
                "lesson_id": lesson_id,
                "action": f"Applied the lesson before {outcome}.",
                "evidence": [f"Compared dashboard output for {outcome}."],
                "sources": [{"kind": "artifact", "source_id": action_source["version_id"]}],
            }
            use = ok(client.post(
                f"{base}/lesson-retrievals/{retrieval['retrieval_id']}/uses",
                headers=auth(recipient_token),
                json=use_body,
            ))
            assert ok(client.post(
                f"{base}/lesson-retrievals/{retrieval['retrieval_id']}/uses",
                headers=auth(recipient_token),
                json=use_body,
            )) == use
            observation_body = {
                "request_id": key(),
                "outcome": outcome,
                "note": f"Observed {outcome} in the second goal.",
                "evidence": [f"Dashboard result for {outcome}."],
                "recheck_items": ["Recheck if cancellation semantics change."],
            }
            observation = ok(client.post(
                f"{base}/lesson-uses/{use['use_id']}/observations",
                headers=auth(recipient_token),
                json=observation_body,
            ))
            assert ok(client.post(
                f"{base}/lesson-uses/{use['use_id']}/observations",
                headers=auth(recipient_token),
                json=observation_body,
            )) == observation
            receipts.append((retrieval, use, observation))

        trail = ok(client.get(
            f"{base}/experience",
            headers=auth(recipient_token),
            params={"target_goal_id": second},
        ))
        assert trail["counts"] == {
            "lessons": 1, "retrievals": 5, "uses": 3, "observations": 3, "handoffs": 1,
        }
        assert trail["handoffs"][0]["purpose"].startswith("Continue the dashboard")
        assert [item["outcome"] for item in trail["observations"]] == list(outcomes)
        assert {item["retrieval_id"] for item in trail["uses"]} == {
            retrieval["retrieval_id"] for retrieval, _, _ in receipts
        }
        assert ok(client.get(
            f"{base}/lesson-retrievals/{receipts[0][0]['retrieval_id']}",
            headers=auth(recipient_token),
        ))["lessons"][0]["id"] == lesson_id
        assert ok(client.get(
            f"{base}/experience",
            headers=auth(outsider_token),
            params={"target_goal_id": second},
        ))["counts"]["lessons"] == 0

    with TestClient(app(tmp_path)) as recovered:
        install(recovered)
        restored = ok(recovered.get(
            f"{base}/experience",
            headers=auth(recipient_token),
            params={"target_goal_id": second},
        ))
        assert restored["counts"] == {
            "lessons": 1, "retrievals": 5, "uses": 3, "observations": 3, "handoffs": 1,
        }
        assert [item["outcome"] for item in restored["observations"]] == list(outcomes)


def test_source_permission_is_checked_again_before_search_and_match(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        assignment = send(client, base, OPERATOR, request_id=key(), text="Source goal", kind="assignment")
        first = ok(client.post(
            f"{base}/goals",
            headers=auth(tokens[lead["member_id"]]),
            json={"request_id": key(), "source_message_id": assignment["message_id"]},
        ))["goal_id"]
        second = second_goal(client, base, lead, tokens[lead["member_id"]])
        author, recipient, outsider = others[:3]
        author_token = tokens[author["member_id"]]
        recipient_token = tokens[recipient["member_id"]]
        outsider_token = tokens[outsider["member_id"]]
        install(client)
        source = publish(client, base.rsplit("/", 1)[-1], author, [author, recipient], b"traceable source")
        lesson_id = ok(client.post(
            f"{base}/lessons",
            headers=auth(author_token),
            json=record(first, source),
        ))["lesson_id"]
        ok(client.post(
            f"{base}/lesson-handoffs",
            headers=auth(author_token),
            json={
                "request_id": key(),
                "source_goal_id": first,
                "target_goal_id": second,
                "lesson_ids": [lesson_id],
                "artifact_version_ids": [source["version_id"]],
                "recipient_session_id": recipient["session_id"],
                "purpose": "Use the traceable source in the next goal.",
            },
        ))
        assert ok(retrieve(client, base, recipient_token, second, "booking"))["lessons"]

        def revoke_source():
            with client.app.state.runtime.db:
                client.app.state.runtime.db.execute(
                    "UPDATE artifact_versions SET reader_session_ids = ? WHERE version_id = ?",
                    (json.dumps([]), source["version_id"]),
                )

        client.portal.call(revoke_source)
        assert ok(retrieve(client, base, recipient_token, second, "booking"))["lessons"] == []
        assert client.get(
            f"{base}/lessons/{lesson_id}",
            headers=auth(recipient_token),
            params={"target_goal_id": second},
        ).status_code == 404
        assert client.get(
            f"{base}/lessons/{lesson_id}",
            headers=auth(outsider_token),
            params={"target_goal_id": second},
        ).status_code == 404


def test_member_handoff_to_unshared_source_is_rejected(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        assignment = send(client, base, OPERATOR, request_id=key(), text="Private source goal", kind="assignment")
        first = ok(client.post(
            f"{base}/goals",
            headers=auth(tokens[lead["member_id"]]),
            json={"request_id": key(), "source_message_id": assignment["message_id"]},
        ))["goal_id"]
        second = second_goal(client, base, lead, tokens[lead["member_id"]])
        author, recipient = others[:2]
        author_token = tokens[author["member_id"]]
        install(client)
        source = publish(client, base.rsplit("/", 1)[-1], author, [author], b"author only")
        lesson_id = ok(client.post(
            f"{base}/lessons",
            headers=auth(author_token),
            json=record(first, source),
        ))["lesson_id"]
        response = client.post(
            f"{base}/lesson-handoffs",
            headers=auth(author_token),
            json={
                "request_id": key(),
                "source_goal_id": first,
                "target_goal_id": second,
                "lesson_ids": [lesson_id],
                "artifact_version_ids": [source["version_id"]],
                "recipient_id": recipient["member_id"],
                "purpose": "Share an unshared source.",
            },
        )
        assert response.status_code == 404


class CompletedModel:
    async def complete(self, messages, tools):
        return {"role": "assistant", "content": "completed"}


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_authorized_assistant_uses_handoff_inside_active_fork_and_loses_access_on_revoke(tmp_path):
    selected_app = create_app(
        model=CompletedModel(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap,
        operator_token=OPERATOR, schedule=False,
    )
    with TestClient(selected_app) as client:
        base, lead, others, tokens = setup_team(client)
        first_assignment = send(client, base, OPERATOR, request_id=key(), text="First goal", kind="assignment")
        first = ok(client.post(
            f"{base}/goals",
            headers=auth(tokens[lead["member_id"]]),
            json={"request_id": key(), "source_message_id": first_assignment["message_id"]},
        ))["goal_id"]
        second = second_goal(client, base, lead, tokens[lead["member_id"]])
        author, outsider = others[0], others[2]
        author_token = tokens[author["member_id"]]
        install(client, assistant_read_tools={"read_file"})
        source = publish(client, base.rsplit("/", 1)[-1], author, [author], b"assistant source")
        lesson_id = ok(client.post(
            f"{base}/lessons",
            headers=auth(author_token),
            json=record(first, source),
        ))["lesson_id"]

        def activate_second():
            with client.app.state.runtime.db:
                client.app.state.runtime.db.execute(
                    "UPDATE team_goals SET status = 'completed' WHERE id = ?", (first,),
                )
                client.app.state.runtime.db.execute(
                    "UPDATE team_goals SET status = 'active' WHERE id = ?", (second,),
                )

        client.portal.call(activate_second)
        turn_id = ok(client.post(
            f"/v1/sessions/{author['session_id']}/turns",
            headers=auth(author_token),
            json={"text": "fork anchor"},
        ))["turn_id"]
        wait_for(lambda: ok(client.get(
            f"/v1/sessions/{author['session_id']}/turns/{turn_id}",
            headers=auth(author_token),
        ))["status"] == "completed")
        created = ok(client.post(
            f"/v1/sessions/{author['session_id']}/forks",
            headers=auth(author_token),
            json={
                "request_id": key(),
                "from_turn_id": turn_id,
                "goal_id": second,
                "purpose": "Read the explicitly handed-off lesson.",
                "scope_id": key(),
                "history_turn_ids": [turn_id],
                "version_ids": [source["version_id"]],
                "allowed_tools": ["read_file"],
                "contact_session_ids": [],
            },
        ))
        helper = created["session_id"]
        ok(client.post(
            f"{base}/lesson-handoffs",
            headers=auth(author_token),
            json={
                "request_id": key(),
                "source_goal_id": first,
                "target_goal_id": second,
                "lesson_ids": [lesson_id],
                "artifact_version_ids": [source["version_id"]],
                "recipient_session_id": helper,
                "purpose": "Assistant may retrieve this lesson in the bounded second-goal fork.",
            },
        ))
        tools = client.app.state.experience_tools
        names = {item["function"]["name"] for item in client.portal.call(tools.tools_for, helper)}
        assert names == {"retrieve_lessons", "read_lesson"}
        def execute(*args):
            return client.portal.call(tools.execute, *args)

        assert not execute(helper, "record_lesson", json.dumps(record(
            first, source,
        )))["ok"]
        result = execute(helper, "retrieve_lessons", json.dumps({
            "request_id": key(), "target_goal_id": second, "query": "original booking",
        }))
        assert result["ok"], result
        assert [item["id"] for item in result["result"]["lessons"]] == [lesson_id]
        direct = execute(helper, "read_lesson", json.dumps({
            "lesson_id": lesson_id, "target_goal_id": second,
        }))
        assert direct["ok"]
        assert direct["result"]["id"] == lesson_id
        assert execute(helper, "retrieve_lessons", json.dumps({
            "request_id": key(), "target_goal_id": first, "query": "",
        }))["status"] == 404
        outsider_result = execute(
            outsider["session_id"], "retrieve_lessons",
            json.dumps({"request_id": key(), "target_goal_id": second, "query": ""}),
        )
        assert outsider_result["ok"] and outsider_result["result"]["lessons"] == []

        ok(client.post(
            f"/v1/forks/{helper}/revoke",
            headers=auth(author_token),
            json={"request_id": key()},
        ))
        assert not execute(helper, "retrieve_lessons", json.dumps({
            "request_id": key(), "target_goal_id": second, "query": "",
        }))["ok"]
