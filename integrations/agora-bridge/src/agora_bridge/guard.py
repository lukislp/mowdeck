# SPDX-License-Identifier: Apache-2.0
"""Safe import of the Agora wrapper (``agora-python-server-sdk``).

The wrapper (2.4.9) does two things on import that the bridge does not accept:

* it **downloads the native SDK** (``urllib.request.urlretrieve``, no hash check) unless
  ``agora/version.txt`` names the expected archive URL and ``agora/agora_sdk/libagora_rtc_sdk.so``
  exists, after deleting the library directory with ``rm -rf``;
* it **turns off TLS certificate verification for the whole process**
  (``ssl._create_default_https_context = ssl._create_unverified_context``).

:func:`import_agora` therefore checks the installed files *before* the import (and refuses to
import when the wrapper would download), blocks ``urlretrieve`` and ``urlopen`` while the import
runs, and restores TLS verification afterwards; :func:`assert_tls_verification` proves it. The
image build places the SDK so the check passes (Dockerfile); the bridge makes no HTTPS calls of
its own anyway.
"""

import contextlib
import importlib
import importlib.util
import ssl
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

from agora_bridge.sdk import SdkLoadError

NATIVE_SDK_URL = (
    "https://download.agora.io/sdk/release/agora_rtc_sdk_x86_64-linux-gnu-v4.4.32.174_28020_"
    "SERVER_20260601_1240_1144448_20251021_1427-3a.zip"
)
"""The archive agora-python-server-sdk 2.4.9 expects on x86_64 Linux (its ``linux_sdk``)."""
NATIVE_SDK_SHA256 = "66f8a30dc75bb4f50a0597b52445c2b197bbc1970cd632262a24cdffe39b9130"
NATIVE_SDK_SIZE = 33_340_805
WRAPPER_VERSION = "2.4.9"
WRAPPER_SHA256 = "3f9453ce4730c1b9db86e56ea9b96713ffc576060b357d9bf0de8f65b473031e"

_DEFAULT_HTTPS_CONTEXT = ssl.create_default_context
"""What ``ssl._create_default_https_context`` is in a Python nobody tampered with."""


class DownloadBlockedError(RuntimeError):
    """The wrapper tried to download something while it was imported."""


def tls_verification_intact() -> bool:
    """True when HTTPS in this process verifies certificates (the standard library's default)."""
    current: Any = ssl._create_default_https_context
    return current is _DEFAULT_HTTPS_CONTEXT


def assert_tls_verification() -> None:
    """Raise when TLS verification is off for the process."""
    if not tls_verification_intact():
        msg = "TLS certificate verification is disabled in this process"
        raise SdkLoadError(msg)


@contextlib.contextmanager
def no_downloads() -> Iterator[None]:
    """Block ``urllib.request.urlretrieve`` and ``urlopen`` while the block runs."""

    def refuse(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        msg = "the Agora SDK wrapper tried to download while it was imported"
        raise DownloadBlockedError(msg)

    saved = (urllib.request.urlretrieve, urllib.request.urlopen)
    urllib.request.urlretrieve = refuse
    urllib.request.urlopen = refuse
    try:
        yield
    finally:
        urllib.request.urlretrieve, urllib.request.urlopen = saved


@contextlib.contextmanager
def keep_tls_verification() -> Iterator[None]:
    """Restore ``ssl._create_default_https_context`` after the block, whatever it did."""
    saved = ssl._create_default_https_context
    try:
        yield
    finally:
        ssl._create_default_https_context = saved


def package_dir() -> Path:
    """Where the ``agora`` package is installed (without importing it)."""
    spec = importlib.util.find_spec("agora")
    if spec is None or not spec.submodule_search_locations:
        msg = "the Agora SDK wrapper (agora-python-server-sdk) is not installed"
        raise SdkLoadError(msg)
    return Path(next(iter(spec.submodule_search_locations)))


def check_installation(root: Path) -> None:
    """Raise unless the wrapper at ``root`` would import without downloading: ``version.txt``
    is exactly :data:`NATIVE_SDK_URL` (no newline) and the native libraries are there."""
    version = root / "version.txt"
    try:
        # The wrapper compares its first line, newline included, with the URL.
        with version.open(encoding="utf-8") as file:
            named = file.readline()
    except OSError:
        named = ""
    if named != NATIVE_SDK_URL:
        msg = "agora/version.txt does not name the pinned native SDK: the import would download"
        raise SdkLoadError(msg)
    for library in ("libagora_rtc_sdk.so", "libaosl.so"):
        if not (root / "agora_sdk" / library).is_file():
            msg = f"agora/agora_sdk/{library} is missing: the import would download"
            raise SdkLoadError(msg)


def import_agora() -> dict[str, ModuleType]:
    """Import the wrapper's modules the bridge uses, offline and with TLS verification kept.

    Raises :class:`~agora_bridge.sdk.SdkLoadError` when the files are not in place, the import
    tried to download, the native library did not load or TLS verification could not be
    restored.
    """
    check_installation(package_dir())
    names = (
        "agora.rtc.agora_base",
        "agora.rtc.agora_service",
        "agora.rtc.rtc_connection",
        "agora.rtc.rtc_connection_observer",
        "agora.rtc.local_user_observer",
        "agora.rtc.video_encoded_frame_observer",
    )
    modules: dict[str, ModuleType] = {}
    with keep_tls_verification(), no_downloads():
        try:
            for name in names:
                modules[name.rsplit(".", 1)[-1]] = importlib.import_module(name)
        except DownloadBlockedError as exc:
            raise SdkLoadError(str(exc)) from exc
        except SystemExit as exc:  # the wrapper calls sys.exit(1) when a library does not load
            msg = "the native Agora SDK did not load"
            raise SdkLoadError(msg) from exc
        except (ImportError, OSError) as exc:
            msg = f"the Agora SDK wrapper did not import: {type(exc).__name__}"
            raise SdkLoadError(msg) from exc
    assert_tls_verification()
    return modules
