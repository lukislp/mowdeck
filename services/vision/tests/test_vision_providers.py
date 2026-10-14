"""Provider settings: per-user providers with encrypted keys, the installation default with
plan gating, the resolution order, the provider test, and the fake provider."""

import base64

import pytest
from mowdeck_contracts.v1 import VisionTask
from mowdeck_service_kit.testing import FakeEntitlements
from vision_support import API, SECRETS_KEY, harness

from vision.adapters.crypto import AesGcmSecretBox, parse_key
from vision.adapters.providers.factory import DefaultProviderFactory
from vision.adapters.providers.fake import FakeVisionProvider
from vision.adapters.providers.llm import LiteLlmProvider
from vision.domain.context import DockHint, MapExcerpt, Motion, SceneContext
from vision.domain.errors import ProviderFailed
from vision.domain.models import ProposalState, ProviderConfig, RiskFlag, Situation

ME = f"{API}/me/provider"


def test_vision_crypto_round_trip_and_binding() -> None:
    box = AesGcmSecretBox(parse_key(SECRETS_KEY))
    blob = box.encrypt(b"sk-ant-secret", "vision:a:u:api_key")
    assert b"sk-ant-secret" not in blob
    assert box.decrypt(blob, "vision:a:u:api_key") == b"sk-ant-secret"
    assert box.encrypt(b"x", "aad") != box.encrypt(b"x", "aad"), "fresh nonce every time"
    with pytest.raises(ValueError, match="cannot decrypt"):
        box.decrypt(blob, "vision:a:other:api_key")
    other = AesGcmSecretBox(parse_key("00" * 32))
    with pytest.raises(ValueError, match="cannot decrypt"):
        other.decrypt(blob, "vision:a:u:api_key")
    assert parse_key(base64.urlsafe_b64encode(bytes(32)).decode().rstrip("=")) == bytes(32)
    for bad in ("short", "zz" * 32, base64.b64encode(bytes(16)).decode()):
        with pytest.raises(ValueError, match="SECRETS_KEY"):
            parse_key(bad)


async def test_vision_providers_put_get_test_delete_with_write_only_key() -> None:
    async with harness(vision_allow_fake_provider=True) as h:
        owner = h.owner()
        empty = (await h.client.get(ME, headers=owner.headers)).json()
        assert empty["configured"] is False and empty["effective"] == "hosted"
        assert empty["hosted_model"] == "fake/recover" and empty["secrets_available"] is True
        saved = await h.client.put(
            ME,
            json={
                "model": "anthropic/claude-sonnet-5-5",
                "api_key": "sk-ant-very-secret",
                "headers": {"X-Proxy": "token-123"},
            },
            headers=owner.headers,
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["api_key_set"] is True and body["header_names"] == ["X-Proxy"]
        assert body["effective"] == "own" and body["family"] == "anthropic"
        assert "sk-ant-very-secret" not in saved.text and "token-123" not in saved.text
        stored = h.store.providers[(owner.principal.subject, owner.account_id)]
        assert stored.api_key_ciphertext and b"very-secret" not in stored.api_key_ciphertext
        # Omitting the key keeps it; an empty string deletes it.
        kept = await h.client.put(
            ME, json={"model": "openai/gpt-5-mini", "enabled": True}, headers=owner.headers
        )
        assert kept.json()["api_key_set"] is True
        config = await h.service.providers.resolve(owner.principal)
        assert (config.model, config.api_key, config.hosted) == (
            "openai/gpt-5-mini",
            "sk-ant-very-secret",
            False,
        )
        assert config.headers == {"X-Proxy": "token-123"}
        cleared = await h.client.put(
            ME,
            json={"model": "fake/recover", "api_key": "", "headers": {}},
            headers=owner.headers,
        )
        assert cleared.json()["api_key_set"] is False and cleared.json()["header_names"] == []
        tested = await h.client.post(f"{ME}/test", headers=owner.headers)
        assert tested.status_code == 200, tested.text
        result = tested.json()
        assert result["ok"] is True and result["model"] == "fake/recover"
        assert result["input_tokens"] == 1200 and result["cost_usd"] == pytest.approx(0.0039)
        after = (await h.client.get(ME, headers=owner.headers)).json()
        assert after["last_test"]["ok"] is True
        assert [r.kind for r in h.store.requests] == ["test"]
        assert (await h.client.delete(ME, headers=owner.headers)).status_code == 204
        assert (await h.client.delete(ME, headers=owner.headers)).status_code == 404
        assert (await h.client.post(f"{ME}/test", headers=owner.headers)).status_code == 409


async def test_vision_providers_validation_and_policies() -> None:
    async with harness(vision_user_api_base_policy="public") as h:
        owner = h.owner()

        async def put(**body: object) -> int:
            return (await h.client.put(ME, json=body, headers=owner.headers)).status_code

        assert await put(model="fake/recover") == 422, "the fake is for development only"
        assert await put(model="noslash") == 422
        assert await put(model="evil/model") == 422
        assert await put(model="ollama/llava", api_base="http://192.168.1.20:11434") == 422
        assert await put(model="ollama/llava", api_base="http://ollama:11434") == 422
        assert await put(model="openai/x", api_base="ftp://example.org") == 422
        assert await put(model="openai/x", api_base="https://u:p@example.org/v1") == 422
        assert await put(model="openai/x", api_base="https://llm.example.org/v1") == 200
        assert await put(model="openai/x", headers={"Bad Header": "x"}) == 422
        assert await put(model="openai/x", headers={"X": "a\nb"}) == 422
        assert await put(model="openai/x", headers={f"H{i}": "v" for i in range(9)}) == 422
    async with harness(vision_user_api_base_policy="none", vision_secrets_key=None) as h:
        owner = h.owner()
        refused = await h.client.put(
            ME, json={"model": "ollama/llava", "api_base": "http://x:1"}, headers=owner.headers
        )
        assert refused.status_code == 422
        no_secrets = await h.client.put(
            ME, json={"model": "openai/gpt-5-mini", "api_key": "sk"}, headers=owner.headers
        )
        assert no_secrets.status_code == 409
        assert no_secrets.json()["type"] == "urn:mowdeck:problem:vision-secrets-unavailable"
        keyless = await h.client.put(ME, json={"model": "ollama/llava"}, headers=owner.headers)
        assert keyless.status_code == 200 and keyless.json()["secrets_available"] is False


async def test_vision_providers_resolution_order_and_plan_gating() -> None:
    plans = FakeEntitlements(plan="home", features=["view", "control", "ai_recover"])
    async with harness(entitlements=plans, vision_allow_fake_provider=True) as h:
        owner = h.owner()
        listed = (await h.client.get(f"{API}/providers", headers=owner.headers)).json()
        assert listed["hosted_available"] is False and listed["hosted_model"] is None
        keys = [f["key"] for f in listed["families"]]
        assert keys == [
            "anthropic",
            "openai",
            "gemini",
            "mistral",
            "ollama",
            "openai_compatible",
            "fake",
        ]
        anthropic = listed["families"][0]
        assert anthropic["key_env"] == "ANTHROPIC_API_KEY" and anthropic["needs_key"] is True
        assert "anthropic/claude-sonnet-5-5" in anthropic["examples"]
        assert (await h.client.get(ME, headers=owner.headers)).json()["effective"] == "none"
        rig = await h.rig(owner)
        url = f"{API}/mowers/{rig.mower_id}/recover"
        assert (await h.client.post(url, json={}, headers=owner.headers)).status_code == 409
        # An own provider needs only ai_recover.
        await h.client.put(ME, json={"model": "fake/recover"}, headers=owner.headers)
        created = (await h.client.post(url, json={}, headers=owner.headers)).json()
        assert created["hosted"] is False and created["provider_model"] == "fake/recover"
        await h.until(created["id"], owner, ProposalState.PROPOSED)
        await h.client.post(f"{API}/proposals/{created['id']}/reject", headers=owner.headers)
        # A disabled own provider falls back to the hosted default when the plan allows it.
        await h.client.put(
            ME, json={"model": "fake/recover", "enabled": False}, headers=owner.headers
        )
        plans.features.append("ai_recover_hosted")
        view = (await h.client.get(ME, headers=owner.headers)).json()
        assert view["effective"] == "hosted" and view["hosted_available"] is True
        hosted = (await h.client.post(url, json={}, headers=owner.headers)).json()
        assert hosted["hosted"] is True


async def test_vision_providers_are_per_user_and_test_is_rate_limited() -> None:
    async with harness(vision_allow_fake_provider=True, vision_provider_tests_per_hour=2) as h:
        owner = h.owner()
        colleague = h.person(owner.account_id)
        await h.client.put(ME, json={"model": "fake/recover"}, headers=owner.headers)
        assert (await h.client.get(ME, headers=colleague.headers)).json()["configured"] is False
        assert (await h.client.delete(ME, headers=colleague.headers)).status_code == 404
        assert (owner.principal.subject, owner.account_id) in h.store.providers
        for _ in range(2):
            assert (await h.client.post(f"{ME}/test", headers=owner.headers)).status_code == 200
        limited = await h.client.post(f"{ME}/test", headers=owner.headers)
        assert limited.status_code == 429
        h.factory.fail = "the model refused the request: AuthenticationError"
        await h.client.put(ME, json={"model": "fake/people"}, headers=colleague.headers)
        failed = (await h.client.post(f"{ME}/test", headers=colleague.headers)).json()
        assert failed["ok"] is False and "AuthenticationError" in failed["detail"]


async def test_vision_providers_status_and_undecryptable_key() -> None:
    async with harness() as h:
        owner = h.owner()
        status = (await h.client.get(f"{API}/status", headers=owner.headers)).json()
        assert status == {
            "hosted_default_enabled": True,
            "hosted_default_model": "fake/recover",
            "secrets_available": True,
            "allow_fake_provider": False,
            "user_api_base_policy": "any",
        }
        assert (await h.client.get(f"{API}/status")).status_code == 401
        await h.client.put(
            ME, json={"model": "openai/gpt-5-mini", "api_key": "sk"}, headers=owner.headers
        )
        stored = h.store.providers[(owner.principal.subject, owner.account_id)]
        stored.api_key_ciphertext = b"\x00" * 40
        rig = await h.rig(owner)
        broken = await h.client.post(
            f"{API}/mowers/{rig.mower_id}/recover", json={}, headers=owner.headers
        )
        assert broken.status_code == 409 and "enter it again" in broken.json()["detail"]


async def test_vision_fake_provider_modes() -> None:
    motion = Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57)
    context = SceneContext(
        task=VisionTask.FIND_DOCK,
        motion=motion,
        map=MapExcerpt(
            radius_m=8, dock=DockHint(forward=-3.0, left=2.0, distance_m=3.6, bearing_deg=146.3)
        ),
    )
    recover = await FakeVisionProvider().analyse([], context, VisionTask.RECOVER)
    assert [s.linear for s in recover.result.proposal] == [-0.8, 0.0, 0.6]
    assert recover.usage.mode == "fake"
    dock = await FakeVisionProvider().analyse([], context, VisionTask.FIND_DOCK)
    turn, approach = dock.result.proposal
    assert turn.angular > 0 and turn.seconds == 3.0
    assert approach.linear == 0.5 and approach.seconds <= 3.0
    near = context.model_copy(
        update={
            "map": MapExcerpt(
                radius_m=8, dock=DockHint(forward=1, left=0, distance_m=1, bearing_deg=0)
            )
        }
    )
    assert (
        await FakeVisionProvider().analyse([], near, VisionTask.FIND_DOCK)
    ).result.situation is Situation.NEAR_DOCK
    no_map = context.model_copy(update={"map": None})
    assert (
        await FakeVisionProvider().analyse([], no_map, VisionTask.FIND_DOCK)
    ).result.proposal == []
    people = await FakeVisionProvider("fake/people").analyse([], context, VisionTask.RECOVER)
    assert people.result.risk == [RiskFlag.PEOPLE_VISIBLE]
    low = await FakeVisionProvider("fake/low_confidence").analyse([], context, VisionTask.RECOVER)
    assert low.result.confidence == 0.2
    empty = await FakeVisionProvider("fake/empty").analyse([], context, VisionTask.RECOVER)
    assert empty.result.proposal == []
    for mode in ("fake/fail", "fake/invalid"):
        with pytest.raises(ProviderFailed):
            await FakeVisionProvider(mode).analyse([], context, VisionTask.RECOVER)
    factory = DefaultProviderFactory()
    assert isinstance(factory.create(ProviderConfig("fake/x", "fake", True)), FakeVisionProvider)
    assert isinstance(
        factory.create(ProviderConfig("anthropic/claude-sonnet-5-5", "anthropic", True)),
        LiteLlmProvider,
    )
