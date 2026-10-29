"""Public demo (ADR 0068): the built-in fake model and no own provider."""

from vision_support import make_settings

from vision.adapters.api.demo import DEMO_POLICY
from vision.settings import DEMO_MODEL

API = "/api/v1/vision"


def test_demo_mode_uses_the_fake_model_whatever_is_configured() -> None:
    settings = make_settings(
        demo_mode=True,
        vision_default_enabled=False,
        vision_default_model="openai/gpt-5-mini",
        vision_default_api_base="https://llm.example.org",
        vision_default_api_key="sk-secret",
        vision_user_api_base_policy="any",
    )
    assert settings.vision_default_enabled is True
    assert settings.vision_default_model == DEMO_MODEL
    assert settings.vision_default_api_base is None
    assert settings.vision_default_api_key is None
    assert settings.vision_allow_fake_provider is True
    assert settings.vision_user_api_base_policy == "none"
    normal = make_settings(vision_default_model="openai/gpt-5-mini")
    assert normal.vision_default_model == "openai/gpt-5-mini"


def test_the_policy_refuses_own_providers_and_uploads_only() -> None:
    assert DEMO_POLICY.refusal("PUT", f"{API}/me/provider")
    assert DEMO_POLICY.refusal("DELETE", f"{API}/me/provider")
    assert DEMO_POLICY.refusal("POST", f"{API}/me/provider/test")
    assert DEMO_POLICY.refusal("POST", f"{API}/mowers/abc/recover/upload")
    assert DEMO_POLICY.refusal("POST", f"{API}/mowers/abc/recover") is None
    assert DEMO_POLICY.refusal("POST", f"{API}/proposals/abc/approve") is None
    assert DEMO_POLICY.refusal("PUT", f"{API}/me/preferences") is None
