"""Command line: ``python -m mowdeck_authz bootstrap``.

Creates the store (if missing) and writes the packaged model (if changed), with the same rules
as the ``authz-bootstrap`` Compose job. Connection settings come from ``MOWDECK_OPENFGA_*``.
Prints the result as JSON. Useful where the Compose job is not available (Helm hook, scripts).
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from mowdeck_authz._client import create_client
from mowdeck_authz.bootstrap import bootstrap_model
from mowdeck_authz.model import load_model
from mowdeck_authz.settings import AuthzSettings


async def _bootstrap(model_path: str | None, store_name: str | None) -> int:
    settings = AuthzSettings.from_env()
    client = create_client(settings)
    try:
        result = await bootstrap_model(
            client,
            model=load_model(model_path),
            store_name=store_name or settings.store_name,
        )
    finally:
        await client.close()
    print(json.dumps(asdict(result)))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(prog="python -m mowdeck_authz")
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser("bootstrap", help="create the store and write the model")
    boot.add_argument("--model", help="model JSON file (default: the packaged model)")
    boot.add_argument("--store-name", help="store name (default: MOWDECK_OPENFGA_STORE_NAME)")
    args = parser.parse_args(argv)
    return asyncio.run(_bootstrap(args.model, args.store_name))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
