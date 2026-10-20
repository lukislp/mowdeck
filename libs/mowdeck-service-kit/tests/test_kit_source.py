"""The source code address of the running build (AGPL-3.0 section 13): the X-Source-Code header."""

import pytest
from fastapi import APIRouter
from mowdeck_service_kit import NotFound
from mowdeck_service_kit.settings import ServiceSettings
from mowdeck_service_kit.source import (
    DEFAULT_SOURCE_URL,
    SOURCE_HEADER,
    release_ref,
    source_code_url,
)
from mowdeck_service_kit.testing import lifespan_client, make_test_app, make_test_settings

router = APIRouter()


@router.get("/thing")
async def thing() -> dict[str, str]:
    return {"ok": "yes"}


@router.get("/missing")
async def missing() -> None:
    raise NotFound("nothing here")


@pytest.mark.parametrize(
    ("version", "ref"),
    [
        ("1.2.3", "v1.2.3"),
        ("v0.4.0", "v0.4.0"),
        ("0.1.0-rc.1", "v0.1.0-rc.1"),
        (" 2.0.0 ", "v2.0.0"),
        ("dev", None),
        ("0.0.0-dev+abc", "v0.0.0-dev+abc"),
        ("", None),
        (None, None),
        ("1.2", None),
    ],
)
def test_release_ref(version: str | None, ref: str | None) -> None:
    assert release_ref(version) == ref


def test_a_release_links_to_its_tag() -> None:
    assert source_code_url(None, version="0.3.0") == f"{DEFAULT_SOURCE_URL}/tree/v0.3.0"


def test_a_development_build_links_to_the_repository() -> None:
    assert source_code_url(None) == DEFAULT_SOURCE_URL
    assert source_code_url("", version="dev") == DEFAULT_SOURCE_URL


def test_the_ref_wins_over_the_version() -> None:
    sha = "389d704b2a5c0d6c0f1e1a2b3c4d5e6f70809010"
    assert source_code_url(None, version="0.1.0-rc.1", ref=sha) == (
        f"{DEFAULT_SOURCE_URL}/tree/{sha}"
    )


def test_a_fork_points_to_its_own_repository() -> None:
    assert (
        source_code_url("https://github.com/someone/mowdeck-fork/", version="1.0.0")
        == "https://github.com/someone/mowdeck-fork/tree/v1.0.0"
    )
    assert (
        source_code_url("https://git.example.org/me/mowdeck.git", ref="main")
        == "https://git.example.org/me/mowdeck/tree/main"
    )


def test_a_template_takes_the_ref() -> None:
    template = "https://codeberg.org/me/mowdeck/src/tag/{ref}"
    assert source_code_url(template, version="1.0.0") == (
        "https://codeberg.org/me/mowdeck/src/tag/v1.0.0"
    )
    assert source_code_url(template) == "https://codeberg.org/me/mowdeck/src/tag/HEAD"


@pytest.mark.parametrize(
    "repository",
    ["javascript:alert(1)", "ftp://example.org/x", "https://example.org/a b", 'https://x/"y'],
)
def test_an_invalid_repository_falls_back_to_the_official_one(repository: str) -> None:
    assert source_code_url(repository, version="1.0.0") == f"{DEFAULT_SOURCE_URL}/tree/v1.0.0"


@pytest.mark.parametrize("ref", ["../../evil", "-rf", "a b", "x" * 200])
def test_an_invalid_ref_is_ignored(ref: str) -> None:
    assert source_code_url(None, version="1.0.0", ref=ref) == f"{DEFAULT_SOURCE_URL}/tree/v1.0.0"


def test_settings_read_the_build_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("MOWDECK_VERSION", "0.5.1")
    monkeypatch.setenv("MOWDECK_SOURCE_URL", "https://github.com/someone/fork")
    settings = ServiceSettings.from_env()
    assert settings.version == "0.5.1"
    assert settings.source_code_url == "https://github.com/someone/fork/tree/v0.5.1"


def test_settings_default_to_the_official_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MOWDECK_VERSION", "MOWDECK_SOURCE_URL", "MOWDECK_SOURCE_REF"):
        monkeypatch.delenv(name, raising=False)
    assert make_test_settings().source_code_url == DEFAULT_SOURCE_URL


async def test_every_response_carries_the_source_header() -> None:
    settings = make_test_settings(version="0.2.0", source_ref="v0.2.0")
    async with lifespan_client(make_test_app(routers=[router], settings=settings)) as client:
        expected = f"{DEFAULT_SOURCE_URL}/tree/v0.2.0"
        for path in ("/api/v1/thing", "/api/v1/missing", "/healthz", "/readyz", "/nope"):
            response = await client.get(path)
            assert response.headers[SOURCE_HEADER] == expected, path
        assert (await client.get("/api/v1/missing")).status_code == 404
