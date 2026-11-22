"""Adapt model tool calls to the same delegation commands used by HTTP."""

from typing import Annotated

from pydantic import Field, ValidationError

from .collaboration import CollaborationForbidden, CollaborationInvalid, Command, ObjectId
from .delegation import CounterProposalInput, CreateProposalInput, ProposalReasonInput, ProposalVersionInput
from .teams import TeamConflict, TeamNotFound
from .workspace import ASSISTANT_DRAFT_TOOLS, WorkspaceNotFound


class ProposalCounterTool(CounterProposalInput):
    proposal_id: ObjectId


class ProposalVersionTool(ProposalVersionInput):
    proposal_id: ObjectId


class ProposalReasonTool(ProposalReasonInput):
    proposal_id: ObjectId


class ReadProposalTool(Command):
    proposal_id: ObjectId
    version: Annotated[int, Field(ge=1)] | None = None


class ReadTaskTool(Command):
    task_id: ObjectId
    revision: Annotated[int, Field(ge=1)] | None = None


TOOLS = {
    "create_proposal": (CreateProposalInput, "Open an initial negotiation or propose a revision to a task."),
    "counter_proposal": (ProposalCounterTool, "Offer new terms for the current proposal version. This clears confirmations for the new version."),
    "confirm_proposal": (ProposalVersionTool, "Confirm the exact current proposal version as a required party."),
    "reject_proposal": (ProposalReasonTool, "Reject the current proposal version as a required party."),
    "withdraw_proposal": (ProposalReasonTool, "Withdraw the current proposal version you submitted."),
    "assign_task": (ProposalVersionTool, "Register one task after all required parties confirm the current initial proposal."),
    "update_task": (ProposalVersionTool, "Register a task revision after all parties confirm the current proposal and the task base still matches."),
    "get_proposal": (ReadProposalTool, "Read a proposal and the confirmations for a version you can access."),
    "get_task": (ReadTaskTool, "Read a task and its current or specified revision."),
    "list_delegation": (Command, "List active goals, delegation proposals and tasks visible in your team."),
}

COMMAND_TYPES = {
    "counter_proposal": CounterProposalInput,
    "confirm_proposal": ProposalVersionInput,
    "reject_proposal": ProposalReasonInput,
    "withdraw_proposal": ProposalReasonInput,
    "assign_task": ProposalVersionInput,
    "update_task": ProposalVersionInput,
}


class DelegationTools:
    def __init__(self, service, auth, workspace):
        self.service = service
        self.auth = auth
        self.workspace = workspace

    @staticmethod
    def handles(name):
        return name in TOOLS

    def tools_for(self, session_id):
        if self.auth.member(session_id) is not None:
            available = TOOLS.keys()
        else:
            try:
                assistant = self.workspace.actor(session_id)
            except WorkspaceNotFound:
                return []
            available = ASSISTANT_DRAFT_TOOLS.intersection(assistant.allowed_tools)
        return [
            {"type": "function", "function": {
                "name": name, "description": description, "parameters": schema.model_json_schema(),
            }} for name, (schema, description) in TOOLS.items() if name in available
        ]

    def execute(self, session_id, name, raw_arguments):
        try:
            actor = self.auth.member(session_id)
            assistant = None
            if actor is None:
                assistant = self.workspace.actor(session_id)
                if name not in ASSISTANT_DRAFT_TOOLS or name not in assistant.allowed_tools:
                    raise CollaborationForbidden("An assistant can only submit drafts in its authorized scope")
                actor = self.auth.member(assistant.parent_session_id)
                if actor is None or actor.team_id != assistant.team_id:
                    raise CollaborationForbidden("The assistant's parent member is unavailable")

            args = TOOLS[name][0].model_validate_json(raw_arguments)
            if name == "get_proposal":
                result = self.service.get_proposal(actor.team_id, actor, args.proposal_id, args.version)
            elif name == "get_task":
                result = self.service.get_task(actor.team_id, actor, args.task_id, args.revision)
            elif name == "list_delegation":
                result = self.service.snapshot(actor.team_id, actor)
                result["active_goals"] = self.service.active_goals(actor.team_id, actor)
            elif name == "create_proposal":
                result = self.service.command(actor.team_id, actor, name, args, assistant=assistant)
            else:
                body = COMMAND_TYPES[name].model_validate(args.model_dump(exclude={"proposal_id"}))
                result = self.service.command(actor.team_id, actor, name, body, proposal_id=args.proposal_id,
                                              assistant=assistant)
            return {"ok": True, "result": result}
        except ValidationError:
            return {"ok": False, "error": "Invalid delegation tool arguments", "status": 422}
        except (WorkspaceNotFound, TeamNotFound, TeamConflict, CollaborationForbidden, CollaborationInvalid) as exc:
            if isinstance(exc, TeamConflict):
                status = 409
            elif isinstance(exc, CollaborationForbidden):
                status = 403
            elif isinstance(exc, CollaborationInvalid):
                status = 422
            else:
                status = 404
            result = {"ok": False, "error": str(exc), "status": status}
            if hasattr(exc, "details"):
                result["details"] = exc.details
            return result
