"""Persisted execution sessions and the model/tool loop."""

import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sqlite3
from uuid import uuid4

from .collaboration import CollaborationInvalid
from .delegation_tools import TOOLS as DELEGATION_TOOLS
from .experience import ExperienceTools
from .journal import ExecutionJournal, GOAL_CONFIRMATION_PHASE
from .model import ModelError, ModelPaused, ModelUnavailable, complete_model, model_request_context
from .sandbox import SandboxError
from .workspace import WorkspaceNotFound

logger = logging.getLogger(__name__)

ROLES = {
    "developer": "You are the developer. Implement and test the requested changes. "
    "Keep drafts in /workspace and publish only files ready for the tester.",
    "tester": "You are the tester. Independently verify the published version and report "
    "observed results. Use list_artifacts for inputs under /inputs and /workspace for your tests.",
}


def _tool(name, description, properties):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


WORKSPACE_TOOL = _tool(
    "run_command",
    "Run a shell command in your private Linux workspace. Published inputs are read-only.",
    {"command": {"type": "string"}},
)

TOOLS = [
    WORKSPACE_TOOL,
    _tool(
        "publish_artifact",
        "Publish an immutable snapshot of one workspace file to the other role.",
        {"path": {"type": "string"}, "recipient": {"type": "string", "enum": list(ROLES)}},
    ),
    _tool("list_artifacts", "List artifact paths and versions available to you.", {}),
]
TOOL_PARAMETERS = {
    tool["function"]["name"]: set(tool["function"]["parameters"]["properties"])
    for tool in TOOLS
}


class TurnConflict(Exception):
    """A session already has a turn, or its active turn has changed."""


class StaleTurn(Exception):
    """A different generation owns this turn."""


class UnknownToolOutcome(Exception):
    """An interrupted tool must be checked before the work can continue."""


# These tools either only read or enforce their own stable business request key.
REPLAYABLE_TOOLS = {
    "ask_human", "get_message", "list_participants", "register_goal", "send_message",
    "list_artifacts", "list_workspace_readers", "read_artifact", "read_file", "get_search_result", "get_source",
    "get_git_action", "request_push",
    "fork_session", "send_fork_message", "decide_fork_result",
    "get_goal", "update_goal_acceptance", "close_goal", "activate_goal",
} | DELEGATION_TOOLS.keys() | ExperienceTools.MEMBER_TOOLS.keys()

DELIVERY_PROMPT = (
    "\n\nThe host provides an authenticated delivery as JSON in the current input. "
    "Work on the current delivery and its goal_id. Use message.id as the source for ask_human. "
    "On a new user assignment the Lead should register_goal with that message ID before delegating. "
    "Agree on the smallest complete deliverable and shared acceptance checks. Start independent work "
    "in parallel when the inputs allow it; state exact input versions, output names, and who reviews. "
    "Confirm a proposal only when its current terms are clear. The proposer registers the task once "
    "all required parties have confirmed. Acknowledge once when needed; use current task state instead "
    "of echoing confirmations or status messages. On an assigned, ready, or returned task, produce the deliverable, "
    "verify it, publish exact versions to the appropriate readers, and submit_result. "
    "For research, run a few focused searches, then read relevant primary sources and start the draft. "
    "Use durable source IDs or cite the actual source and passage you read. Search titles and snippets alone "
    "are leads. Mark claims awaiting evidence, and change approach or report a blocker if further searches "
    "produce no useful evidence or the provider reports degradation. "
    "When review_request is present, complete the review_result action using evidence for each criterion. "
    "The Lead records the required task deliveries in the goal acceptance and uses close_goal with the "
    "accepted versions when the goal is complete. For earlier assignments, use explicitly shared "
    "artifacts or recorded experience. A final reply stays in this session. Use send_message for "
    "a team-visible update when another member or the user needs to act."
)


@dataclass
class ActiveTurn:
    id: str
    generation: int
    task: asyncio.Task | None = None


@dataclass(frozen=True)
class CloseGoalCall:
    turn: ActiveTurn
    step: int
    position: int
    call_id: str


class Runtime:
    def __init__(self, model, sandbox, database: Path, max_steps: int = 20):
        self.model = model
        self._model_session_scope = str(Path(database).resolve())
        self.sandbox = sandbox
        self.max_steps = max_steps
        self.active: dict[str, ActiveTurn] = {}
        self.closing = False
        self.workspace_tools = None
        self.collaboration = None
        self.forks = None
        self.fork_messages = None
        self.experience_tools = None
        self.git_approvals = None
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                reply TEXT,
                error TEXT,
                history TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_running_turn
                ON turns(role) WHERE status = 'running';
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL REFERENCES turns(id),
                type TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_by_turn ON events(turn_id, seq);
            CREATE TABLE IF NOT EXISTS scheduled_turns (
                turn_id TEXT PRIMARY KEY REFERENCES turns(id),
                event_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                purpose TEXT NOT NULL,
                UNIQUE (event_id, session_id, purpose),
                FOREIGN KEY (event_id, session_id, purpose)
                    REFERENCES message_deliveries(event_id, session_id, purpose)
            );
        """)
        self.journal = ExecutionJournal(self.db)
        has_forks = self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'assistant_jobs'").fetchone()
        for row in self.db.execute("""
            SELECT t.id, s.turn_id AS scheduled FROM turns t
            LEFT JOIN scheduled_turns s ON s.turn_id = t.id WHERE t.status = 'running'
        """).fetchall():
            assistant = has_forks and self.db.execute(
                "SELECT 1 FROM assistant_jobs WHERE turn_id = ?", (row["id"],),
            ).fetchone()
            status = "recoverable" if row["scheduled"] is not None or assistant else "interrupted"
            self._finish(row["id"], status, error="Server restarted during the turn")

    def _event(self, turn_id: str, event_type: str, **payload):
        self.db.execute(
            "INSERT INTO events(turn_id, type, payload) VALUES (?, ?, ?)",
            (turn_id, event_type, json.dumps(payload, ensure_ascii=False)),
        )

    def _write_finish(self, turn_id: str, status: str, *, reply=None, error=None, history=None):
        self.db.execute(
            "UPDATE turns SET status = ?, reply = ?, error = ?, history = ? WHERE id = ?",
            (status, reply, error, json.dumps(history, ensure_ascii=False) if history else None, turn_id),
        )
        if status != "recoverable" and self._scheduled(turn_id):
            self.db.execute("""
                UPDATE message_deliveries SET status = ?
                WHERE (event_id, session_id, purpose) IN (
                    SELECT event_id, session_id, purpose FROM scheduled_turns WHERE turn_id = ?
                ) AND status = 'running'
            """, (status, turn_id))
        if status != "recoverable" and self.forks is not None:
            self.db.execute("UPDATE assistant_jobs SET status = ? WHERE turn_id = ? AND status = 'running'",
                            (status, turn_id))
        self._event(turn_id, f"turn.{status}", reply=reply, error=error)

    def _finish(self, turn_id: str, status: str, **fields):
        with self.db:
            self._write_finish(turn_id, status, **fields)

    def _scheduled(self, turn_id):
        return self.db.execute("SELECT 1 FROM scheduled_turns WHERE turn_id = ?", (turn_id,)).fetchone() is not None

    def create_turn(self, role, text):
        turn_id = uuid4().hex
        self.db.execute("INSERT INTO turns(id, role, status) VALUES (?, ?, 'running')", (turn_id, role))
        self.journal.create(turn_id, [{"role": "user", "content": text}])
        self._event(turn_id, "turn.started", text=text, generation=1)
        return turn_id

    def reopen_turn(self, turn_id):
        generation = self.journal.next_generation(turn_id)
        self.db.execute("UPDATE turns SET status = 'running', error = NULL WHERE id = ?", (turn_id,))
        self._event(turn_id, "turn.resumed", generation=generation)

    def launch(self, role, turn_id, prompt, workspace_id):
        turn = ActiveTurn(turn_id, self.journal.checkpoint(turn_id)["generation"])
        self.active[role] = turn
        turn.task = asyncio.create_task(self._run(role, turn, prompt, workspace_id))

    def start(self, role: str, text: str, *, prompt: str | None = None, workspace_id: str | None = None) -> str:
        if reason := getattr(self.model, "paused_reason", None):
            raise ModelPaused(reason)
        if self.closing or role in self.active or self.db.execute("""
            SELECT 1 FROM turns WHERE role = ? AND status IN ('running', 'recoverable')
        """, (role,)).fetchone():
            raise TurnConflict("The session already has a running turn or is shutting down")
        with self.db:
            turn_id = self.create_turn(role, text)
        self.launch(role, turn_id, prompt, workspace_id)
        return turn_id

    def steer(self, role: str, turn_id: str, text: str):
        turn = self.active.get(role)
        if self.closing or turn is None or turn.id != turn_id:
            raise TurnConflict("This is no longer the running turn")
        checkpoint = self.journal.checkpoint(turn_id)
        if checkpoint["phase"] == GOAL_CONFIRMATION_PHASE:
            raise TurnConflict("This turn is confirming a closed goal")
        if len(checkpoint["pending"]) >= 16:
            raise TurnConflict("The turn already has too many pending inputs")
        with self.db:
            self.journal.queue_input(turn_id, text)
            self._event(turn_id, "turn.input_queued", text=text)

    def get_turn(self, role: str, turn_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT id AS turn_id, role, status, reply, error FROM turns WHERE role = ? AND id = ?",
            (role, turn_id),
        ).fetchone()
        return dict(row) if row else None

    def list_turns(self, role):
        return [dict(row) for row in self.db.execute("""
            SELECT id AS turn_id, role, status, reply, error FROM turns WHERE role = ? ORDER BY rowid
        """, (role,))]

    def events(self, turn_id: str, after: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT seq, type, payload FROM events WHERE turn_id = ? AND seq > ? ORDER BY seq LIMIT 100",
            (turn_id, after),
        )
        return [
            {"seq": row["seq"], "type": row["type"], **json.loads(row["payload"])}
            for row in rows
        ]

    def _history(self, role: str, message_ids: set[str] | None = None) -> list[dict]:
        rows = self.db.execute("""
            SELECT t.history, d.message_id FROM turns t
            LEFT JOIN scheduled_turns s ON s.turn_id = t.id
            LEFT JOIN message_deliveries d USING (event_id, session_id, purpose)
            WHERE t.role = ? AND t.status IN ('completed', 'waiting_human', 'waiting_approval')
            ORDER BY t.rowid
        """, (role,))
        return [message for row in rows if message_ids is None or row["message_id"] in message_ids
                for message in json.loads(row["history"])]

    async def _complete_model(self, role, turn_id, goal_id, messages, tools):
        token = model_request_context.set({"turn_id": turn_id, "goal_id": goal_id})
        try:
            return await complete_model(self.model, messages, tools, session_id=f"{self._model_session_scope}:{role}")
        finally:
            model_request_context.reset(token)

    def _check_generation(self, turn):
        if not self.journal.current(turn.id, turn.generation):
            raise StaleTurn

    def _advance(self, turn, step, phase, current):
        if not self.journal.advance(turn.id, turn.generation, step, phase, current):
            raise StaleTurn

    def _check_assistant_job(self, role, turn_id):
        self.workspace_tools.actor(role)
        job = self.db.execute("SELECT input FROM assistant_jobs WHERE turn_id = ?", (turn_id,)).fetchone()
        if job is None:
            raise WorkspaceNotFound("Assistant job is unavailable")
        self.fork_messages.authorize_job(role, job["input"])

    async def _run(self, role: str, turn: ActiveTurn, prompt, workspace_id):
        scheduled = self._scheduled(turn.id)
        is_fork = self.forks is not None and self.forks.exists(role)
        fork_message_id = None

        def check_scope():
            if is_fork:
                self._check_assistant_job(role, turn.id)
            elif fork_message_id is not None:
                return self.fork_messages.authorize_message(role, fork_message_id)
            elif scheduled:
                delivery = self.db.execute("""
                    SELECT d.status FROM scheduled_turns s
                    JOIN message_deliveries d USING (event_id, session_id, purpose)
                    WHERE s.turn_id = ?
                """, (turn.id,)).fetchone()
                if delivery is None or delivery["status"] != "running":
                    raise StaleTurn

        try:
            checkpoint = self.journal.checkpoint(turn.id)
            if checkpoint["phase"] == GOAL_CONFIRMATION_PHASE:
                await self._confirm_goal_close(role, turn)
                return
            instructions = prompt if prompt is not None else ROLES[role]
            history_scope = None
            goal_id = None
            if scheduled:
                instructions += DELIVERY_PROMPT
                delivery = self.db.execute("""
                    SELECT d.* FROM scheduled_turns s JOIN message_deliveries d USING (event_id, session_id, purpose)
                    WHERE s.turn_id = ?
                """, (turn.id,)).fetchone()
                fork_message_id = self.collaboration.fork_message_for_delivery(delivery)
                message = json.loads(checkpoint["history"][0]["content"])["message"]
                actor = self.workspace_tools.actor(role)
                goal_id, history_scope = self.collaboration.goal_scope_for_message(actor.team_id, message["id"])
                if history_scope is None and message["kind"] == "assignment":
                    history_scope = {message["id"]}
            version_ids = check_scope()
            if is_fork:
                instructions += (
                    f"\n\nYou are the restricted assistant {role}. "
                    "Consider the explicitly shared history and relay results using send_fork_message with assistant_id "
                    "equal to your session ID when the tool is available. Respect the assigned scope."
                )
                prior_history = self.forks.model_history(role, turn.id)
            else:
                prior_history = self._history(role, history_scope)
            prior = [{"role": "system", "content": instructions}, *prior_history]
            current, step, phase = (checkpoint[key] for key in ("history", "step", "phase"))
            if version_ids is not None:
                payload = json.loads(current[0]["content"])
                self.fork_messages.add_delivery_versions(payload, fork_message_id, version_ids)
                current[0]["content"] = json.dumps(payload, ensure_ascii=False)
            extra_steps = self.db.execute("""
                SELECT COALESCE(SUM(CAST(json_extract(payload, '$.extra_steps') AS INTEGER)), 0)
                FROM events WHERE turn_id = ? AND type = 'model.retry_requested'
            """, (turn.id,)).fetchone()[0]
            while phase == "tools" or step < self.max_steps + extra_steps:
                self._check_generation(turn)
                check_scope()
                if phase == "tools":
                    pause = None
                    for position, call in enumerate(current[-1].get("tool_calls") or []):
                        if fork_message_id is not None:
                            check_scope()
                        result, pause = await self._apply_tool(
                            role, turn, step, position, call, workspace_id, scheduled=scheduled, pause=pause,
                        )
                        if call["function"]["name"] == "close_goal" and self.journal.confirming_goal(turn.id):
                            await self._confirm_goal_close(role, turn)
                            return
                        current.append({"role": "tool", "tool_call_id": call["id"],
                                        "content": json.dumps(result, ensure_ascii=False)})
                    phase = "model"
                    with self.db:
                        self._advance(turn, step, phase, current)
                        if pause:
                            self._write_finish(turn.id, pause, history=current)
                            return
                    continue

                tools = self.workspace_tools.tools_for(role) if workspace_id is not None else TOOLS
                if workspace_id is not None and self.collaboration is not None:
                    tools.extend(self.collaboration.tools_for(role))
                if workspace_id is not None and self.experience_tools is not None:
                    tools.extend(self.experience_tools.tools_for(role))
                if workspace_id is not None and self.git_approvals is not None:
                    tools.extend(self.git_approvals.tools_for(role, scheduled))
                if workspace_id is not None and self.forks is not None:
                    tools.extend(self.forks.tools_for(role))
                    tools.extend(self.fork_messages.tools_for(role))
                model_identity = getattr(self.model, "identity", {"mode": "injected", "endpoint": None, "model": None})
                with self.db:
                    for new_text in self.journal.drain_inputs(turn.id, turn.generation):
                        self._event(turn.id, "turn.input_applied", text=new_text)
                        current.append({"role": "user", "content": new_text})
                    self._advance(turn, step, phase, current)
                    self._event(turn.id, "model.started", step=step + 1, generation=turn.generation, **model_identity)
                if scheduled and goal_id is None:
                    goal_id = self.collaboration.goal_for_message(actor.team_id, message["id"])
                assistant = await self._complete_model(role, turn.id, goal_id, prior + current, tools)
                self._check_generation(turn)
                check_scope()
                if not is_fork and fork_message_id is None and workspace_id is not None:
                    self.workspace_tools.actor(role)
                current.append(assistant)
                count = len(assistant.get("tool_calls") or [])
                if count > 8:
                    raise ModelError("Model returned too many tools in one decision")
                step += 1
                phase = "tools" if count else "model"
                with self.db:
                    self._event(turn.id, "model.completed", step=step, generation=turn.generation, tool_count=count)
                    self._advance(turn, step, phase, current)
                    if not count and not self.journal.checkpoint(turn.id)["pending"]:
                        reply = assistant.get("content")
                        if not reply or not reply.strip():
                            raise ModelError("Model returned no reply and no tool calls")
                        self._write_finish(turn.id, "completed", reply=reply, history=current)
                        return
            with self.db:
                if self.journal.current(turn.id, turn.generation):
                    self._event(turn.id, "model.step_limit", generation=turn.generation, step=step)
                    self._write_finish(turn.id, "failed", error="Turn exceeded its model step limit")
        except StaleTurn:
            return
        except UnknownToolOutcome as exc:
            if self.journal.current(turn.id, turn.generation):
                self._finish(turn.id, "needs_reconciliation", error=str(exc))
        except ModelPaused as exc:
            with self.db:
                if self.journal.current(turn.id, turn.generation):
                    self._event(turn.id, "model.paused", generation=turn.generation, error=str(exc))
                    self._write_finish(turn.id, "recoverable" if scheduled or is_fork else "failed", error=str(exc))
        except ModelError as exc:
            with self.db:
                if self.journal.current(turn.id, turn.generation):
                    self._event(turn.id, "model.failed", generation=turn.generation, error=str(exc),
                                transient=isinstance(exc, ModelUnavailable))
                    self._write_finish(turn.id, "failed", error=str(exc))
        except (WorkspaceNotFound, CollaborationInvalid):
            if self.journal.current(turn.id, turn.generation):
                self._finish(turn.id, "interrupted", error="Execution workspace is no longer available")
        except asyncio.CancelledError:
            if self.journal.current(turn.id, turn.generation):
                self._finish(turn.id, "recoverable" if scheduled or is_fork else "interrupted", error="Server stopped")
            raise
        except Exception:
            logger.exception("Runtime failed for turn %s", turn.id)
            if self.journal.current(turn.id, turn.generation):
                self._finish(turn.id, "failed", error="Unexpected runtime error")
        finally:
            if self.active.get(role) is turn:
                self.active.pop(role)

    async def _apply_tool(self, role, turn, step, position, call, workspace_id, *, scheduled, pause):
        self._check_generation(turn)
        name = call["function"]["name"]
        call_id = call["id"]
        arguments = call["function"]["arguments"]
        receipt = self.journal.receipt(turn.id, step, position)
        if receipt and receipt["state"] == "completed":
            result = receipt["result"]
            return result, pause or self._tool_pause(name, result, scheduled)
        if receipt and name not in REPLAYABLE_TOOLS:
            raise UnknownToolOutcome(f"Check the outcome of {name} ({call_id}) before retrying this work")

        pending = self.journal.checkpoint(turn.id)["pending"]
        skip_reason = ("Work is waiting for human input." if pause else
                       "Input changed. Decide again before using this tool." if pending and receipt is None else None)
        if workspace_id is not None:
            self.workspace_tools.actor(role)
        with self.db:
            if not self.journal.start_tool(turn.id, turn.generation, step, position, call_id, name, arguments):
                raise StaleTurn
            if skip_reason is None:
                self._event(turn.id, "tool.started", name=name, tool_call_id=call_id, generation=turn.generation)
            else:
                result = {"ok": False, "error": skip_reason}
                self._complete_tool(turn, step, position, name, call_id, result, "tool.skipped")
                return result, pause
        try:
            close_call = CloseGoalCall(turn, step, position, call_id) if name == "close_goal" else None
            result = await self._execute_tool(role, turn, call_id, name, arguments, workspace_id, scheduled,
                                              close_call=close_call)
        except (WorkspaceNotFound, asyncio.CancelledError):
            raise
        except Exception as exc:
            if name not in REPLAYABLE_TOOLS:
                raise UnknownToolOutcome(f"Check the outcome of {name} ({call_id}) before retrying this work") from exc
            raise
        if close_call is None or not self.journal.confirming_goal(turn.id):
            with self.db:
                self._complete_tool(turn, step, position, name, call_id, result, "tool.completed")
        return result, self._tool_pause(name, result, scheduled)

    def record_goal_close(self, call: CloseGoalCall, receipt: dict):
        """Called inside the goal terminal transaction."""
        turn = call.turn
        if not self.journal.begin_goal_confirmation(turn.id, turn.generation, call.step):
            raise StaleTurn
        self._complete_tool(turn, call.step, call.position, "close_goal", call.call_id,
                            {"ok": True, "result": receipt}, "tool.completed")

    async def _confirm_goal_close(self, role, turn):
        self._check_generation(turn)
        checkpoint = self.journal.checkpoint(turn.id)
        history, step = checkpoint["history"], checkpoint["step"]
        decision = history[-1]
        closing_pair = None
        with self.db:
            for position, call in enumerate(decision["tool_calls"]):
                call_id = call["id"]
                name, arguments = (call["function"][key] for key in ("name", "arguments"))
                receipt = self.journal.receipt(turn.id, step, position)
                if receipt is not None and receipt["state"] == "completed":
                    result = receipt["result"]
                else:
                    result = {"ok": False, "error": "This goal has ended. No further tools may run in this turn."}
                    if not self.journal.start_tool(turn.id, turn.generation, step, position, call_id, name, arguments):
                        raise StaleTurn
                    self._complete_tool(turn, step, position, name, call_id, result, "tool.skipped")
                message = {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)}
                history.append(message)
                if name == "close_goal" and result["ok"]:
                    goal_id = result["result"]["goal_id"]
                    closing_pair = [{"role": "assistant", "content": None, "tool_calls": [call]}, message]
            if closing_pair is None:
                raise ModelError("The closed goal has no recorded tool receipt")
            identity = getattr(self.model, "identity", {"mode": "injected", "endpoint": None, "model": None})
            self._event(turn.id, "model.started", step=step + 1, generation=turn.generation, **identity)

        # The closed work scope grants a confirmation only, without another tool decision.
        response = await self._complete_model(role, turn.id, goal_id, [
            {"role": "system", "content": "The goal has ended. Summarize the recorded close_goal result "
                                           "in a short final reply. No further actions are available."},
            {"role": "user", "content": "Confirm the goal's recorded terminal outcome."},
            *closing_pair,
        ], [])
        self._check_generation(turn)
        text = response.get("content")
        if response.get("tool_calls") or not text or not text.strip():
            raise ModelError("The model did not provide a text confirmation of the closed goal")
        with self.db:
            self._event(turn.id, "model.completed", step=step + 1, generation=turn.generation, tool_count=0)
            self._write_finish(turn.id, "completed", reply=text, history=[*history, response])

    def _complete_tool(self, turn, step, position, name, call_id, result, outcome):
        if not self.journal.complete_tool(turn.id, turn.generation, step, position, result):
            raise StaleTurn
        self._event(turn.id, outcome, name=name, tool_call_id=call_id, generation=turn.generation, result=result)

    def _tool_pause(self, name, result, scheduled):
        if name == "ask_human" and result["ok"]:
            if scheduled or self.collaboration.question_waiting(result["result"]["question"]["question_id"]):
                return "waiting_human"
        if name == "request_push" and result["ok"] and result["result"]["pause"]:
            return "waiting_approval"
        return None

    async def _execute_tool(self, role, turn, call_id, name, arguments, workspace_id, scheduled, *, close_call=None):
        if workspace_id is not None and self.forks is not None:
            if name == "fork_session":
                return await self.forks.execute_create(role, arguments)
            if name in {"send_fork_message", "decide_fork_result"}:
                return self.fork_messages.execute(role, name, arguments)
        if workspace_id is not None and self.git_approvals is not None and self.git_approvals.handles(name):
            return await self.git_approvals.execute(role, turn.id, turn.generation, call_id, name, arguments)
        if workspace_id is not None and self.experience_tools is not None and self.experience_tools.handles(name):
            return self.experience_tools.execute(role, name, arguments)
        if workspace_id is not None and self.collaboration is not None and self.collaboration.handles(name):
            work = None
            if scheduled and name == "ask_human":
                work = self.db.execute("""
                    SELECT d.message_id FROM scheduled_turns s JOIN message_deliveries d
                        USING (event_id, session_id, purpose) WHERE s.turn_id = ?
                """, (turn.id,)).fetchone()["message_id"]
            return self.collaboration.execute_tool(role, name, arguments, work_message_id=work, close_call=close_call)
        if workspace_id is not None:
            return await self.workspace_tools.execute(role, name, arguments)
        return await asyncio.to_thread(self._dispatch, role, name, arguments)

    def _dispatch(self, role: str, name: str, raw_arguments: str) -> dict:
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError, RecursionError):
            return {"ok": False, "error": "Tool arguments must be valid JSON"}
        if (
            name not in TOOL_PARAMETERS
            or not isinstance(arguments, dict)
            or set(arguments) != TOOL_PARAMETERS[name]
            or any(not isinstance(value, str) or not value.strip() for value in arguments.values())
        ):
            return {"ok": False, "error": "Unknown tool or invalid arguments"}
        try:
            if name == "run_command":
                data = self.sandbox.run(role, arguments["command"])
            elif name == "publish_artifact":
                data = self.sandbox.publish(role, arguments["path"], arguments["recipient"])
            else:
                data = self.sandbox.list_artifacts(role)
            return {"ok": True, "result": data}
        except SandboxError as exc:
            return {"ok": False, "error": str(exc)}

    async def close(self):
        self.closing = True
        tasks = [turn.task for turn in self.active.values() if turn.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()
