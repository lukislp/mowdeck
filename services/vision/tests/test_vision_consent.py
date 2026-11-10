# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Explicit consent before data goes to a cloud AI model (ADR 0081), through the real app.

The installation default of these tests is a cloud model (``anthropic/...``, answered by the
static factory); the rest of the suite uses the simulated ``fake/recover`` and never needs
consent. Every path that sends user data refuses without a covering consent before anything is
captured or sent: manual request and retry, automatic analysis and structured answers; the
provider test is exempt.
"""

from typing import Any

from mowdeck_contracts.v1 import MowerActivity, utc_now
from test_vision_flow import _png_b64
from vision_support import (
    API,
    Harness,
    Person,
    SimRig,
    StaticFactory,
    StaticStructuredFactory,
    answer,
    harness,
    wait_for,
)

from vision.application.auto import AutoAnalysis
from vision.domain.auto import Skip
from vision.domain.models import AutoSetting, ProposalState, RiskFlag
from vision.settings import DEMO_MODEL

CLOUD_MODEL = "anthropic/claude-sonnet-5-5"
CLOUD: dict[str, Any] = {"vision_default_model": CLOUD_MODEL}
CONSENT = f"{API}/me/consent"
STRUCTURED = f"{API}/me/provider/structured"
PROVIDER = f"{API}/me/provider"
PROBLEM = "urn:mowdeck:problem:vision-consent-required"


async def _stuck(h: Harness, owner: Person) -> SimRig:
    rig = await h.rig(owner)
    rig.sim.inject_stuck()
    await wait_for(
        lambda: (
            (o := h.service.view.state(rig.mower_id)) is not None
            and o.state.activity is MowerActivity.ERROR
        )
    )
    return rig


def _recover(rig: SimRig) -> str:
    return f"{API}/mowers/{rig.mower_id}/recover"


def _structured() -> dict[str, Any]:
    return {
        "purpose": "assistant",
        "system": "Turn requests into actions.",
        "text": "send it home",
        "schema": {"type": "object", "properties": {"reply": {"type": "string"}}},
    }


async def _agree(h: Harness, person: Person) -> dict[str, Any]:
    response = await h.client.put(CONSENT, headers=person.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- status, grant, withdraw -----------------------------------------------------------------


async def test_vision_consent_status_grant_and_withdraw() -> None:
    async with harness(structured=StaticStructuredFactory(), **CLOUD) as h:
        owner = h.owner()
        before = (await h.client.get(CONSENT, headers=owner.headers)).json()
        assert before["required"] is True and before["covers"] is False
        assert before["granted_at"] is None and before["granted_for"] is None
        assert before["destination"] == {
            "kind": "cloud",
            "provider": "Anthropic",
            "family": "anthropic",
            "model": CLOUD_MODEL,
        }
        granted = await _agree(h, owner)
        assert granted["covers"] is True and granted["granted_at"] is not None
        assert granted["granted_for"]["model"] == CLOUD_MODEL
        assert (await h.client.get(CONSENT, headers=owner.headers)).json() == granted
        withdrawn = await h.client.delete(CONSENT, headers=owner.headers)
        assert withdrawn.status_code == 200
        assert withdrawn.json()["covers"] is False and withdrawn.json()["granted_at"] is None


async def test_vision_consent_is_not_required_for_simulated_or_local_models() -> None:
    async with harness() as h:  # fake/recover: simulated
        owner = h.owner()
        status = (await h.client.get(CONSENT, headers=owner.headers)).json()
        assert status["required"] is False and status["destination"]["kind"] == "simulated"
        granted = await _agree(h, owner)  # nothing to record
        assert granted["granted_at"] is None
    async with harness(vision_default_model="ollama/llava") as h:
        status = (await h.client.get(CONSENT, headers=h.owner().headers)).json()
        assert status["required"] is False and status["destination"]["kind"] == "local"


async def test_vision_consent_default_local_setting_classifies_the_installation_default() -> None:
    async with harness(
        vision_default_model="openai/qwen2.5-vl",
        vision_default_api_base="http://vllm:8000/v1",
        vision_default_local=True,
        structured=StaticStructuredFactory(),
    ) as h:
        owner = h.owner()
        assert (await h.client.get(f"{API}/status", headers=owner.headers)).json()[
            "hosted_default_kind"
        ] == "local"
        assert (await h.client.get(CONSENT, headers=owner.headers)).json()["required"] is False
        assert (
            await h.client.post(STRUCTURED, json=_structured(), headers=owner.headers)
        ).is_success
    # unknown means cloud: the same model without the operator's statement
    async with harness(vision_default_model="openai/gpt-5-mini") as h:
        owner = h.owner()
        status = (await h.client.get(f"{API}/status", headers=owner.headers)).json()
        assert status["hosted_default_kind"] == "cloud"
        providers = (await h.client.get(f"{API}/providers", headers=owner.headers)).json()
        assert providers["hosted_destination"]["kind"] == "cloud"
        me = (await h.client.get(PROVIDER, headers=owner.headers)).json()
        assert me["destination"]["kind"] == "cloud" and me["effective"] == "hosted"


async def test_vision_consent_without_a_provider_cannot_be_granted() -> None:
    async with harness(vision_default_enabled=False) as h:
        owner = h.owner()
        status = (await h.client.get(CONSENT, headers=owner.headers)).json()
        assert status["required"] is False and status["destination"] is None
        assert (await h.client.put(CONSENT, headers=owner.headers)).status_code == 409


async def test_vision_consent_follows_the_provider_in_effect_and_its_model() -> None:
    async with harness(structured=StaticStructuredFactory(), **CLOUD) as h:
        owner = h.owner()
        await _agree(h, owner)
        await h.client.put(
            PROVIDER, json={"model": "anthropic/claude-haiku-5-5"}, headers=owner.headers
        )
        status = (await h.client.get(CONSENT, headers=owner.headers)).json()
        assert status["required"] is True and status["covers"] is False, "another model"
        assert status["granted_for"]["model"] == CLOUD_MODEL
        assert status["destination"]["model"] == "anthropic/claude-haiku-5-5"
        refused = await h.client.post(STRUCTURED, json=_structured(), headers=owner.headers)
        assert refused.status_code == 403 and refused.json()["type"] == PROBLEM
        await _agree(h, owner)  # re-consent for the new model
        assert (
            await h.client.post(STRUCTURED, json=_structured(), headers=owner.headers)
        ).is_success
        # a local own provider needs none, and consent for it is not asked
        await h.client.put(PROVIDER, json={"model": "ollama/llava"}, headers=owner.headers)
        assert (await h.client.get(CONSENT, headers=owner.headers)).json()["required"] is False


async def test_vision_consent_is_per_user_and_account() -> None:
    async with harness(structured=StaticStructuredFactory(), **CLOUD) as h:
        alice, bob = h.owner(), h.owner()
        await _agree(h, alice)
        assert (
            await h.client.post(STRUCTURED, json=_structured(), headers=alice.headers)
        ).is_success
        other = (await h.client.get(CONSENT, headers=bob.headers)).json()
        assert other["covers"] is False and other["granted_at"] is None
        refused = await h.client.post(STRUCTURED, json=_structured(), headers=bob.headers)
        assert refused.status_code == 403
        # a second user of alice's account has no consent of their own either
        sub = h.person(alice.account_id)
        assert (await h.client.get(CONSENT, headers=sub.headers)).json()["covers"] is False
        # withdrawing bob's (empty) consent leaves alice's alone
        await h.client.delete(CONSENT, headers=bob.headers)
        assert (await h.client.get(CONSENT, headers=alice.headers)).json()["covers"] is True


# --- enforcement on every sending path ---------------------------------------------------------


async def test_vision_consent_manual_request_is_refused_before_any_photo() -> None:
    async with harness(structured=StaticStructuredFactory(), **CLOUD) as h:
        owner = h.owner()
        rig = await _stuck(h, owner)
        rig.photos = False  # a photo request would time out; the refusal must come first
        refused = await h.client.post(_recover(rig), json={}, headers=owner.headers)
        assert refused.status_code == 403, refused.text
        problem = refused.json()
        assert problem["type"] == PROBLEM
        assert problem["errors"] == [
            {"parameter": "destination", "code": "cloud", "detail": f"Anthropic / {CLOUD_MODEL}"}
        ]
        assert h.factory.calls == []
        assert h.store.proposals == {}, "no proposal, nothing recorded"
        # the same with uploaded images: they are not analysed either
        body = {"images": [{"media_type": "image/png", "data": _png_b64()}]}
        assert (
            await h.client.post(_recover(rig), json=body, headers=owner.headers)
        ).status_code == 403
        assert h.factory.calls == []
        await _agree(h, owner)
        created = await h.client.post(_recover(rig), json={}, headers=owner.headers)
        assert created.status_code == 202, created.text
        await h.until(created.json()["id"], owner, ProposalState.PROPOSED)
        assert len(h.factory.calls) == 1


async def test_vision_consent_retry_is_refused_after_withdrawal() -> None:
    factory = StaticFactory(default=answer(risk=[RiskFlag.PEOPLE_VISIBLE]))
    async with harness(factory=factory, structured=StaticStructuredFactory(), **CLOUD) as h:
        owner = h.owner()
        rig = await _stuck(h, owner)
        await _agree(h, owner)
        first = (await h.client.post(_recover(rig), json={}, headers=owner.headers)).json()
        await h.until(first["id"], owner, ProposalState.REJECTED_BY_AI)
        await h.client.delete(CONSENT, headers=owner.headers)
        calls = len(h.factory.calls)
        retry = f"{API}/proposals/{first['id']}/retry"
        refused = await h.client.post(retry, json={}, headers=owner.headers)
        assert refused.status_code == 403 and refused.json()["type"] == PROBLEM
        assert len(h.factory.calls) == calls
        await _agree(h, owner)
        assert (await h.client.post(retry, json={}, headers=owner.headers)).status_code == 202


async def test_vision_consent_structured_answers_need_it_and_the_provider_test_does_not() -> None:
    async with harness(structured=StaticStructuredFactory(), **CLOUD) as h:
        owner = h.owner()
        refused = await h.client.post(STRUCTURED, json=_structured(), headers=owner.headers)
        assert refused.status_code == 403 and refused.json()["type"] == PROBLEM
        assert h.store.requests == [], "nothing was asked, nothing is accounted"
        await _agree(h, owner)
        assert (
            await h.client.post(STRUCTURED, json=_structured(), headers=owner.headers)
        ).is_success
        # A user's own cloud provider with another consent state: the test sends a synthetic
        # image and is exempt.
        other = h.owner()
        await h.client.put(PROVIDER, json={"model": CLOUD_MODEL}, headers=other.headers)
        tested = await h.client.post(f"{PROVIDER}/test", headers=other.headers)
        assert tested.status_code == 200 and tested.json()["ok"] is True


async def test_vision_consent_demo_model_never_asks() -> None:
    async with harness(vision_default_model=DEMO_MODEL) as h:
        owner = h.owner()
        assert (
            await h.client.post(STRUCTURED, json=_structured(), headers=owner.headers)
        ).is_success
        rig = await _stuck(h, owner)
        assert (
            await h.client.post(_recover(rig), json={}, headers=owner.headers)
        ).status_code == 202


# --- automatic analysis ----------------------------------------------------------------------


async def _store_opt_in(h: Harness, person: Person) -> None:
    from datetime import timedelta

    await h.store.put_auto_setting(
        AutoSetting(
            user_id=person.principal.subject,
            account_id=person.account_id,
            enabled=True,
            updated_at=utc_now() - timedelta(minutes=1),
            kind=person.principal.kind,
        )
    )


def _auto(h: Harness) -> AutoAnalysis:
    auto: AutoAnalysis = h.app.state.auto_analysis
    return auto


async def test_vision_consent_enabling_automatic_analysis_needs_it() -> None:
    async with harness(structured=StaticStructuredFactory(), **CLOUD) as h:
        owner = h.owner()
        url = f"{API}/me/preferences"
        refused = await h.client.put(url, json={"auto_analysis": True}, headers=owner.headers)
        assert refused.status_code == 403 and refused.json()["type"] == PROBLEM
        assert (await h.client.get(url, headers=owner.headers)).json()["auto_analysis"] is False
        await _agree(h, owner)
        enabled = await h.client.put(url, json={"auto_analysis": True}, headers=owner.headers)
        assert enabled.status_code == 200 and enabled.json()["auto_analysis"] is True
        # a preference that is not the opt-in never asks
        other = h.owner()
        assert (
            await h.client.put(url, json={"stereo_frames": False}, headers=other.headers)
        ).is_success


async def test_vision_consent_withdrawing_switches_automatic_analysis_off() -> None:
    async with harness(structured=StaticStructuredFactory(), **CLOUD) as h:
        owner = h.owner()
        await _agree(h, owner)
        url = f"{API}/me/preferences"
        await h.client.put(url, json={"auto_analysis": True}, headers=owner.headers)
        await h.client.delete(CONSENT, headers=owner.headers)
        assert (await h.client.get(url, headers=owner.headers)).json()["auto_analysis"] is False
        assert await h.store.auto_subscribers(owner.account_id) == []


async def test_vision_consent_automatic_analysis_skips_users_without_a_covering_consent() -> None:
    async with harness(structured=StaticStructuredFactory(), **CLOUD) as h:
        owner = h.owner()
        rig = await _stuck(h, owner)
        await _store_opt_in(h, owner)  # opted in before consent existed: not grandfathered
        state = rig.sim.snapshot().model_copy(
            update={"activity": MowerActivity.ERROR, "observed_at": utc_now()}
        )
        assert await _auto(h).trigger(state, "1201") is Skip.NO_CONSENT
        assert h.factory.calls == [] and h.store.proposals == {}
        await _agree(h, owner)
        # the provider changes: the old consent no longer covers the model
        await h.client.put(
            PROVIDER, json={"model": "anthropic/claude-haiku-5-5"}, headers=owner.headers
        )
        assert await _auto(h).trigger(state, "1201") is Skip.NO_CONSENT
        await _agree(h, owner)
        started = await _auto(h).trigger(state, "1201")
        assert not isinstance(started, Skip)
        assert started.trigger.value == "error"
