# SPDX-License-Identifier: Apache-2.0
"""``python -m agora_bridge``: one bridge process on stdin and stdout.

* ``python -m agora_bridge``: speak the bridge protocol (one channel, then exit);
* ``python -m agora_bridge --check``: load the SDK offline, verify that TLS verification is
  intact, print one JSON line and exit 0 (3 when the SDK does not load). The image build and the
  server's start run it.

stdout carries only protocol records: before anything else, the protocol stream gets a private
copy of file descriptor 1 and descriptor 1 is pointed at stderr, so whatever the SDK or the
wrapper prints (both do) lands in the log and can never corrupt a record. Arguments and the
environment never carry a secret; credentials come on stdin only.
"""

import argparse
import json
import logging
import os
import sys
from typing import BinaryIO

from agora_bridge import __version__
from agora_bridge.bridge import EXIT_FAILURE, EXIT_OK, EXIT_SDK_LOAD, Bridge
from agora_bridge.guard import tls_verification_intact
from agora_bridge.protocol import encode_event
from agora_bridge.sdk import Sdk, SdkLoadError


def _protocol_stdout() -> BinaryIO:
    """A private stream on the real stdout; fd 1 and ``sys.stdout`` go to stderr."""
    sys.stdout.flush()
    # Buffered: a BufferedWriter writes every byte (a raw write may be short).
    stream = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return stream


def _quiet_import_paths(record: logging.LogRecord) -> bool:
    message = str(record.msg)
    return not message.startswith(("sdk_library_dir:", "zip_path:"))


def _load_sdk() -> Sdk:
    from agora_bridge.agora_sdk import AgoraSdk

    return AgoraSdk.load()


def _check(out: BinaryIO) -> int:
    try:
        sdk = _load_sdk()
    except SdkLoadError as exc:
        out.write(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")
        return EXIT_SDK_LOAD
    result = {
        "ok": tls_verification_intact(),
        "bridge": __version__,
        "sdk": sdk.version,
        "tls_verification": tls_verification_intact(),
    }
    out.write(json.dumps(result).encode() + b"\n")
    sdk.leave()
    return EXIT_OK if result["ok"] else EXIT_SDK_LOAD


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agora-bridge", description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="load the SDK offline and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("AGORA_BRIDGE_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # The wrapper logs its internals (including callback arguments) at DEBUG and INFO, and two
    # paths at ERROR on every import even when nothing is wrong.
    wrapper = logging.getLogger("agora")
    wrapper.setLevel(logging.WARNING)
    wrapper.addFilter(_quiet_import_paths)
    out = _protocol_stdout()
    if args.check:
        return _check(out)
    try:
        sdk = _load_sdk()
    except SdkLoadError as exc:
        logging.getLogger("agora_bridge").error("the SDK did not load: %s", exc)
        out.write(_load_error(str(exc)))
        return EXIT_SDK_LOAD
    try:
        return Bridge(sdk, out).run(sys.stdin)
    except Exception:
        logging.getLogger("agora_bridge").exception("bridge failed")
        return EXIT_FAILURE


def _load_error(detail: str) -> bytes:
    return encode_event("error", code="sdk_load_failed", detail=detail[:200])


if __name__ == "__main__":
    sys.exit(main())
