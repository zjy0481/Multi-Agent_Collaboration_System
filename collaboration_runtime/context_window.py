"""Bound the serialized message history while retaining complete tool exchanges."""
import json

from .model import ModelError


def size(messages):
    return len(json.dumps(messages, ensure_ascii=False))


def _exchanges(messages):
    groups = []
    for message in messages:
        if message.get("role") == "tool" and groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def _compact_delivery(message):
    payload = json.loads(message["content"])
    keep = {key: payload[key] for key in ("message", "goal_id", "source_message", "fork_message") if key in payload}
    keep["delivery"] = {key: value for key, value in payload["delivery"].items()
                        if key in ("purpose", "event_id", "session_id", "message_id")}
    if payload.get("answers"):
        keep["answer_message_ids"] = [answer["id"] for answer in payload["answers"]]
        keep["latest_answer"] = payload["answers"][-1]
    for key in ("dependency_outputs", "dependency_blockers"):
        if key in payload:
            keep[key] = payload[key]
    for key in ("task", "proposal", "result_reference"):
        if key in payload:
            keep[key] = {k: v for k, v in payload[key].items() if k in
                         ("id", "revision", "submission_id", "review_id", "version", "status")}
    keep["history_note"] = "Retrieve the full authorized task/proposal or message by ID."
    return {"role": "user", "content": json.dumps(keep, ensure_ascii=False)}


def bounded_messages(system, history, current, budget, state):
    """History is expendable. Current input, tool pairs and later nudges stay in causal order."""
    full = [system, *history, *current]
    if state is None and size(full) <= budget:
        return full
    first = current[0]
    if size([system, first]) > budget // 2:
        first = _compact_delivery(first)
    prefix = [system]
    if state:
        prefix.append({"role": "user", "content": json.dumps(state, ensure_ascii=False)})
    prefix.append(first)
    if size(prefix) > budget:
        raise ModelError("Current delivery and required context exceed the model input budget; use a smaller delivery")
    # Keep whole recent prior turns and atomic assistant/tool exchanges. Never edit the audit journal.
    prior_turns = []
    for message in history:
        if message["role"] == "user" or not prior_turns:
            prior_turns.append([])
        prior_turns[-1].append(message)
    recent = {}
    remaining = budget - size(prefix)
    groups = _exchanges(current[1:])
    mandatory = {i for i, group in enumerate(groups) if group[0]["role"] == "user"}
    latest_decision = next((i for i in reversed(range(len(groups))) if groups[i][0]["role"] == "assistant"), None)
    if latest_decision is not None:
        mandatory.add(latest_decision)
    # Reserve the current steering and latest decision before taking older evidence.
    order = sorted(mandatory, key=lambda i: groups[i][0]["role"] != "user")
    order.extend(i for i in reversed(range(len(groups))) if i not in mandatory)
    for i in order:
        group = groups[i]
        cost = size(group)
        if cost > remaining and len(group) > 1 and i in mandatory:
            decision, *receipts = group
            compact = [decision]
            for receipt in receipts:
                content = receipt["content"]
                if len(content) > 1500:
                    content = json.dumps({"excerpt": content[:1200], "omitted_chars": len(content) - 1200,
                                          "hint": "Full result retained in turn receipts; retrieve the object by ID."})
                compact.append({**receipt, "content": content})
            if size(compact) > remaining:
                # Preserve call IDs, names and pairing, replacing only oversized historical arguments.
                calls = []
                for call in decision["tool_calls"]:
                    function = call["function"]
                    if len(function["arguments"]) > 512:
                        function = {**function, "arguments": json.dumps({"archived_arguments": True})}
                    calls.append({**call, "function": function})
                compact[0] = {**decision, "tool_calls": calls}
            group, cost = compact, size(compact)
        if cost > remaining:
            if i in mandatory:
                raise ModelError("Current steering and latest tool exchange exceed the model input budget")
            continue
        recent[i] = group
        remaining -= cost
    prior = []
    for group in reversed(prior_turns):
        cost = size(group)
        if cost > remaining:
            break
        prior.append(group)
        remaining -= cost
    return [system, *(m for group in reversed(prior) for m in group), *prefix[1:],
            *(m for i in sorted(recent) for m in recent[i])]
