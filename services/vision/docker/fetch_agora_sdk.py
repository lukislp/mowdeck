"""Download the Agora Web SDK for the capture worker image (ADR 0035), checked by its hash.

``python3 fetch_agora_sdk.py <version> <sha512-base64> <out_dir>`` downloads
``agora-rtc-sdk-ng-<version>.tgz`` from the npm registry, compares it with the npm integrity
hash (``sha512-...`` in ``pnpm-lock.yaml``, the same version the web app ships) and writes the
self-contained UMD bundle ``AgoraRTC_N-production.js`` to ``<out_dir>``. Standard library only.
"""

import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

REGISTRY = "https://registry.npmjs.org/agora-rtc-sdk-ng/-/agora-rtc-sdk-ng-{version}.tgz"
MEMBER = "package/AgoraRTC_N-production.js"


def main(version: str, sha512: str, out_dir: str) -> None:
    """Download, verify and extract."""
    url = REGISTRY.format(version=version)
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
        archive = response.read()
    digest = base64.b64encode(hashlib.sha512(archive).digest()).decode()
    if digest != sha512:
        sys.exit(f"agora-rtc-sdk-ng {version}: sha512 mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile(MEMBER)
        if member is None:
            sys.exit(f"{MEMBER} missing in the package")
        data = member.read()
    target = Path(out_dir) / "AgoraRTC_N-production.js"
    target.write_bytes(data)
    print(f"agora-rtc-sdk-ng {version}: {len(data)} bytes to {target}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: fetch_agora_sdk.py <version> <sha512-base64> <out_dir>")
    main(sys.argv[1], sys.argv[2], sys.argv[3])
