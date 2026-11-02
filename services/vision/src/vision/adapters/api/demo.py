"""What Vision refuses in the public demo (ADR 0068), and the sandbox purge (ADR 0076).

The demo's AI assistant is the installation default with the rule based fake model
(``fake/recover``), so "AI recover" works without any model provider. Refused: setting, testing
or removing an own provider (a key or base URL would make the server call a third party) and
uploading own pictures.

``DELETE /vision/demo/accounts/{account_id}`` is for Identity's sandbox cleanup only (404
outside demo mode, 403 without the seed secret) and hidden from the API documents.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from mowdeck_service_kit.demo import DemoPolicy, DemoPurgeOut, require_demo_seed, rule
from mowdeck_service_kit.errors import ServiceUnavailable

from vision.application.demo import DemoPurge

_P = "/api/v1/vision"

DEMO_POLICY = DemoPolicy(
    blocked=(
        rule(
            rf"{_P}/me/provider(/test)?",
            "The demo uses a built-in demo model; your own AI provider works in your own MowDeck.",
        ),
        rule(
            rf"{_P}/mowers/[^/]+/recover/upload",
            "Uploading pictures is not available in the demo.",
        ),
    )
)

router = APIRouter(prefix="/vision/demo", tags=["vision: demo"])


@router.delete("/accounts/{account_id}", include_in_schema=False)
async def demo_purge(
    account_id: UUID,
    request: Request,
    _: Annotated[None, Depends(require_demo_seed)],
) -> DemoPurgeOut:
    """Delete everything Vision holds of an expired demo sandbox account: proposals with their
    images and steps, provider requests, own providers, preferences, opt-ins and the error
    watch. Idempotent."""
    purge: DemoPurge | None = getattr(request.app.state, "vision_demo", None)
    if purge is None:
        raise ServiceUnavailable("vision is starting")
    return DemoPurgeOut(deleted=await purge.purge_account(account_id))
