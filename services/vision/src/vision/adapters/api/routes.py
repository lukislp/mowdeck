"""Routes of vision under ``/api/v1/vision``.

==========================================  ==============  ====================================
Route                                       Who             What
==========================================  ==============  ====================================
GET  /status                                any principal   hosted default (model only), policy
GET  /providers                             any principal   model families, hosted availability
GET  /me/provider                           the caller      own provider (no key), effective one
PUT  /me/provider                           the caller      save own provider (key write-only)
DELETE /me/provider                         the caller      delete own provider
POST /me/provider/test                      the caller      minimal vision call (rate limited)
POST /me/provider/structured                the caller      text-only JSON answer (ADR 0075)
GET  /me/preferences                        the caller      preferences: stereo frames, automatic
                                                            analysis on errors (with its limits)
PUT  /me/preferences                        the caller      change them (opt-in: ``ai_recover``)
POST /mowers/{id}/recover                   ``can_drive``   start an analysis (202), plan feature
POST /mowers/{id}/recover/upload            ``can_drive``   the same with multipart image files
GET  /mowers/{id}/proposals                 ``can_view``    proposals of the mower, newest first
GET  /proposals/{id}                        ``can_view``    one proposal with its steps (poll)
GET  /proposals/{id}/images/{index}         ``can_view``    an image the model saw (short-lived)
POST /proposals/{id}/approve                ``can_drive``   approve: drive it on the caller's behalf
POST /proposals/{id}/reject                 ``can_drive``   reject, or cancel a running execution
POST /proposals/{id}/retry                  ``can_drive``   another round (up to the limit)
==========================================  ==============  ====================================
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    File,
    Form,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from mowdeck_contracts.v1 import VisionTask

from vision.adapters.api.deps import (
    AutoSettingsDep,
    CallerDep,
    MowerDriver,
    MowerViewer,
    ProposalDriver,
    ProposalViewer,
    ProvidersDep,
    ServiceDep,
)
from vision.adapters.api.schemas import (
    ApproveIn,
    FamilyOut,
    PreferencesIn,
    PreferencesOut,
    ProposalOut,
    ProposalPageOut,
    ProviderIn,
    ProviderOut,
    ProvidersOut,
    ProviderTestOut,
    RecoverIn,
    RetryIn,
    StatusOut,
    StructuredIn,
    StructuredOut,
)
from vision.application.providers import ProviderUpdate, StructuredRequest
from vision.domain.errors import InvalidInput
from vision.domain.models import Image

router = APIRouter(prefix="/vision", tags=["vision"])


async def _uploads(files: list[UploadFile], max_bytes: int) -> list[Image]:
    images: list[Image] = []
    for item in files:
        media_type = (item.content_type or "").split(";")[0].strip()
        if media_type not in ("image/jpeg", "image/png"):
            raise InvalidInput("images must be JPEG or PNG")
        images.append(Image(await item.read(max_bytes + 1), media_type, "upload"))
    return images


@router.get("/status")
async def vision_status(caller: CallerDep, request: Request) -> StatusOut:
    """What the operator configured: whether a hosted default exists and its model string
    (never a key), whether users can store keys, the base URL policy."""
    config = request.app.state.vision_status
    return StatusOut(**config)


@router.get("/providers")
async def providers(caller: CallerDep, service: ProvidersDep) -> ProvidersOut:
    """The model families users can configure and whether the installation's model may be
    used by the caller's account."""
    assert caller.account_id is not None  # noqa: S101 - account principals only
    hosted = await service.hosted_available(caller.account_id)
    return ProvidersOut(
        families=[FamilyOut.of(f) for f in service.families()],
        hosted_available=hosted,
        hosted_model=service.hosted.model if hosted else None,
        secrets_available=service.box is not None,
    )


@router.get("/me/provider")
async def my_provider(caller: CallerDep, service: ProvidersDep) -> ProviderOut:
    """The caller's own provider (the key only as ``api_key_set``) and the provider a request
    would use now (``effective``)."""
    view = await service.view(caller)
    return ProviderOut.of(view, secrets_available=service.box is not None)


@router.put("/me/provider")
async def put_my_provider(
    body: ProviderIn, caller: CallerDep, service: ProvidersDep
) -> ProviderOut:
    """Save the caller's provider. 422 for an unknown family, a refused base URL or bad
    headers; 409 ``vision-secrets-unavailable`` when a key is sent but cannot be stored."""
    view = await service.put(
        caller,
        ProviderUpdate(
            model=body.model.strip(),
            enabled=body.enabled,
            api_base=body.api_base,
            api_key=body.api_key,
            headers=body.headers,
        ),
    )
    return ProviderOut.of(view, secrets_available=service.box is not None)


@router.delete("/me/provider", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_provider(caller: CallerDep, service: ProvidersDep) -> Response:
    """Delete the caller's provider (404 when there is none)."""
    await service.delete(caller)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/me/provider/test")
async def test_my_provider(caller: CallerDep, service: ProvidersDep) -> ProviderTestOut:
    """One minimal vision call with a tiny generated image: model, latency, tokens and the
    estimated cost. 429 ``vision-quota`` beyond the hourly limit."""
    return ProviderTestOut.of(await service.test(caller))


@router.post(
    "/me/provider/structured",
    responses={
        409: {"description": "`vision-provider-missing`: no own provider, no hosted default."},
        422: {"description": "A field is out of its limits or the schema is not an object."},
        429: {"description": "`vision-quota`: the hourly limit of structured answers."},
        502: {"description": "`vision-provider-failed`: no JSON object came back."},
    },
)
async def structured_answer(
    body: StructuredIn, caller: CallerDep, service: ProvidersDep
) -> StructuredOut:
    """A text-only question to the caller's effective provider (own, else the installation's)
    whose answer is one JSON object shaped by ``schema`` (ADR 0075): the interface other
    modules (the optional assistant) use to reuse the AI provider without seeing a key. Its own
    hourly limit per user (``MOWDECK_VISION_STRUCTURED_PER_HOUR``); recorded in the cost
    accounting as kind ``structured``. The text and the answer are neither logged nor stored;
    the caller validates the answer."""
    result = await service.structured(
        caller,
        StructuredRequest(
            purpose=body.purpose,
            system=body.system,
            text=body.text,
            schema=body.json_schema,
            name=body.name,
        ),
    )
    return StructuredOut.of(result)


@router.get("/me/preferences")
async def my_preferences(
    caller: CallerDep, service: ProvidersDep, auto: AutoSettingsDep
) -> PreferencesOut:
    """The caller's AI assist preferences (the defaults until saved): ``stereo_frames`` and the
    opt-in to automatic AI analyses on errors (off until saved) with the installation's limits
    (cooldown per mower, analyses per mower and day)."""
    return PreferencesOut.of(await service.preferences(caller), await auto.get(caller))


@router.put("/me/preferences")
async def put_my_preferences(
    body: PreferencesIn, caller: CallerDep, service: ProvidersDep, auto: AutoSettingsDep
) -> PreferencesOut:
    """Change the caller's AI assist preferences; a field left out keeps its value.

    ``auto_analysis``: when a mower of the account enters a recoverable error (stuck, blocked,
    outside the work area, wheel slip), the service takes a photo, analyses it with the caller's
    provider (or the installation's) and asks for approval like a manual request; nothing is
    executed automatically. Enabling needs the plan feature ``ai_recover`` (403
    ``feature-not-in-plan``; nothing is saved then)."""
    auto_view = (
        await auto.get(caller)
        if body.auto_analysis is None
        else await auto.put(caller, enabled=body.auto_analysis)
    )
    preferences = (
        await service.preferences(caller)
        if body.stereo_frames is None
        else await service.put_preferences(caller, stereo_frames=body.stereo_frames)
    )
    return PreferencesOut.of(preferences, auto_view)


@router.post("/mowers/{mower_id}/recover", status_code=status.HTTP_202_ACCEPTED)
async def recover(
    mower_id: UUID, body: RecoverIn, principal: MowerDriver, service: ServiceDep
) -> ProposalOut:
    """Start AI assist for the mower: ``task`` ``recover`` or ``find_dock``, optional camera
    snapshots (base64, each optionally with ``camera``, ``side`` and ``captured_at``; a
    ``left`` and a ``right`` frame of the same moment are analysed as a stereo pair when the
    caller's preference ``stereo_frames`` is on, else only the left one). The analysis runs in
    the background; poll ``GET /proposals/{id}``. Without images the mower's gateway is asked
    for one. 403 ``feature-not-in-plan`` without ``ai_recover``, 409 ``vision-provider-missing``
    without a usable provider, 409 when a proposal for the mower is open, 429 ``vision-quota``
    beyond the daily cap."""
    images = [image.to_image() for image in body.images]
    proposal = await service.request(principal, mower_id, body.task, images)
    return ProposalOut.of(proposal, rounds_left=service.config.max_rounds - proposal.round)


@router.post("/mowers/{mower_id}/recover/upload", status_code=status.HTTP_202_ACCEPTED)
async def recover_upload(
    mower_id: UUID,
    principal: MowerDriver,
    service: ServiceDep,
    task: Annotated[VisionTask, Form()] = VisionTask.RECOVER,
    images: Annotated[list[UploadFile] | None, File()] = None,
) -> ProposalOut:
    """``recover`` with the snapshots as ``multipart/form-data`` files (field ``images``)."""
    uploaded = await _uploads(images or [], service.config.max_image_bytes)
    proposal = await service.request(principal, mower_id, task, uploaded)
    return ProposalOut.of(proposal, rounds_left=service.config.max_rounds - proposal.round)


@router.get("/mowers/{mower_id}/proposals")
async def mower_proposals(
    mower_id: UUID,
    principal: MowerViewer,
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: datetime | None = None,
) -> ProposalPageOut:
    """Proposals of the mower, newest first."""
    rows = await service.list_for_mower(principal, mower_id, limit=limit + 1, before=before)
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        ProposalOut.of(p, rounds_left=max(0, service.config.max_rounds - p.round)) for p in rows
    ]
    return ProposalPageOut(items=items, next_before=rows[-1].created_at if more else None)


@router.get("/proposals/{proposal_id}")
async def get_proposal(access: ProposalViewer, service: ServiceDep) -> ProposalOut:
    """One proposal with its step audit; poll while ``final`` is false."""
    principal, proposal = access
    return ProposalOut.of_details(await service.details(principal, proposal.id))


@router.get(
    "/proposals/{proposal_id}/images/{index}",
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}}}},
    response_class=Response,
)
async def proposal_image(
    index: Annotated[int, Path(ge=0, le=7)], access: ProposalViewer, service: ServiceDep
) -> Response:
    """An image the model saw; 404 once it was purged (after the proposal TTL)."""
    principal, proposal = access
    stored = await service.image(principal, proposal.id, int(index))
    return Response(
        content=stored.data,
        media_type=stored.media_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/proposals/{proposal_id}/approve")
async def approve(
    access: ProposalDriver,
    service: ServiceDep,
    body: Annotated[ApproveIn | None, Body()] = None,
) -> ProposalOut:
    """Approve: the chosen option runs on the caller's behalf (blades off, dead-man 1 s). The
    optional body picks the option (``model`` or ``fallback``; omitted: the default
    ``action``). One approval per proposal; 409 ``vision-proposal-expired`` after
    ``expires_at``; 422 for an option the proposal does not offer."""
    principal, proposal = access
    option = body.option if body is not None else None
    approved = await service.approve(principal, proposal.id, option)
    return ProposalOut.of(approved, await service.store.steps(approved.id))


@router.post("/proposals/{proposal_id}/reject")
async def reject(access: ProposalDriver, service: ServiceDep) -> ProposalOut:
    """Reject a waiting proposal, or stop a running execution at once."""
    principal, proposal = access
    return ProposalOut.of(await service.reject(principal, proposal.id))


@router.post("/proposals/{proposal_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry(body: RetryIn, access: ProposalDriver, service: ServiceDep) -> ProposalOut:
    """Another round after one that did not help (``can_retry``), with fresh snapshots."""
    principal, proposal = access
    images = [image.to_image() for image in body.images]
    created = await service.retry(principal, proposal.id, images)
    return ProposalOut.of(created, rounds_left=service.config.max_rounds - created.round)
