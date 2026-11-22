"""T003: durable clarification routing, recovery and correction acceptance."""
import json

from fastapi.testclient import TestClient
from collaboration_runtime.local_auth import OPERATOR_ID

from test_collaboration import OPERATOR, app, auth, send, setup_team, snapshot


def inbox(client, base, token):
    response = client.get(f"{base}/deliveries", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def ask(client, member, request_id, source_id, text):
    arguments = json.dumps({"request_id": request_id, "source_message_id": source_id, "text": text})
    response = client.portal.call(
        client.app.state.collaboration.execute_tool, member["session_id"], "ask_human", arguments
    )
    assert response["ok"], response
    return response["result"]


def test_two_questions_restart_reply_priority_idempotency_and_correction(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens = setup_team(client)
        first, second, independent = others[:3]
        token = lambda member: tokens[member["member_id"]]
        first_work = send(client, base, OPERATOR, request_id="work-1", text="Investigate A", mentions=[first["member_id"]])
        second_work = send(client, base, OPERATOR, request_id="work-2", text="Investigate B", mentions=[second["member_id"]])
        other_work = send(client, base, OPERATOR, request_id="work-3", text="Independent work", mentions=[independent["member_id"]])
        q1 = ask(client, first, "question-1", first_work["message_id"], "Which rule applies to A?")
        assert ask(client, first, "question-1", first_work["message_id"], "Which rule applies to A?") == q1
        q2 = ask(client, second, "question-2", second_work["message_id"], "Which rule applies to B?")
        for question, asker, work in [(q1, first, first_work), (q2, second, second_work)]:
            entry = question["question"]
            assert entry["asker_id"] == asker["member_id"]
            assert entry["source_message_id"] == work["message_id"]
            assert entry["answer_message_id"] is None
            assert not any(d["message_id"] == work["message_id"] for d in inbox(client, base, token(asker)))
        assert any(d["message_id"] == other_work["message_id"] for d in inbox(client, base, token(independent)))
        questions = {q["question_id"]: q for q in snapshot(client, base, OPERATOR)["questions"]}
        assert questions[q1["question"]["question_id"]]["answer_message_id"] is None
        assert questions[q2["question"]["question_id"]]["answer_message_id"] is None

    with TestClient(app(tmp_path)) as client:
        question_id = q1["question"]["question_id"]
        answer_payload = {"request_id": "answer-1", "text": "Use rule A", "reply_to": question_id,
                          "mentions": [second["member_id"]]}
        answer = send(client, base, OPERATOR, **answer_payload)
        assert send(client, base, OPERATOR, **answer_payload) == answer
        duplicate = client.post(f"{base}/messages", headers=auth(OPERATOR),
                                json={**answer_payload, "request_id": "answer-again"})
        assert duplicate.status_code == 409
        first_inbox = inbox(client, base, token(first))
        resumed = [d for d in first_inbox if d["purpose"] == "clarification.resume" and d["message_id"] == answer["message_id"]]
        assert len(resumed) == 1 and resumed[0]["status"] == "pending"
        assert not any(d["message_id"] == answer["message_id"] for d in inbox(client, base, token(second)))
        assert not any(d["message_id"] == second_work["message_id"] for d in inbox(client, base, token(second)))
        assert any(d["message_id"] == other_work["message_id"] for d in inbox(client, base, token(independent)))
        questions = {q["question_id"]: q for q in snapshot(client, base, OPERATOR)["questions"]}
        assert questions[question_id]["answer_message_id"] == answer["message_id"]
        assert questions[q2["question"]["question_id"]]["answer_message_id"] is None

        # A different participant explicitly cites the decision before it changes.
        send(client, base, token(independent), request_id="adopt", text="I will use that rule", reply_to=answer["message_id"])
        correction = send(client, base, OPERATOR, request_id="correct-1", text="Correction: use rule C", reply_to=answer["message_id"])
        for member in (first, independent):
            delivered = [d for d in inbox(client, base, token(member))
                         if d["message_id"] == correction["message_id"] and d["purpose"] == "clarification.correction"]
            assert len(delivered) == 1
        assert not any(d["message_id"] == correction["message_id"] for d in inbox(client, base, token(second)))


def test_same_work_waits_for_every_answer(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, _, others, tokens = setup_team(client)
        member = others[0]
        token = tokens[member["member_id"]]
        work = send(client, base, OPERATOR, request_id="work", text="Work", mentions=[member["member_id"]])
        q1 = ask(client, member, "q1", work["message_id"], "First question?")
        q2 = ask(client, member, "q2", work["message_id"], "Second question?")
        a1 = send(client, base, OPERATOR, request_id="a1", text="First answer", reply_to=q1["question"]["question_id"])
        assert not any(d["purpose"] == "clarification.resume" for d in inbox(client, base, token))
        a2 = send(client, base, OPERATOR, request_id="a2", text="Second answer", reply_to=q2["question"]["question_id"])
        resumed = [d for d in inbox(client, base, token) if d["purpose"] == "clarification.resume"]
        assert len(resumed) == 1
        assert resumed[0]["source_message_id"] == work["message_id"]
        assert resumed[0]["answer_message_ids"] == [a1["message_id"], a2["message_id"]]
        assert resumed[0]["message_id"] == a2["message_id"]


def test_private_question_and_answer_do_not_leak(tmp_path):
    from test_collaboration import events

    with TestClient(app(tmp_path)) as client:
        base, _, others, tokens = setup_team(client)
        asker, outsider = others[:2]
        asker_token, outsider_token = tokens[asker["member_id"]], tokens[outsider["member_id"]]
        cursor = snapshot(client, base, outsider_token)["cursor"]
        work = send(client, base, OPERATOR, request_id="private-work", text="Private work", recipient_id=asker["member_id"])
        question = ask(client, asker, "private-q", work["message_id"], "Private question?")
        qid = question["question"]["question_id"]
        # An agent may send a private thread message, but it is not the human's decision.
        reply = client.post(f"{base}/messages", headers=auth(asker_token), json={
            "request_id": "agent-reply", "text": "My own reply", "recipient_id": OPERATOR_ID, "reply_to": qid,
        })
        assert reply.status_code == 201, reply.text
        before = {q["question_id"]: q for q in snapshot(client, base, OPERATOR)["questions"]}
        assert before[qid]["answer_message_id"] is None
        answer = send(client, base, OPERATOR, request_id="private-answer", text="Private answer", reply_to=qid)
        for token in (OPERATOR, asker_token):
            state = snapshot(client, base, token)
            assert {qid, answer["message_id"]} <= {m["id"] for m in state["messages"]}
            assert any(q["question_id"] == qid and q["answer_message_id"] == answer["message_id"] for q in state["questions"])
        outside = snapshot(client, base, outsider_token)
        assert not ({work["message_id"], qid, answer["message_id"]} & {m["id"] for m in outside["messages"]})
        assert all(q["question_id"] != qid for q in outside["questions"])
        replay = events(client, base, outsider_token, after=cursor)
        assert all(qid not in json.dumps(data) and answer["message_id"] not in json.dumps(data) for _, data, _ in replay)
