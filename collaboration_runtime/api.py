"""Run with uvicorn bound to loopback and a single worker."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from typing import Annotated

from .artifacts import ArtifactError, ArtifactNotFound
from .collaboration import CollaborationForbidden, CollaborationInvalid, CollaborationService, GoalInput, MessageInput
from .delegation import (
    CounterProposalInput, CreateProposalInput, DelegationConflict, DelegationService, ProposalReasonInput,
    ProposalVersionInput,
)
from .delegation_tools import DelegationTools
from .fork_messages import ForkDecisionInput, ForkMessageInput, ForkMessages
from .forks import ForkHandoffInput, ForkInput, ForkService
from .task_results import ReviewResultInput, SubmitResultInput
from .git_approvals import ApprovalInput, GitApprovals
from .git_executor import GitExecutor
from .local_auth import LocalAuth, Principal, SESSION_COOKIE, operator_credential
from .model import load_model
from .roles import ROLE_ID_PATTERN, RoleDefinition, RoleFileError
from .runtime import ROLES, Runtime, TurnConflict
from .sandbox import DockerSandbox
from .scheduler import DeliveryScheduler
from .search import SearchClient
from .teams import TeamConflict, TeamNotFound, TeamService, TeamUnavailable
from .workspace import WorkspaceNotFound, WorkspaceService

RequestId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
STATIC = Path(__file__).with_name("static")


class TurnInput(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)]


class TeamInput(BaseModel):
    request_id: RequestId
    roster: Annotated[str, StringConstraints(min_length=1, max_length=250_000)]
    name: DisplayName | None = None


class LoginInput(BaseModel):
    token: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


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
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(status_code=403, detail="This API accepts same-origin browser requests only")


async def _credential(
    request: Request, bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
):
    if "authorization" in request.headers:
        return bearer.credentials if bearer else None
    return request.cookies.get(SESSION_COOKIE)


Credential = Annotated[str | None, Depends(_credential)]


async def _authenticated(request: Request, response: Response, token: Credential):
    response.headers["Cache-Control"] = "no-store"
    actor = request.app.state.auth.resolve(token)
    if actor is None:
        raise HTTPException(status_code=401, detail="A valid local credential is required",
                            headers={"WWW-Authenticate": "Bearer"})
    return actor


Identity = Annotated[Principal, Depends(_authenticated)]


async def _require_operator(actor: Identity):
    if not actor.is_operator:
        raise HTTPException(status_code=403, detail="This action requires the local operator")


async def _team_identity(request: Request, team_id: str, actor: Identity):
    request.app.state.collaboration.team(team_id, actor)
    return actor


def _check_execution_identity(actor: Principal, role: str):
    allowed = actor.is_operator if role in ROLES else actor.session_id == role
    if not allowed:
        raise HTTPException(status_code=404, detail="Execution session not found")


def create_app(model=None, sandbox=None, *, state_dir: Path | None = None, bootstrap=None, search=None,
               operator_token: str | None = None, schedule: bool = True,
               git_targets_dir: Path | None = None, git_executor=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        state = Path(state_dir or os.environ.get("COLLABORATION_STATE_DIR", ".tmp/runtime"))
        selected_model = model if model is not None else load_model()
        runtime = None
        teams = None
        selected_search = None
        scheduler = None
        approval_worker = None
        try:
            state.mkdir(parents=True, exist_ok=True)
            selected_sandbox = sandbox if sandbox is not None else DockerSandbox(
                state / "sandbox", image=os.environ.get("COLLABORATION_SANDBOX_IMAGE", "collaboration-sandbox:local")
            )
            runtime = Runtime(selected_model, selected_sandbox, state / "runtime.sqlite3")
            teams = TeamService(runtime.db, selected_model, selected_sandbox, bootstrap)
            auth = LocalAuth(runtime.db, teams, operator_token if operator_token is not None else operator_credential(state))
            collaboration = CollaborationService(runtime.db, teams, auth)
            selected_search = search if search is not None else SearchClient.from_env()
            workspace = WorkspaceService(runtime.db, teams, selected_sandbox, state / "team-artifacts", selected_search)
            delegation = DelegationService(collaboration, workspace)
            collaboration.delegation = delegation
            collaboration.delegation_tools = DelegationTools(delegation, auth, workspace)
            runtime.workspace_tools = workspace
            runtime.collaboration = collaboration
            forks = ForkService(runtime, collaboration, workspace, delegation)
            runtime.forks = forks
            runtime.fork_messages = ForkMessages(forks, collaboration, runtime)
            workspace.assistant_access = forks.authorize_workspace
            workspace.assistant_readers = forks.material_readers
            delegation.forks = forks
            app.state.runtime = runtime
            app.state.teams = teams
            app.state.workspace = workspace
            app.state.auth = auth
            app.state.collaboration = collaboration
            app.state.delegation = delegation
            target_directory = git_targets_dir or os.environ.get("COLLABORATION_GIT_TARGETS_DIR")
            executor = git_executor
            if executor is None and target_directory:
                executor = GitExecutor(state / "git", Path(target_directory))
            runtime.git_approvals = GitApprovals(runtime, collaboration, workspace, executor) if executor else None
            if runtime.git_approvals is not None:
                approval_worker = asyncio.create_task(runtime.git_approvals.run())
            if schedule:
                scheduler = asyncio.create_task(DeliveryScheduler(runtime, collaboration, workspace).run())
            yield
        finally:
            workers = [worker for worker in (scheduler, approval_worker) if worker is not None]
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
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
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def workbench():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.post("/v1/auth/login", status_code=204)
    async def login(body: LoginInput, response: Response, request: Request):
        if app.state.auth.resolve(body.token) is None:
            raise HTTPException(status_code=401, detail="Invalid local credential")
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(SESSION_COOKIE, body.token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")

    @app.delete("/v1/auth/session", status_code=204)
    async def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="strict")

    @app.exception_handler(RoleFileError)
    async def invalid_roster(_request, exc):
        return JSONResponse(status_code=422, content={"detail": {"line": exc.line, "message": str(exc)}})

    @app.exception_handler(TeamConflict)
    async def team_conflict(_request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DelegationConflict)
    async def delegation_conflict(_request, exc):
        return JSONResponse(status_code=409, content={"detail": {"message": str(exc), **exc.details}})

    @app.exception_handler(TeamNotFound)
    async def team_not_found(_request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(TeamUnavailable)
    async def team_unavailable(_request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(CollaborationForbidden)
    async def collaboration_forbidden(_request, exc):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(CollaborationInvalid)
    async def collaboration_invalid(_request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.post("/v1/teams", status_code=201, dependencies=[Depends(_require_operator)])
    async def create_team(body: TeamInput):
        return await app.state.teams.create(body.request_id, body.roster, body.name)

    @app.get("/v1/teams")
    async def list_teams(actor: Identity):
        return app.state.collaboration.list_teams(actor)

    @app.get("/v1/teams/{team_id}")
    async def get_team(team_id: str, actor: Identity):
        return app.state.collaboration.team(team_id, actor)

    @app.get("/v1/teams/{team_id}/contracts/{version}", dependencies=[Depends(_team_identity)])
    async def get_contract(team_id: str, version: int):
        return app.state.teams.get_contract(team_id, version)

    @app.post("/v1/teams/{team_id}/members", status_code=201, dependencies=[Depends(_require_operator)])
    async def add_member(team_id: str, body: MemberInput):
        return await app.state.teams.add_member(
            team_id, body.request_id, body.expected_version, RoleDefinition(body.role_id, body.name, body.prompt),
        )

    @app.post("/v1/teams/{team_id}/members/{member_id}/credential", dependencies=[Depends(_require_operator)])
    async def issue_member_credential(team_id: str, member_id: str):
        return {"token": app.state.auth.issue(team_id, member_id)}

    @app.post("/v1/teams/{team_id}/messages", status_code=201)
    async def send_message(team_id: str, body: MessageInput, actor: Identity):
        return app.state.collaboration.send_message(team_id, actor, body)

    @app.get("/v1/teams/{team_id}/messages/{message_id}")
    async def get_message(team_id: str, message_id: str, actor: Identity):
        return app.state.collaboration.get_message(team_id, actor, message_id)

    @app.get("/v1/teams/{team_id}/deliveries")
    async def pending_deliveries(team_id: str, actor: Identity):
        return app.state.collaboration.pending_deliveries(team_id, actor)

    @app.post("/v1/teams/{team_id}/goals", status_code=201)
    async def register_goal(team_id: str, body: GoalInput, actor: Identity):
        return app.state.collaboration.register_goal(team_id, actor, body)

    @app.post("/v1/teams/{team_id}/proposals", status_code=201)
    async def create_proposal(team_id: str, body: CreateProposalInput, actor: Identity):
        return app.state.delegation.command(team_id, actor, "create_proposal", body)

    @app.get("/v1/proposals/{proposal_id}")
    async def get_proposal(proposal_id: str, actor: Identity, version: Annotated[int | None, Query(ge=1)] = None):
        service = app.state.delegation
        try:
            return service.get_proposal(service.proposal_team(proposal_id), actor, proposal_id, version)
        except TeamNotFound as exc:
            raise TeamNotFound("Proposal not found") from exc

    def proposal_command(proposal_id, actor, operation, body):
        service = app.state.delegation
        return service.command(service.proposal_team(proposal_id), actor, operation, body, proposal_id=proposal_id)

    @app.post("/v1/proposals/{proposal_id}/counter")
    async def counter_proposal(proposal_id: str, body: CounterProposalInput, actor: Identity):
        return proposal_command(proposal_id, actor, "counter_proposal", body)

    @app.post("/v1/proposals/{proposal_id}/confirm")
    async def confirm_proposal(proposal_id: str, body: ProposalVersionInput, actor: Identity):
        return proposal_command(proposal_id, actor, "confirm_proposal", body)

    @app.post("/v1/proposals/{proposal_id}/reject")
    async def reject_proposal(proposal_id: str, body: ProposalReasonInput, actor: Identity):
        return proposal_command(proposal_id, actor, "reject_proposal", body)

    @app.post("/v1/proposals/{proposal_id}/withdraw")
    async def withdraw_proposal(proposal_id: str, body: ProposalReasonInput, actor: Identity):
        return proposal_command(proposal_id, actor, "withdraw_proposal", body)

    @app.post("/v1/proposals/{proposal_id}/assign", status_code=201)
    async def assign_task(proposal_id: str, body: ProposalVersionInput, actor: Identity):
        return proposal_command(proposal_id, actor, "assign_task", body)

    @app.post("/v1/proposals/{proposal_id}/update")
    async def update_task(proposal_id: str, body: ProposalVersionInput, actor: Identity):
        return proposal_command(proposal_id, actor, "update_task", body)

    @app.get("/v1/teams/{team_id}/tasks/{task_id}")
    async def get_task(team_id: str, task_id: str, actor: Identity, revision: Annotated[int | None, Query(ge=1)] = None):
        return app.state.delegation.get_task(team_id, actor, task_id, revision)

    @app.post("/v1/teams/{team_id}/tasks/{task_id}/submissions", status_code=201)
    async def submit_result(team_id: str, task_id: str, body: SubmitResultInput, actor: Identity):
        return app.state.delegation.results.command(team_id, actor, task_id, body)

    @app.post("/v1/teams/{team_id}/tasks/{task_id}/submissions/{submission_id}/review", status_code=201)
    async def review_result(team_id: str, task_id: str, submission_id: str, body: ReviewResultInput, actor: Identity):
        return app.state.delegation.results.command(team_id, actor, task_id, body, submission_id=submission_id)

    @app.get("/v1/teams/{team_id}/snapshot")
    async def team_snapshot(team_id: str, actor: Identity):
        return app.state.collaboration.snapshot(team_id, actor)

    @app.get("/v1/teams/{team_id}/approvals", dependencies=[Depends(_require_operator)])
    async def pending_approvals(team_id: str, actor: Identity):
        approvals = app.state.runtime.git_approvals
        if approvals is None:
            app.state.collaboration.team(team_id, actor)
            return []
        return approvals.list_pending(team_id, actor)

    @app.post("/v1/approvals/{approval_id}/decision", dependencies=[Depends(_require_operator)])
    async def decide_approval(approval_id: str, body: ApprovalInput):
        approvals = app.state.runtime.git_approvals
        if approvals is None:
            raise TeamNotFound("Push approval not found")
        return approvals.decide(approval_id, body)

    @app.get("/v1/teams/{team_id}/git-actions/{action_id}")
    async def read_git_action(team_id: str, action_id: str, actor: Identity):
        approvals = app.state.runtime.git_approvals
        if approvals is None:
            raise TeamNotFound("Git action not found")
        return approvals.get_action(team_id, actor, action_id)

    @app.get("/v1/teams/{team_id}/events", response_class=EventSourceResponse)
    async def team_events(
        team_id: str, actor: Annotated[Principal, Depends(_team_identity)], token: Credential,
        after: str | None = None, last_event_id: Annotated[str | None, Header()] = None, follow: bool = True,
    ):
        collaboration = app.state.collaboration
        cursor = collaboration.resume_position(team_id, actor, last_event_id if last_event_id is not None else after)
        if cursor is None:
            yield ServerSentEvent(event="stream.reset", data={"reason": "snapshot_required"})
            return
        while app.state.auth.resolve(token) == actor:
            batch, cursor, has_more = collaboration.events(team_id, actor, cursor)
            for event_type, seq, data in batch:
                yield ServerSentEvent(event=event_type, id=collaboration.cursor(team_id, actor, seq), data=data)
            if has_more:
                continue
            if not follow:
                return
            await asyncio.sleep(0.2)

    @app.get("/v1/artifacts/{artifact_id}/versions/{version}")
    async def artifact_version(artifact_id: str, version: int, actor: Identity):
        if actor.session_id is None:
            raise HTTPException(status_code=404, detail="Artifact version not found")
        try:
            record, path = app.state.workspace.resolve_number(actor.session_id, artifact_id, version)
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

    @app.post("/v1/sessions/{role}/forks", status_code=201)
    async def create_fork(role: str, body: ForkInput, actor: Identity):
        try:
            return await app.state.runtime.forks.create(actor, role, body)
        except ArtifactNotFound as exc:
            raise HTTPException(status_code=404, detail="Fork material not found") from exc

    @app.get("/v1/forks/{assistant_id}/messages")
    async def read_fork_messages(assistant_id: str, actor: Identity):
        return app.state.runtime.fork_messages.list_for(assistant_id, actor.session_id)

    @app.post("/v1/forks/{assistant_id}/messages", status_code=201)
    async def send_fork_message(assistant_id: str, body: ForkMessageInput, actor: Identity):
        return app.state.runtime.fork_messages.post(assistant_id, actor.session_id, body)

    @app.post("/v1/forks/{assistant_id}/decision", status_code=201)
    async def decide_fork_result(assistant_id: str, body: ForkDecisionInput, actor: Identity):
        return app.state.runtime.fork_messages.decide(assistant_id, actor.session_id, body)

    @app.post("/v1/forks/{assistant_id}/revoke")
    async def revoke_fork(assistant_id: str, actor: Identity):
        return app.state.runtime.forks.revoke(assistant_id, actor)

    @app.post("/v1/forks/{assistant_id}/handoff", status_code=201)
    async def handoff_fork_material(assistant_id: str, body: ForkHandoffInput, actor: Identity):
        try:
            return app.state.runtime.forks.handoff(assistant_id, actor, body)
        except ArtifactNotFound as exc:
            raise HTTPException(status_code=404, detail="Fork material not found") from exc

    def check_turn_reader(role, caller, turn_id):
        forks = app.state.runtime.forks
        if not forks.exists(role):
            _check_execution_identity(caller, role)
        elif not any(turn["turn_id"] == turn_id for turn in forks.observable_turns(role, caller)):
            raise HTTPException(status_code=404, detail="Turn not found in this session")

    @app.post("/v1/sessions/{role}/turns", status_code=202)
    async def start_turn(role: str, body: TurnInput, caller: Identity):
        _check_execution_identity(caller, role)
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
    async def steer_turn(role: str, turn_id: str, body: TurnInput, caller: Identity):
        _check_execution_identity(caller, role)
        get_turn(role, turn_id)
        try:
            app.state.runtime.steer(role, turn_id, body.text)
        except TurnConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"turn_id": turn_id, "status": "queued"}

    @app.get("/v1/sessions/{role}/turns/{turn_id}")
    async def read_turn(role: str, turn_id: str, caller: Identity):
        check_turn_reader(role, caller, turn_id)
        turn = get_turn(role, turn_id)
        if app.state.runtime.forks.exists(role):
            return {**turn, "reply": None, "error": None}
        return turn

    @app.get("/v1/sessions/{role}/turns")
    async def list_turns(role: str, caller: Identity, completed_only: bool = False):
        runtime = app.state.runtime
        if runtime.forks.exists(role):
            turns = runtime.forks.observable_turns(role, caller)
        else:
            _check_execution_identity(caller, role)
            turns = runtime.list_turns(role)
        return [turn for turn in turns if turn["status"] == "completed"] if completed_only else turns

    @app.get("/v1/sessions/{role}/turns/{turn_id}/events")
    async def read_events(
        role: str,
        turn_id: str,
        caller: Identity,
        token: Credential,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[int | None, Header(ge=0)] = None,
    ):
        check_turn_reader(role, caller, turn_id)
        get_turn(role, turn_id)
        runtime = app.state.runtime
        restricted = runtime.forks.exists(role)

        async def stream():
            cursor = max(after, last_event_id or 0)
            while app.state.auth.resolve(token) == caller:
                if role not in ROLES:
                    try:
                        app.state.workspace.actor(role)
                    except WorkspaceNotFound:
                        return
                for event in runtime.events(turn_id, cursor):
                    cursor = event["seq"]
                    if restricted:
                        if not event["type"].startswith("turn."):
                            continue
                        event = {"seq": cursor, "type": event["type"]}
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if runtime.get_turn(role, turn_id)["status"] != "running" and not runtime.events(turn_id, cursor):
                    return
                await asyncio.sleep(0.08)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return app


app = create_app()
