"""Run with uvicorn bound to loopback and a single worker."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from typing import Annotated

from .artifacts import ArtifactError, ArtifactNotFound
from .model import CourseModel
from .roles import ROLE_ID_PATTERN, RoleDefinition, RoleFileError
from .runtime import ROLES, Runtime, TurnConflict
from .sandbox import DockerSandbox
from .search import SearchClient
from .teams import TeamConflict, TeamNotFound, TeamService, TeamUnavailable
from .workspace import WorkspaceNotFound, WorkspaceService

RequestId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class TurnInput(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)]


class TeamInput(BaseModel):
    request_id: RequestId
    roster: Annotated[str, StringConstraints(min_length=1, max_length=250_000)]
    name: DisplayName | None = None


class MemberInput(BaseModel):
    request_id: RequestId
    expected_version: Annotated[int, Field(ge=1)]
    role_id: Annotated[str, StringConstraints(pattern=f"^{ROLE_ID_PATTERN}$")]
    name: DisplayName
    prompt: Annotated[str, StringConstraints(min_length=1, max_length=12_000)]

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value):
        if not value.strip():
            raise ValueError("Role prompt must contain text")
        return value


async def _local_operator(request: Request):
    if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="This API accepts local operators only")


def create_app(model=None, sandbox=None, *, state_dir: Path | None = None, bootstrap=None, search=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        state = Path(state_dir or os.environ.get("COLLABORATION_STATE_DIR", ".tmp/runtime"))
        selected_model = model if model is not None else CourseModel.from_env()
        runtime = None
        teams = None
        selected_search = None
        try:
            state.mkdir(parents=True, exist_ok=True)
            selected_sandbox = sandbox if sandbox is not None else DockerSandbox(
                state / "sandbox", image=os.environ.get("COLLABORATION_SANDBOX_IMAGE", "collaboration-sandbox:local")
            )
            runtime = Runtime(selected_model, selected_sandbox, state / "runtime.sqlite3")
            teams = TeamService(runtime.db, selected_model, selected_sandbox, bootstrap)
            selected_search = search if search is not None else SearchClient.from_env()
            workspace = WorkspaceService(runtime.db, teams, selected_sandbox, state / "team-artifacts", selected_search)
            runtime.workspace_tools = workspace
            app.state.runtime = runtime
            app.state.teams = teams
            app.state.workspace = workspace
            yield
        finally:
            if teams is not None:
                await teams.close()
            if runtime is not None:
                await runtime.close()
            if hasattr(selected_search, "aclose"):
                await selected_search.aclose()
            if hasattr(selected_model, "aclose"):
                await selected_model.aclose()

    app = FastAPI(title="Course collaboration runtime", lifespan=lifespan, dependencies=[Depends(_local_operator)])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.exception_handler(RoleFileError)
    async def invalid_roster(_request, exc):
        return JSONResponse(status_code=422, content={"detail": {"line": exc.line, "message": str(exc)}})

    @app.exception_handler(TeamConflict)
    async def team_conflict(_request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(TeamNotFound)
    async def team_not_found(_request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(TeamUnavailable)
    async def team_unavailable(_request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.post("/v1/teams", status_code=201)
    async def create_team(body: TeamInput):
        return await app.state.teams.create(body.request_id, body.roster, body.name)

    @app.get("/v1/teams/{team_id}")
    async def get_team(team_id: str):
        return app.state.teams.get(team_id)

    @app.get("/v1/teams/{team_id}/contracts/{version}")
    async def get_contract(team_id: str, version: int):
        return app.state.teams.get_contract(team_id, version)

    @app.post("/v1/teams/{team_id}/members", status_code=201)
    async def add_member(team_id: str, body: MemberInput):
        return await app.state.teams.add_member(
            team_id, body.request_id, body.expected_version, RoleDefinition(body.role_id, body.name, body.prompt),
        )

    @app.get("/v1/artifacts/{artifact_id}/versions/{version}")
    async def artifact_version(artifact_id: str, version: int, session_id: Annotated[str, Header(alias="X-Session-ID")]):
        try:
            record, path = app.state.workspace.resolve_number(session_id, artifact_id, version)
        except (WorkspaceNotFound, ArtifactNotFound) as exc:
            raise HTTPException(status_code=404, detail="Artifact version not found") from exc
        except ArtifactError as exc:
            raise HTTPException(status_code=503, detail="Artifact content is unavailable") from exc
        return FileResponse(path, media_type="application/octet-stream", headers={
            "Cache-Control": "no-store", "ETag": f'"{record["sha256"]}"',
        })

    def get_turn(role: str, turn_id: str):
        if role not in ROLES:
            try:
                app.state.workspace.actor(role)
            except WorkspaceNotFound as exc:
                raise HTTPException(status_code=404, detail="Turn not found in this session") from exc
        turn = app.state.runtime.get_turn(role, turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="Turn not found in this session")
        return turn

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/sessions/{role}/turns", status_code=202)
    async def start_turn(role: str, body: TurnInput):
        actor = None
        if role not in ROLES:
            try:
                actor = app.state.workspace.actor(role)
            except WorkspaceNotFound as exc:
                raise HTTPException(status_code=404, detail="Session not found") from exc
        try:
            turn_id = app.state.runtime.start(
                role, body.text,
                prompt=actor.prompt if actor else None,
                workspace_id=actor.workspace_id if actor else None,
            )
        except TurnConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"turn_id": turn_id, "status": "running"}

    @app.post("/v1/sessions/{role}/turns/{turn_id}/steer", status_code=202)
    async def steer_turn(role: str, turn_id: str, body: TurnInput):
        get_turn(role, turn_id)
        try:
            app.state.runtime.steer(role, turn_id, body.text)
        except TurnConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"turn_id": turn_id, "status": "queued"}

    @app.get("/v1/sessions/{role}/turns/{turn_id}")
    async def read_turn(role: str, turn_id: str):
        return get_turn(role, turn_id)

    @app.get("/v1/sessions/{role}/turns/{turn_id}/events")
    async def read_events(
        role: str,
        turn_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[int | None, Header(ge=0)] = None,
    ):
        get_turn(role, turn_id)
        runtime = app.state.runtime

        async def stream():
            cursor = max(after, last_event_id or 0)
            while True:
                if role not in ROLES:
                    try:
                        app.state.workspace.actor(role)
                    except WorkspaceNotFound:
                        return
                for event in runtime.events(turn_id, cursor):
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if runtime.get_turn(role, turn_id)["status"] != "running" and not runtime.events(turn_id, cursor):
                    return
                await asyncio.sleep(0.08)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
