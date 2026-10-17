# SPDX-License-Identifier: Apache-2.0
"""Image build step: place agora-python-server-sdk and the native SDK without running their
installers.

``pip install agora-python-server-sdk`` would run the package's ``setup.py``, which downloads
the native SDK without a hash check and with TLS verification switched off. This script does
the work itself, from pinned URLs with pinned sizes and SHA-256 digests, over verified TLS:

1. download the wrapper's sdist and copy only its ``agora/`` package (pure Python, MIT) to
   ``<target>/agora``; ``setup.py`` is never executed;
2. download the native SDK archive and extract its ``agora_sdk/`` directory to
   ``<target>/agora/agora_sdk`` (where the wrapper loads it from);
3. write ``<target>/agora/version.txt`` with exactly the archive URL the wrapper expects, so its
   import-time check finds the SDK and never downloads.

Every archive member is checked to stay inside its target. Run with ``python -I`` (no
user site, no current directory on the path).

    python -I install_sdk.py /opt/agora-sdk
"""

import hashlib
import platform
import shutil
import ssl
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

WRAPPER = {
    "url": "https://files.pythonhosted.org/packages/33/4d/715e0c325e42ff20484729c1503ac5d89097d31abd2b9db27f48bcac6eec/agora_python_server_sdk-2.4.9.tar.gz",
    "sha256": "3f9453ce4730c1b9db86e56ea9b96713ffc576060b357d9bf0de8f65b473031e",
    "size": 94_571,
}
NATIVE = {
    "url": "https://download.agora.io/sdk/release/agora_rtc_sdk_x86_64-linux-gnu-v4.4.32.174_28020_SERVER_20260601_1240_1144448_20251021_1427-3a.zip",
    "sha256": "66f8a30dc75bb4f50a0597b52445c2b197bbc1970cd632262a24cdffe39b9130",
    "size": 33_340_805,
}
SDIST_ROOT = "agora_python_server_sdk-2.4.9"


def fetch(spec: dict[str, object], target: Path) -> None:
    """Download ``spec["url"]`` over verified TLS and check size and SHA-256."""
    url = str(spec["url"])
    context = ssl.create_default_context()  # certificate and host name verification
    digest = hashlib.sha256()
    size = 0
    with (
        urllib.request.urlopen(url, timeout=120, context=context) as response,  # noqa: S310
        target.open("wb") as out,
    ):
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
            if size > int(str(spec["size"])):
                break  # larger than pinned: refused below without writing the rest
            out.write(chunk)
    if size != spec["size"] or digest.hexdigest() != spec["sha256"]:
        msg = (
            f"{url}: got {size} bytes with sha256 {digest.hexdigest()}, "
            f"expected {spec['size']} bytes with sha256 {spec['sha256']}"
        )
        raise SystemExit(msg)
    print(f"verified {url.rsplit('/', 1)[-1]} ({size} bytes, sha256 {spec['sha256']})")


def _inside(name: str, prefix: str) -> PurePosixPath | None:
    """The member's path below ``prefix``, or None when it is outside or unsafe."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        msg = f"unsafe archive member {name!r}"
        raise SystemExit(msg)
    try:
        return path.relative_to(prefix)
    except ValueError:
        return None


def install_wrapper(archive: Path, package: Path) -> None:
    """Copy the sdist's ``agora/`` package, nothing else (no setup.py, no egg-info)."""
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            relative = _inside(member.name, f"{SDIST_ROOT}/agora")
            if relative is None or not member.isfile():
                continue
            if relative.suffix != ".py":
                continue
            destination = package / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)
    if not (package / "__init__.py").is_file() or not (package / "rtc" / "__init__.py").is_file():
        msg = "the wrapper sdist has no agora package"
        raise SystemExit(msg)


def install_native(archive: Path, package: Path) -> None:
    """Extract ``agora_sdk/`` to ``<package>/agora_sdk``."""
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            relative = _inside(info.filename, "agora_sdk")
            if relative is None or info.is_dir():
                continue
            destination = package / "agora_sdk" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)
    for library in ("libagora_rtc_sdk.so", "libaosl.so"):
        if not (package / "agora_sdk" / library).is_file():
            msg = f"the native SDK archive has no agora_sdk/{library}"
            raise SystemExit(msg)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: install_sdk.py <target directory>")
    if platform.machine() != "x86_64":
        msg = f"the pinned native SDK is for x86_64 Linux, not {platform.machine()}"
        raise SystemExit(msg)
    package = Path(sys.argv[1]) / "agora"
    if package.exists():
        raise SystemExit(f"{package} exists already")
    with tempfile.TemporaryDirectory() as work:
        wrapper, native = Path(work) / "wrapper.tar.gz", Path(work) / "native.zip"
        fetch(WRAPPER, wrapper)
        fetch(NATIVE, native)
        install_wrapper(wrapper, package)
        install_native(native, package)
    # Exactly the URL, no newline: the wrapper compares its first line with it.
    (package / "version.txt").write_text(str(NATIVE["url"]), "utf-8")
    print(f"installed the Agora SDK wrapper 2.4.9 and the native SDK in {package}")


if __name__ == "__main__":
    main()
