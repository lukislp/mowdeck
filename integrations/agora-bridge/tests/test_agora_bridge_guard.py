# SPDX-License-Identifier: Apache-2.0
"""The safe import of the Agora wrapper: no download, TLS verification restored."""

import importlib.util
import ssl
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from agora_bridge import guard
from agora_bridge.guard import (
    NATIVE_SDK_URL,
    assert_tls_verification,
    check_installation,
    import_agora,
    keep_tls_verification,
    no_downloads,
    tls_verification_intact,
)
from agora_bridge.sdk import SdkLoadError

MODULES = (
    "agora_base",
    "agora_service",
    "rtc_connection",
    "rtc_connection_observer",
    "local_user_observer",
    "video_encoded_frame_observer",
)

# What the wrapper's agora/__init__.py does on import, reduced: disable TLS verification for the
# process and download the native SDK unless version.txt and the library are in place.
FAKE_INIT = """
import os, ssl
from urllib import request
ssl._create_default_https_context = ssl._create_unverified_context
_root = os.path.dirname(os.path.abspath(__file__))
_url = {url!r}
with open(os.path.join(_root, "version.txt")) as f:
    _named = f.readline()
if _named != _url or not os.path.exists(os.path.join(_root, "agora_sdk", "libagora_rtc_sdk.so")):
    request.urlretrieve(_url, os.path.join(_root, "agora_rtc_sdk.zip"))
"""


def _fake_wrapper(root: Path, *, version: str = NATIVE_SDK_URL, library: bool = True) -> Path:
    package = root / "agora"
    (package / "rtc").mkdir(parents=True)
    (package / "agora_sdk").mkdir()
    (package / "__init__.py").write_text(FAKE_INIT.format(url=NATIVE_SDK_URL), "utf-8")
    (package / "rtc" / "__init__.py").write_text(
        "import ssl\nssl._create_default_https_context = ssl._create_unverified_context\n",
        "utf-8",
    )
    for name in MODULES:
        (package / "rtc" / f"{name}.py").write_text(f"NAME = {name!r}\n", "utf-8")
    (package / "version.txt").write_text(version, "utf-8", newline="")
    (package / "agora_sdk" / "libaosl.so").write_bytes(b"")
    if library:
        (package / "agora_sdk" / "libagora_rtc_sdk.so").write_bytes(b"")
    return package


@pytest.fixture
def clean_modules() -> Iterator[None]:
    def drop() -> None:
        for name in [n for n in sys.modules if n == "agora" or n.startswith("agora.")]:
            del sys.modules[name]

    drop()
    saved = ssl._create_default_https_context
    try:
        yield
    finally:
        drop()
        ssl._create_default_https_context = saved


def test_the_pins_are_the_ones_of_the_reviewed_release() -> None:
    assert guard.WRAPPER_VERSION == "2.4.9"
    assert guard.WRAPPER_SHA256 == (
        "3f9453ce4730c1b9db86e56ea9b96713ffc576060b357d9bf0de8f65b473031e"
    )
    assert guard.NATIVE_SDK_SHA256 == (
        "66f8a30dc75bb4f50a0597b52445c2b197bbc1970cd632262a24cdffe39b9130"
    )
    assert guard.NATIVE_SDK_SIZE == 33_340_805
    assert NATIVE_SDK_URL.startswith("https://download.agora.io/sdk/release/")


def test_the_image_build_uses_the_same_pins() -> None:
    root = Path(__file__).parents[1]
    installer = (root / "docker" / "install_sdk.py").read_text("utf-8")
    for pin in (NATIVE_SDK_URL, guard.NATIVE_SDK_SHA256, guard.WRAPPER_SHA256):
        assert pin in installer
    assert f"{guard.NATIVE_SDK_SIZE:_}" in installer
    assert f"agora_python_server_sdk-{guard.WRAPPER_VERSION}.tar.gz" in installer
    dockerfile = (root / "Dockerfile").read_text("utf-8")
    assert "install_sdk.py" in dockerfile
    assert "pip install" not in dockerfile  # the wrapper's setup.py never runs
    assert "--network=none" in dockerfile  # the import is proven offline


def test_an_installation_that_would_download_is_refused(tmp_path: Path) -> None:
    good = _fake_wrapper(tmp_path / "good")
    check_installation(good)
    with pytest.raises(SdkLoadError, match=r"version\.txt"):
        check_installation(_fake_wrapper(tmp_path / "newline", version=NATIVE_SDK_URL + "\n"))
    with pytest.raises(SdkLoadError, match=r"version\.txt"):
        check_installation(_fake_wrapper(tmp_path / "other", version="https://example.org/x"))
    with pytest.raises(SdkLoadError, match=r"libagora_rtc_sdk\.so"):
        check_installation(_fake_wrapper(tmp_path / "nolib", library=False))


def test_downloads_are_blocked_inside_the_guard() -> None:
    with no_downloads(), pytest.raises(guard.DownloadBlockedError):
        urllib.request.urlretrieve("https://example.org/sdk.zip", "x")
    assert urllib.request.urlretrieve.__name__ == "urlretrieve"


def test_tls_verification_is_restored_after_a_block_that_disables_it(
    clean_modules: None,
) -> None:
    assert tls_verification_intact()
    with keep_tls_verification():
        ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[assignment]
        assert not tls_verification_intact()
        with pytest.raises(SdkLoadError, match="TLS"):
            assert_tls_verification()
    assert tls_verification_intact()


def test_the_import_keeps_tls_verification_and_never_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_modules: None
) -> None:
    _fake_wrapper(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    modules = import_agora()
    assert sorted(modules) == sorted(MODULES)
    assert tls_verification_intact()  # the fake wrapper turned it off on import


def test_an_import_that_tries_to_download_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_modules: None
) -> None:
    package = _fake_wrapper(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(guard, "check_installation", lambda root: None)  # pretend it passed
    (package / "agora_sdk" / "libagora_rtc_sdk.so").unlink()
    with pytest.raises(SdkLoadError, match="download"):
        import_agora()
    assert tls_verification_intact()
    assert not (package / "agora_rtc_sdk.zip").exists()


def test_a_missing_wrapper_is_a_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(SdkLoadError, match="not installed"):
        import_agora()
