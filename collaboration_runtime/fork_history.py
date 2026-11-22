"""Project explicitly shared parent turns without importing unrelated tool results."""

import json

from .teams import TeamNotFound

MAX_HISTORY_CHARS = 96_000


def _input_allowed(message, fork, collaboration, parent):
    try:
        payload = json.loads(message["content"])
    except (TypeError, ValueError):
        # A manually supplied input can be shared by explicitly selecting its completed turn.
        return True
    if not isinstance(payload, dict) or "delivery" not in payload:
        return True
    item = payload.get("message")
    if not isinstance(item, dict):
        return False
    try:
        actual = collaboration.get_message(fork["team_id"], parent, item["id"])
    except (KeyError, TeamNotFound):
        return False
    if actual["recipient_id"] is not None or item != actual:
        return False
    for kind in ("proposal", "task"):
        if kind not in payload:
            continue
        context = payload[kind]
        scope_id = (context.get("task_id") or context.get("scope_id")) if kind == "proposal" else context.get("id")
        if context.get("goal_id") != fork["goal_id"] or scope_id != fork["scope_id"]:
            return False
        if not set(context["terms"]["input_version_ids"]) <= set(fork["history_version_ids"]):
            return False
        if kind == "task":
            if context["revision"] != fork["expected_revision"]:
                return False
            collaboration.delegation.get_task(fork["team_id"], parent, context["id"], context["revision"])
        else:
            collaboration.delegation.get_proposal(fork["team_id"], parent, context["id"], context["version"])
    # Deliveries with additional private inputs need an explicit material handoff.
    return set(payload) <= {"delivery", "message", "task", "proposal"}


def _turn_projection(messages, fork, collaboration, parent):
    pending = {}
    projected = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            if pending or not _input_allowed(message, fork, collaboration, parent):
                return []
            projected.append({"role": "user", "content": message["content"]})
        elif role == "assistant":
            if pending:
                return []
            approved_calls = message.get("tool_calls") or []
            for call in approved_calls:
                fn = call["function"]
                if fn["name"] != "read_artifact":
                    return []
                arguments = json.loads(fn["arguments"])
                if set(arguments) != {"version_id"}:
                    return []
                version = arguments["version_id"]
                if version not in fork["history_version_ids"]:
                    return []
                pending[call["id"]] = version
            if approved_calls:
                projected.append({"role": "assistant", "content": None, "tool_calls": approved_calls})
            # A reply can paraphrase older private turns. Share explicit input and authorized material only.
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                return []
            version = pending.pop(call_id)
            result = json.loads(message["content"])
            if not result.get("ok") or result.get("result", {}).get("version_id") != version:
                return []
            artifact = result["result"]
            data = {"ok": True, "result": {"version_id": version, "content": artifact["content"]}}
            projected.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(data, ensure_ascii=False)})
        else:
            return []
    return [] if pending else projected


def project_history(forks, assistant_id):
    fork = forks.require_active(assistant_id)
    parent = forks.collaboration.auth.member(fork["parent_session_id"])
    rows = forks.db.execute("""
        SELECT history FROM turns
        WHERE role = :parent AND status = 'completed'
          AND rowid <= (SELECT rowid FROM turns WHERE id = :anchor)
          AND id IN (SELECT value FROM json_each(:selected))
        ORDER BY rowid DESC
    """, {"parent": parent.session_id, "anchor": fork["from_turn_id"],
          "selected": json.dumps(fork["history_turn_ids"])})
    groups = []
    remaining = MAX_HISTORY_CHARS
    for row in rows:
        try:
            turn = _turn_projection(json.loads(row["history"]), fork, forks.collaboration, parent)
        except (TypeError, ValueError, KeyError, AttributeError, TeamNotFound):
            continue
        size = len(json.dumps(turn, ensure_ascii=False))
        if turn and size <= remaining:
            groups.append(turn)
            remaining -= size
    return [message for turn in reversed(groups) for message in turn]
