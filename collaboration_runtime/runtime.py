"""Persisted role sessions and the model/tool loop for the two-role demo."""

import asyncio
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import sqlite3
from uuid import uuid4

from .model import ModelError
from .sandbox import SandboxError

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


@dataclass
class ActiveTurn:
    id: str
    pending: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None


class Runtime:
    def __init__(self, model, sandbox, database: Path, max_steps: int = 12):
        self.model = model
        self.sandbox = sandbox
        self.max_steps = max_steps
        self.active: dict[str, ActiveTurn] = {}
        self.closing = False
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
        """)
        for row in self.db.execute("SELECT id FROM turns WHERE status = 'running'").fetchall():
            self._finish(row["id"], "interrupted", error="Server restarted during the turn")

    def _event(self, turn_id: str, event_type: str, **payload):
        self.db.execute(
            "INSERT INTO events(turn_id, type, payload) VALUES (?, ?, ?)",
            (turn_id, event_type, json.dumps(payload, ensure_ascii=False)),
        )

    def _finish(self, turn_id: str, status: str, *, reply=None, error=None, history=None):
        with self.db:
            self.db.execute(
                "UPDATE turns SET status = ?, reply = ?, error = ?, history = ? WHERE id = ?",
                (status, reply, error, json.dumps(history, ensure_ascii=False) if history else None, turn_id),
            )
            self._event(turn_id, f"turn.{status}", reply=reply, error=error)

    def start(self, role: str, text: str, *, prompt: str | None = None, workspace_id: str | None = None) -> str:
        if self.closing or role in self.active:
            raise TurnConflict("The session already has a running turn or is shutting down")
        turn = ActiveTurn(uuid4().hex)
        with self.db:
            self.db.execute("INSERT INTO turns(id, role, status) VALUES (?, ?, 'running')", (turn.id, role))
            self._event(turn.id, "turn.started", text=text)
        self.active[role] = turn
        turn.task = asyncio.create_task(self._run(role, turn, text, prompt, workspace_id))
        return turn.id

    def steer(self, role: str, turn_id: str, text: str):
        turn = self.active.get(role)
        if self.closing or turn is None or turn.id != turn_id:
            raise TurnConflict("This is no longer the running turn")
        if len(turn.pending) >= 16:
            raise TurnConflict("The turn already has too many pending inputs")
        with self.db:
            self._event(turn_id, "turn.input_queued", text=text)
        turn.pending.append(text)

    def get_turn(self, role: str, turn_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT id AS turn_id, role, status, reply, error FROM turns WHERE role = ? AND id = ?",
            (role, turn_id),
        ).fetchone()
        return dict(row) if row else None

    def events(self, turn_id: str, after: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT seq, type, payload FROM events WHERE turn_id = ? AND seq > ? ORDER BY seq LIMIT 100",
            (turn_id, after),
        )
        return [
            {"seq": row["seq"], "type": row["type"], **json.loads(row["payload"])}
            for row in rows
        ]

    def _history(self, role: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT history FROM turns WHERE role = ? AND status = 'completed' ORDER BY rowid",
            (role,),
        )
        return [message for row in rows for message in json.loads(row["history"])]

    async def _run(self, role: str, turn: ActiveTurn, text: str, prompt, workspace_id):
        try:
            prior = [{"role": "system", "content": prompt if prompt is not None else ROLES[role]}, *self._history(role)]
            current = [{"role": "user", "content": text}]
            tools = [WORKSPACE_TOOL] if workspace_id is not None else TOOLS
            for step in range(1, self.max_steps + 1):
                if self.closing:
                    self._finish(turn.id, "interrupted", error="Server is shutting down")
                    return
                with self.db:
                    for new_text in turn.pending:
                        self._event(turn.id, "turn.input_applied", text=new_text)
                        current.append({"role": "user", "content": new_text})
                    self._event(turn.id, "model.started", step=step)
                turn.pending.clear()
                assistant = await self.model.complete(prior + current, tools)
                calls = assistant.get("tool_calls") or []
                if len(calls) > 8:
                    raise ModelError("Model returned too many tools in one decision")
                with self.db:
                    self._event(turn.id, "model.completed", step=step, tool_count=len(calls))
                current.append(assistant)
                if calls:
                    for call in calls:
                        await self._apply_tool(role, turn, call, current, workspace_id)
                    continue
                reply = assistant.get("content")
                if turn.pending:
                    continue
                if not reply or not reply.strip():
                    raise ModelError("Model returned no reply and no tool calls")
                self._finish(turn.id, "completed", reply=reply, history=current)
                return
            self._finish(turn.id, "failed", error="Turn exceeded its model step limit")
        except ModelError as exc:
            self._finish(turn.id, "failed", error=str(exc))
        except asyncio.CancelledError:
            self._finish(turn.id, "interrupted", error="Turn was cancelled")
            raise
        except Exception:
            logger.exception("Runtime failed for turn %s", turn.id)
            self._finish(turn.id, "failed", error="Unexpected runtime error")
        finally:
            self.active.pop(role, None)

    async def _apply_tool(self, role: str, turn: ActiveTurn, call: dict, history: list[dict], workspace_id):
        name = call["function"]["name"]
        call_id = call["id"]
        if turn.pending or self.closing:
            result = {"ok": False, "error": "Input changed. Decide again before using this tool."}
            outcome = "tool.skipped"
        else:
            with self.db:
                self._event(turn.id, "tool.started", name=name, tool_call_id=call_id)
            result = await asyncio.to_thread(
                self._dispatch, role, name, call["function"]["arguments"], workspace_id,
            )
            outcome = "tool.completed"
        history.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        })
        with self.db:
            self._event(turn.id, outcome, name=name, tool_call_id=call_id, result=result)

    def _dispatch(self, role: str, name: str, raw_arguments: str, workspace_id: str | None = None) -> dict:
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError, RecursionError):
            return {"ok": False, "error": "Tool arguments must be valid JSON"}
        if (
            name not in TOOL_PARAMETERS
            or (workspace_id is not None and name != "run_command")
            or not isinstance(arguments, dict)
            or set(arguments) != TOOL_PARAMETERS[name]
            or any(not isinstance(value, str) or not value.strip() for value in arguments.values())
        ):
            return {"ok": False, "error": "Unknown tool or invalid arguments"}
        try:
            if name == "run_command":
                data = self.sandbox.run(workspace_id or role, arguments["command"])
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
        await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()
