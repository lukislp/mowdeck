"""Routes of the assistant under ``/api/v1/assistant`` (ADR 0075).

==========================================  ==============  =================================
Route                                       Rights          Purpose
==========================================  ==============  =================================
GET  /status                                the caller      ready? (AI provider configured)
POST /mowers/{id}/proposals                 ``can_view``    interpret one request: validated
                                                            actions to confirm (nothing runs)
==========================================  ==============  =================================

Every check runs on the caller's token against the public API; the actions a proposal offers
follow the caller's relations on the mower (``can_control``, ``can_edit_schedule``), and the
executing endpoints check them again after the user confirmed.
"""

from uuid import UUID

from fastapi import APIRouter

from assistant.adapters.api.deps import CallerDep, ServiceDep
from assistant.adapters.api.schemas import ProposalIn, ProposalOut, StatusOut

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.get("/status")
async def assistant_status(caller: CallerDep, service: ServiceDep) -> StatusOut:
    """Whether the caller can ask now: with the provider interpreter a configured AI provider
    (own or the installation's, Settings, AI provider) is needed; the phrase parser is always
    ready."""
    _, token = caller
    return StatusOut.of(await service.status(token), service.interpreter.kind)


@router.post(
    "/mowers/{mower_id}/proposals",
    responses={
        404: {"description": "The mower does not exist or the caller cannot see it."},
        409: {"description": "`assistant-provider-missing`: no AI provider configured."},
        422: {"description": "Empty or too long text, or an unknown time zone."},
        429: {"description": "`assistant-limit`: the caller's or the provider's limit."},
        502: {"description": "`assistant-provider-failed`: the model gave no usable answer."},
        503: {"description": "A MowDeck service the assistant reads did not answer."},
    },
)
async def propose(
    mower_id: UUID, body: ProposalIn, caller: CallerDep, service: ServiceDep
) -> ProposalOut:
    """Interpret one request for one mower and validate the result against the mower's
    actions, areas and the caller's rights. Answers a proposal; nothing is executed. The text
    is neither stored nor logged."""
    principal, token = caller
    proposal = await service.propose(principal, token, mower_id, body.text, body.timezone)
    return ProposalOut.of(proposal)
