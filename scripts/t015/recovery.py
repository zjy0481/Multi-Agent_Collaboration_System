"""Pure selection policy for bounded operator recovery."""
TASK_DELIVERIES = {"task.assigned", "task.updated", "task.ready", "task.returned", "task.submitted", "clarification.resume", "clarification.correction"}

def retry_key(turn, source, current_messages, failures_at_start, attempted, counts, seconds_left):
    key = (turn['role'], turn['purpose'], turn['message_id'])
    generation = turn.get('generation')
    failure = turn.get('failure') or {}
    if not (turn['purpose'] in TASK_DELIVERIES or turn['message_id'] == source):
        return None
    if (turn['message_id'] not in current_messages or turn['status'] != 'failed'
            or turn['id'] in failures_at_start or generation is None
            or failure.get('generation') != generation
            or not (failure.get('type') == 'model.step_limit' or
                    (failure.get('type') == 'model.failed' and failure.get('transient') is True))
            or (turn['id'], generation) in attempted or counts.get(key, 0) >= 2
            or seconds_left < 60):
        return None
    return key
