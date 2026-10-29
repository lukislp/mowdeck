"""Public demo (ADR 0068): static weather, no development endpoints, a schedule cap."""

from scheduler_support import make_settings

from scheduler.adapters.api.demo import DEMO_POLICY
from scheduler.settings import DEMO_MAX_SCHEDULES


def test_demo_mode_never_asks_a_weather_service() -> None:
    settings = make_settings(
        demo_mode=True,
        scheduler_weather_provider="open_meteo",
        scheduler_dev_endpoints=True,
        scheduler_max_schedules_per_account=1000,
    )
    assert settings.scheduler_weather_provider == "static"
    assert settings.scheduler_dev_endpoints is False
    assert settings.scheduler_max_schedules_per_account == DEMO_MAX_SCHEDULES
    assert make_settings(scheduler_weather_provider="open_meteo").scheduler_weather_provider == (
        "open_meteo"
    )


def test_the_policy_refuses_only_the_development_endpoints() -> None:
    assert DEMO_POLICY.refusal("PUT", "/api/v1/scheduler/dev/weather")
    assert DEMO_POLICY.refusal("POST", "/api/v1/scheduler/schedules") is None
    assert DEMO_POLICY.refusal("POST", "/api/v1/scheduler/schedules/abc/run-now") is None
