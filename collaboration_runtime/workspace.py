"""Workspace authorization and tool handlers shared by turns and artifact views."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .artifacts import ArtifactError, ArtifactNotFound, ArtifactStore
from .sandbox import SandboxError
from .validation_feedback import validation_feedback
from .search import SearchError
from .source_fetch import SourceHTTPError, fetch_public_source


class WorkspaceNotFound(Exception):
    """The execution identity or resource is not accessible."""


class WorkspaceConflict(Exception):
    """An execution identity is already registered with a different scope."""


@dataclass(frozen=True)
class Actor:
    team_id: str
    session_id: str
    workspace_id: str
    prompt: str
    parent_session_id: str | None = None
    scope_id: str | None = None
    versions: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()


ASSISTANT_DRAFT_TOOLS = frozenset({"create_proposal", "counter_proposal"})
ASSISTANT_FORK_TOOLS = frozenset({"send_fork_message"})
ASSISTANT_EXPERIENCE_TOOLS = frozenset({"retrieve_lessons", "read_lesson", "read_handoff"})


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Identifier = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)]


class PathArgs(ToolArgs):
    path: Text


class WriteArgs(PathArgs):
    content: str


class CommandArgs(ToolArgs):
    command: Text
    artifact_version_ids: list[Identifier] = Field(default_factory=list, max_length=16)
    measure_timings: bool = False


class PublishArgs(PathArgs):
    reader_session_ids: list[Identifier] = Field(max_length=32)
    artifact_id: Identifier | None = None


class TextWindowArgs(ToolArgs):
    start_char: int = Field(default=0, ge=0)
    max_chars: int | None = Field(default=None, ge=1, le=40_000)


class ReadArtifactArgs(TextWindowArgs):
    version_id: Identifier


class SearchArgs(ToolArgs):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    limit: int = Field(default=5, ge=1, le=10)


class SearchResultArgs(ToolArgs):
    search_id: Identifier


class SourceArgs(SearchResultArgs, TextWindowArgs):
    url: Text | None = None
    result_id: int | None = Field(default=None, ge=1, strict=True)


class SourceRecordArgs(TextWindowArgs):
    source_id: Identifier


TOOLS = {
    "read_file": (PathArgs, "Read a UTF-8 file from your private workspace."),
    "create_file": (WriteArgs, "Create a UTF-8 file in your private workspace."),
    "update_file": (WriteArgs, "Replace an existing UTF-8 file in your private workspace."),
    "delete_file": (PathArgs, "Delete a regular file in your private workspace."),
    "run_command": (CommandArgs, "Run a command or tests in your workspace. Pass exact artifact version IDs to import them read-only. "
                    "The sandbox has no external network. Use Python urllib.request for local HTTP tests. "
                    "Set measure_timings only when diagnosing sandbox latency."),
    "list_workspace_readers": (ToolArgs, "List eligible participants for your artifact publication."),
    "publish_artifact": (PublishArgs, "Publish a file snapshot to explicitly named readers. An artifact ID publishes the next version."),
    "list_artifacts": (ToolArgs, "List artifact versions you are authorized to read."),
    "read_artifact": (ReadArtifactArgs, "Read an authorized UTF-8 version and get its read-only mount path. "
                      "Use max_chars and start_char for an excerpt."),
    "search_web": (SearchArgs, "Search the web and save URLs and excerpts. Check the provider and any fallback reason."),
    "get_search_result": (SearchResultArgs, "Retrieve a search record made by this execution session."),
    "read_source": (SourceArgs, "Record evidence from a search result by result_id (1-based) or exact URL. "
                    "Supports accessible HTML, text and text-based PDFs. If a site rejects access, use a different "
                    "public result. Search excerpts are leads. max_chars limits the returned excerpt."),
    "get_source": (SourceRecordArgs, "Retrieve recorded source text and provenance. Use max_chars and start_char for an excerpt."),
}


def _text_window(record, field, args):
    if args.start_char == 0 and args.max_chars is None:
        return record
    value = record[field]
    start = min(args.start_char, len(value))
    end = len(value) if args.max_chars is None else min(start + args.max_chars, len(value))
    return {**record, field: value[start:end], "text_range": {
        "start_char": start, "end_char": end, "total_chars": len(value),
    }}


class WorkspaceService:
    def __init__(self, db, teams, sandbox, root: Path, search=None):
        self.db = db
        self.teams = teams
        self.sandbox = sandbox
        self.artifacts = ArtifactStore(db, root)
        self.search = search
        self.assistant_access = None
        self.assistant_readers = None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS assistant_workspaces (
                session_id TEXT PRIMARY KEY,
                details TEXT NOT NULL,
                enabled INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_searches (
                search_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                scope_id TEXT,
                query TEXT NOT NULL,
                results TEXT NOT NULL,
                upstream_status TEXT NOT NULL DEFAULT 'ok',
                provider TEXT,
                fallback_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS workspace_sources (
                source_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, session_id TEXT NOT NULL,
                scope_id TEXT, data TEXT NOT NULL
            );
        """)
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(workspace_searches)')}
        if 'upstream_status' not in columns:
            self.db.execute("ALTER TABLE workspace_searches ADD COLUMN upstream_status TEXT NOT NULL DEFAULT 'ok'")
        if 'provider' not in columns:
            self.db.execute("ALTER TABLE workspace_searches ADD COLUMN provider TEXT")
        if 'fallback_reason' not in columns:
            self.db.execute("ALTER TABLE workspace_searches ADD COLUMN fallback_reason TEXT")

    def _member_actor(self, session_id: str) -> Actor | None:
        member = self.teams.session(session_id)
        if member is None:
            return None
        return Actor(member["team_id"], session_id, member["workspace_id"], member["prompt"])

    def actor(self, session_id: str) -> Actor:
        member = self._member_actor(session_id)
        if member is not None:
            return member
        row = self.db.execute(
            "SELECT details FROM assistant_workspaces WHERE session_id = ? AND enabled = 1",
            (session_id,),
        ).fetchone()
        if row is None:
            raise WorkspaceNotFound("Execution session not found")
        details = json.loads(row["details"])
        parent = self._member_actor(details["parent_session_id"])
        if parent is None or parent.team_id != details["team_id"]:
            raise WorkspaceNotFound("Execution session not found")
        if self.assistant_access is not None:
            self.assistant_access(session_id)
        return Actor(**details)

    def assistant_details(
        self, *, parent_session_id: str, session_id: str, scope_id: str, prompt: str,
        version_ids: list[str], allowed_tools: list[str],
    ) -> dict:
        parent = self._member_actor(parent_session_id)
        if parent is None or self._member_actor(session_id) is not None:
            raise WorkspaceNotFound("Parent or assistant identity is unavailable")
        if UUID(session_id).hex != session_id or not scope_id.strip() or not prompt.strip():
            raise ValueError("A stable assistant ID, scope and prompt are required")
        if not set(allowed_tools) <= (
            TOOLS.keys() | ASSISTANT_DRAFT_TOOLS | ASSISTANT_FORK_TOOLS | ASSISTANT_EXPERIENCE_TOOLS
        ):
            raise ValueError("Assistant tools must be registered workspace tools")
        for version_id in version_ids:
            self.artifacts.resolve(parent.team_id, parent.session_id, version_id)
        return {
            "team_id": parent.team_id, "session_id": session_id,
            "workspace_id": f"ws-{uuid5(UUID(session_id), 'assistant-workspace').hex}",
            "prompt": prompt, "parent_session_id": parent_session_id, "scope_id": scope_id,
            "versions": sorted(set(version_ids)), "allowed_tools": sorted(set(allowed_tools)),
        }

    def install_assistant(self, details):
        session_id = details["session_id"]
        serialized = json.dumps(details, ensure_ascii=False, sort_keys=True)
        self.db.execute("INSERT OR IGNORE INTO assistant_workspaces VALUES (?, ?, 1)", (session_id, serialized))
        existing = self.db.execute(
            "SELECT details, enabled FROM assistant_workspaces WHERE session_id = ?", (session_id,),
        ).fetchone()
        if existing["details"] != serialized or not existing["enabled"]:
            raise WorkspaceConflict("Assistant identity already has a different or revoked scope")

    async def register_assistant(
        self, *, parent_session_id: str, session_id: str, scope_id: str, prompt: str,
        version_ids: list[str], allowed_tools: list[str],
    ) -> Actor:
        """Prepare a helper workspace before recording its scope."""
        details = self.assistant_details(parent_session_id=parent_session_id, session_id=session_id,
                                         scope_id=scope_id, prompt=prompt, version_ids=version_ids,
                                         allowed_tools=allowed_tools)
        await asyncio.to_thread(self.sandbox.prepare, details["workspace_id"])
        with self.db:
            self.install_assistant(details)
        return self.actor(session_id)

    def revoke_assistant(self, session_id: str):
        with self.db:
            self.db.execute("UPDATE assistant_workspaces SET enabled = 0 WHERE session_id = ?", (session_id,))

    @staticmethod
    def _has_tool(actor: Actor, name: str) -> bool:
        return actor.parent_session_id is None or name in actor.allowed_tools

    def tools_for(self, session_id: str) -> list[dict]:
        actor = self.actor(session_id)
        return [
            {"type": "function", "function": {
                "name": name, "description": description, "parameters": args.model_json_schema(),
            }}
            for name, (args, description) in TOOLS.items() if self._has_tool(actor, name)
        ]

    @staticmethod
    def _artifact_view(metadata: dict) -> dict:
        return {**metadata, "path": f"/inputs/{metadata['version_id']}/artifact"}

    def _resolve(self, actor: Actor, version_id: str) -> tuple[dict, Path]:
        if actor.parent_session_id is None:
            return self.artifacts.resolve(actor.team_id, actor.session_id, version_id)
        if version_id in actor.versions:
            return self.artifacts.resolve(actor.team_id, actor.parent_session_id, version_id)
        if any(item["version_id"] == version_id for item in self._own_versions(actor)):
            return self.artifacts.resolve(actor.team_id, actor.session_id, version_id)
        raise ArtifactNotFound("Artifact version not found")

    def _own_versions(self, actor: Actor) -> list[dict]:
        return [
            item for item in self.artifacts.list_for(actor.team_id, actor.session_id)
            if item["author_session_id"] == actor.session_id
        ]

    def resolve_number(self, session_id: str, artifact_id: str, version: int) -> tuple[dict, Path]:
        actor = self.actor(session_id)
        if not self._has_tool(actor, "read_artifact"):
            raise ArtifactNotFound("Artifact version not found")
        if actor.parent_session_id is None:
            return self.artifacts.resolve_number(actor.team_id, session_id, artifact_id, version)
        inherited = [
            item for item in self.artifacts.list_for(actor.team_id, actor.parent_session_id)
            if item["version_id"] in actor.versions
        ]
        for item in [*self._own_versions(actor), *inherited]:
            if item["artifact_id"] == artifact_id and item["version"] == version:
                return self._resolve(actor, item["version_id"])
        raise ArtifactNotFound("Artifact version not found")

    def _readers(self, actor: Actor) -> list[dict]:
        allowed = {actor.parent_session_id}
        if actor.parent_session_id is not None and self.assistant_readers is not None:
            allowed.update(self.assistant_readers(actor.session_id))
        return [
            {key: member[key] for key in ("session_id", "role_id", "name")}
            for member in self.teams.get(actor.team_id)["members"]
            if member["session_id"] != actor.session_id
            and (actor.parent_session_id is None or member["session_id"] in allowed)
        ]

    async def execute(self, session_id: str, name: str, raw_arguments: str) -> dict:
        try:
            actor = self.actor(session_id)
            if name not in TOOLS or not self._has_tool(actor, name):
                return {"ok": False, "error": "Tool not available for this execution session"}
            args = TOOLS[name][0].model_validate_json(raw_arguments)
            result = await self._execute(actor, name, args)
            self.actor(session_id)
            return {"ok": True, "result": result}
        except ValidationError as exc:
            if name == "read_source":
                error = "Invalid read_source arguments: provide search_id and a positive integer result_id or exact URL"
            else:
                error = "Invalid tool arguments"
            return {"ok": False, "error": error, "errors": validation_feedback(exc, TOOLS[name][0])}
        except UnicodeError:
            return {"ok": False, "error": "This tool requires UTF-8 text; use a read-only artifact mount for binary files"}
        except ArtifactNotFound as exc:
            if name in {"read_artifact", "run_command"}:
                return {"ok": False, "error": (
                    "Artifact version not found. Check the ID with list_artifacts. Ask an authorized publisher "
                    "to use publish_artifact with your session in reader_session_ids and send you the new version_id. "
                    "If you are an assistant, ask your parent to hand the version off to you."
                )}
            return {"ok": False, "error": str(exc)}
        except SourceHTTPError as exc:
            result = {"ok": False, "error": str(exc), "status": exc.status_code}
            if exc.status_code in {202, 401, 403, 404, 406, 410}:
                result["next_action"] = (
                    "Try a different accessible public source with read_source. Use a search excerpt as a lead "
                    "and mark any claim you cannot verify."
                )
            return result
        except (WorkspaceNotFound, ArtifactError, SearchError, SandboxError) as exc:
            return {"ok": False, "error": str(exc)}

    async def _execute(self, actor: Actor, name: str, args):
        workspace = actor.workspace_id
        if name == "read_file":
            content = await asyncio.to_thread(self.sandbox.read_bytes, workspace, args.path)
            return {"path": args.path, "content": content.decode("utf-8")}
        if name in {"create_file", "update_file"}:
            mode = "create" if name == "create_file" else "update"
            await asyncio.to_thread(self.sandbox.write_bytes, workspace, args.path, args.content.encode("utf-8"), mode=mode)
            return {"path": args.path}
        if name == "delete_file":
            await asyncio.to_thread(self.sandbox.delete_file, workspace, args.path)
            return {"path": args.path}
        if name == "run_command":
            inputs = {version_id: self._resolve(actor, version_id)[1] for version_id in args.artifact_version_ids}
            timing = {"measure_timings": True} if args.measure_timings else {}
            return await asyncio.to_thread(self.sandbox.run, workspace, args.command, inputs=inputs, **timing)
        if name == "list_workspace_readers":
            return self._readers(actor)
        if name == "publish_artifact":
            eligible = {reader["session_id"] for reader in self._readers(actor)}
            if not set(args.reader_session_ids) <= eligible:
                raise WorkspaceNotFound("Artifact reader not found in this execution scope")
            data = await asyncio.to_thread(self.sandbox.read_bytes, workspace, args.path)
            self.actor(actor.session_id)
            return self._artifact_view(self.artifacts.publish(
                team_id=actor.team_id, author_session_id=actor.session_id,
                source_path=args.path, content=data, reader_session_ids=args.reader_session_ids,
                artifact_id=args.artifact_id, scope_id=actor.scope_id,
            ))
        if name == "list_artifacts":
            versions = (
                self.artifacts.list_for(actor.team_id, actor.session_id)
                if actor.parent_session_id is None else self._own_versions(actor)
            )
            for version_id in actor.versions:
                try:
                    versions.append(self._resolve(actor, version_id)[0])
                except ArtifactNotFound:
                    continue
            return list({item["version_id"]: self._artifact_view(item) for item in versions}.values())
        if name == "read_artifact":
            metadata, path = self._resolve(actor, args.version_id)
            record = {**self._artifact_view(metadata), "content": path.read_text(encoding="utf-8")}
            return _text_window(record, "content", args)
        if name == "search_web":
            return await self._search(actor, args.query, args.limit)
        if name == "get_search_result":
            return self._search_record(actor, args.search_id)
        if name == "read_source":
            search = self._search_record(actor, args.search_id)
            if (args.url is None) == (args.result_id is None):
                raise WorkspaceNotFound('Provide exactly one of result_id or url')
            if args.result_id is not None:
                if args.result_id > len(search['results']):
                    raise WorkspaceNotFound('result_id out of bounds for this search record')
                url = search['results'][args.result_id - 1]['url']
            else:
                url = args.url
                if url not in [item['url'] for item in search['results']]:
                    raise WorkspaceNotFound('URL not found in this search record')
            result = await asyncio.to_thread(fetch_public_source, url)
            self.actor(actor.session_id)
            result.update(source_id=uuid4().hex, search_id=args.search_id,
                          retrieved_at=datetime.now(timezone.utc).isoformat())
            with self.db:
                self.db.execute('INSERT INTO workspace_sources VALUES (?, ?, ?, ?, ?)',
                    (result['source_id'], actor.team_id, actor.session_id, actor.scope_id, json.dumps(result, ensure_ascii=False)))
            return _text_window(result, "text", args)
        row = self.db.execute('SELECT data FROM workspace_sources WHERE source_id = ? AND team_id = ? AND session_id = ? AND scope_id IS ?',
                              (args.source_id, actor.team_id, actor.session_id, actor.scope_id)).fetchone()
        if row is None:
            raise WorkspaceNotFound('Source record not found')
        return _text_window(json.loads(row['data']), "text", args)

    async def _search(self, actor: Actor, query: str, limit: int):
        if self.search is None:
            raise SearchError("Search is not configured on the host. Set EXA_API_KEY or COURSE_SEARCH_URL and restart the host.")
        results = await self.search.search(query, limit)
        self.actor(actor.session_id)
        search_id = uuid4().hex
        upstream_status = "degraded" if getattr(results, "degraded", False) else "ok"
        provider = getattr(results, "provider", None)
        fallback_reason = getattr(results, "fallback_reason", None)
        with self.db:
            self.db.execute(
                """INSERT INTO workspace_searches
                   (search_id, team_id, session_id, scope_id, query, results, upstream_status, provider, fallback_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (search_id, actor.team_id, actor.session_id, actor.scope_id, query,
                 json.dumps(results, ensure_ascii=False), upstream_status, provider, fallback_reason),
            )
        return {"search_id": search_id, "query": query, "results": [{**item, "result_id": i} for i, item in enumerate(results, 1)],
                "upstream_status": upstream_status, "provider": provider, "fallback_reason": fallback_reason}

    def _search_record(self, actor: Actor, search_id: str):
        row = self.db.execute(
            """SELECT query, results, upstream_status, provider, fallback_reason FROM workspace_searches
               WHERE search_id = ? AND team_id = ? AND session_id = ? AND scope_id IS ?""",
            (search_id, actor.team_id, actor.session_id, actor.scope_id),
        ).fetchone()
        if row is None:
            raise WorkspaceNotFound("Search record not found")
        return {"search_id": search_id, "query": row["query"], "results": [{**item, "result_id": i} for i, item in enumerate(json.loads(row["results"]), 1)],
                "upstream_status": row["upstream_status"], "provider": row["provider"],
                "fallback_reason": row["fallback_reason"]}
