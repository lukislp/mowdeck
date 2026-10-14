"""Init container: wait until the service database is at the newest migration of this image.

Usage: python wait_for_schema.py <package>.adapters.db.migrate

The module's alembic_config() names the packaged migrations. The database URL comes from
MOWDECK_DATABASE_URL_FILE (or MOWDECK_DATABASE_URL). The database counts as ready when it carries
every head revision of the image, or a revision the image does not know (a newer release migrated
it; migrations stay backward compatible, so an older image may run). Gives up after
WAIT_TIMEOUT_SECONDS (default 900) with exit code 1, so Kubernetes restarts the pod.
"""

import asyncio
import importlib
import os
import sys
import time

import asyncpg
from alembic.script import ScriptDirectory


def database_url() -> str:
    path = os.environ.get("MOWDECK_DATABASE_URL_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            url = handle.read().strip()
    else:
        url = os.environ["MOWDECK_DATABASE_URL"]
    scheme, sep, rest = url.partition("://")
    return f"postgresql://{rest}" if sep and scheme.startswith("postgres") else url


async def applied_revisions(url: str) -> set[str]:
    connection = await asyncpg.connect(url, timeout=5)
    try:
        rows = await connection.fetch("SELECT version_num FROM alembic_version")
    except asyncpg.UndefinedTableError:
        return set()
    finally:
        await connection.close()
    return {row[0] for row in rows}


def main() -> int:
    module = importlib.import_module(sys.argv[1])
    script = ScriptDirectory.from_config(module.alembic_config())
    heads = set(script.get_heads())
    known = {revision.revision for revision in script.walk_revisions()}
    deadline = time.monotonic() + float(os.environ.get("WAIT_TIMEOUT_SECONDS", "900"))
    while True:
        try:
            applied = asyncio.run(applied_revisions(database_url()))
            if heads <= applied:
                print(f"wait-for-schema: database at {sorted(applied)}", flush=True)
                return 0
            if applied - known:
                print(f"wait-for-schema: database at newer {sorted(applied - known)}", flush=True)
                return 0
            message = f"database at {sorted(applied) or 'no revision'}, waiting for {sorted(heads)}"
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            message = f"database not reachable yet: {exc.__class__.__name__}: {exc}"
        if time.monotonic() > deadline:
            print(f"wait-for-schema: giving up, {message}", flush=True)
            return 1
        print(f"wait-for-schema: {message}", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
