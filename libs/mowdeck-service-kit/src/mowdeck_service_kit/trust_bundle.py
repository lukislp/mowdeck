# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Write the trust store of the HTTP clients for internal TLS (docs/ops/internal-tls.md).

    python -m mowdeck_service_kit.trust_bundle --ca /etc/mowdeck/tls/ca.crt \\
        --out /etc/mowdeck/trust/ca-bundle.pem

The bundle is the image's public roots followed by the installation's CA; ``SSL_CERT_FILE``
points to it, so calls to the internet keep working while the internal hops are verified. The
Helm chart runs it as the init container ``trust-bundle``; Compose's ``tls-init`` writes the same
bundle itself. Exit code 0 when the bundle was written, 1 otherwise.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from mowdeck_service_kit.tls import write_bundle


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m mowdeck_service_kit.trust_bundle``."""
    parser = argparse.ArgumentParser(prog="mowdeck_service_kit.trust_bundle")
    parser.add_argument("--ca", required=True, type=Path, help="CA certificate(s), PEM")
    parser.add_argument("--out", required=True, type=Path, help="bundle to write (SSL_CERT_FILE)")
    args = parser.parse_args(argv)
    try:
        write_bundle(args.ca, args.out)
    except (OSError, ValueError) as exc:
        print(f"trust-bundle: {exc}", file=sys.stderr)
        return 1
    print(f"trust-bundle: wrote {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
