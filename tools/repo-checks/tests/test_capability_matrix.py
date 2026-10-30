"""The capability matrix matches the providers' code; the renderer reads a fixture (ADR 0071)."""

import importlib.util
from pathlib import Path
from types import ModuleType

from mowdeck_contracts.v1 import (
    CapabilityProfile,
    ConnectionKind,
    CredentialField,
    CredentialFieldKind,
    ErrorLexiconInfo,
    MowerCapabilities,
    ProfileFeature,
    ProfileReport,
    ProviderDescriptor,
    SettingGroup,
    SettingSpec,
    SettingType,
)


def _generator(repo_root: Path) -> ModuleType:
    path = repo_root / "tools/repo-checks/scripts/capability_matrix.py"
    spec = importlib.util.spec_from_file_location("capability_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _descriptor(*, tested: bool = False) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider="acme",
        name="Acme Cloud",
        summary={"en": "Acme mowers through the Acme cloud."},
        fields=[
            CredentialField(
                key="email", kind=CredentialFieldKind.EMAIL, label={"en": "E-mail"}, identity=True
            ),
            CredentialField(
                key="password", kind=CredentialFieldKind.SECRET, label={"en": "Password"}
            ),
        ],
        tested_on_hardware=tested,
        warnings=[{"en": "Unofficial API."}],
    )


def _profile(**update: object) -> CapabilityProfile:
    values: dict[str, object] = {
        "family": "rover",
        "name": "Rover",
        "models": ["Rover 1"],
        "capabilities": MowerCapabilities(
            connection_kinds=frozenset({ConnectionKind.CLOUD}),
            blade_height_range_mm=(30, 60),
            settings=[
                SettingSpec(key="rain_detection", type=SettingType.BOOL, group=SettingGroup.WEATHER)
            ],
        ),
        "reports": frozenset({ProfileReport.POSITION}),
        "varies": frozenset({ProfileFeature.BLADE_HEIGHT.value, ProfileFeature.LIGHT.value}),
        "not_offered": frozenset({ProfileFeature.CAMERA.value}),
        "notes": [{"en": "Blade height only with the module."}],
    }
    values.update(update)
    return CapabilityProfile.model_validate(values)


def _provider(module: ModuleType, **update: object) -> object:
    values: dict[str, object] = {
        "descriptor": _descriptor(),
        "enabled_by_default": False,
        "profiles": [_profile()],
        "lexicon": ErrorLexiconInfo(
            vendor="acme", vendor_name="Acme", entries=12, unverified=2, locales=["de", "en"]
        ),
    }
    values.update(update)
    return module.Provider(**values)


def test_committed_page_is_fresh(repo_root: Path) -> None:
    assert _generator(repo_root).main(["--root", str(repo_root), "--check"]) == 0, (
        "docs/reference/capabilities.md is stale: run `uv run poe capability-matrix`"
    )


def test_page_is_in_the_nav(repo_root: Path) -> None:
    assert "reference/capabilities.md" in (repo_root / "mkdocs.yml").read_text(encoding="utf-8")


def test_every_installed_provider_declares_profiles(repo_root: Path) -> None:
    generator = _generator(repo_root)
    providers = generator.installed_providers()
    assert providers, "no cloud provider is installed in the workspace"
    for provider in providers:
        assert provider.profiles, f"{provider.descriptor.provider} declares no capability profiles"
        assert provider.lexicon is not None, f"{provider.descriptor.provider} has no error lexicon"
    assert generator.problems(providers) == []


def test_marks_follow_the_legend(repo_root: Path) -> None:
    generator = _generator(repo_root)
    profile = _profile()
    assert generator.mark(profile, ProfileFeature.TRANSPORT_CLOUD) == "yes"
    assert generator.mark(profile, ProfileFeature.BLADE_HEIGHT) == "varies: 30 to 60 mm"
    assert generator.mark(profile, ProfileFeature.LIGHT) == "varies"
    assert generator.mark(profile, ProfileFeature.CAMERA) == "not offered"
    assert generator.mark(profile, ProfileFeature.MANUAL_DRIVE) == "not yet"
    assert generator.mark(profile, ProfileReport.POSITION) == "yes"
    assert generator.mark(profile, "rain_detection") == "yes"


def test_renders_a_fixture_provider(tmp_path: Path, repo_root: Path) -> None:
    generator = _generator(repo_root)
    page = generator.render([_provider(generator)])
    assert page.startswith(generator.HEADER)
    assert (
        "| [Acme Cloud](#acme-cloud) | Rover | Vendor cloud | 12 codes (de, en), 2 unverified"
        in (page)
    )
    assert "## Acme Cloud" in page
    assert '!!! note "Not tested on real hardware"' in page
    assert "!!! warning\n    Unofficial API." in page
    assert "| Blade height | varies: 30 to 60 mm |" in page
    assert "| Live camera | not offered |" in page
    assert "| Manual driving (joystick) | not yet |" in page
    assert "| Rain detection | yes |" in page
    assert "- Blade height only with the module." in page
    empty = generator.render([_provider(generator, profiles=None)])
    assert "This provider declares no capability profiles yet." in empty


def test_flags_declarations_it_cannot_show(repo_root: Path) -> None:
    generator = _generator(repo_root)
    tested = _provider(generator, profiles=[_profile(tested_on_hardware=True)])
    twice = _provider(generator, profiles=[_profile(), _profile()])
    assert generator.problems([tested]) == [
        "acme/rover: tested on hardware, but the provider's descriptor says it is not"
    ]
    assert generator.problems([twice]) == ["acme: family 'rover' is declared twice"]
    fine = _provider(
        generator, descriptor=_descriptor(tested=True), profiles=[_profile(tested_on_hardware=True)]
    )
    assert generator.problems([fine]) == []
