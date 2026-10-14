"""Store and model resolution, and the idempotent model bootstrap.

Rule shared with the ``authz-bootstrap`` Compose job (``infra/openfga/bootstrap``): the store is
found by **name**; with several stores of that name the oldest wins. Services therefore need no
store or model id in their configuration; ``MOWDECK_OPENFGA_STORE_ID`` and
``MOWDECK_OPENFGA_MODEL_ID`` only pin them when an operator wants to.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from openfga_sdk import CreateStoreRequest
from openfga_sdk.models.write_authorization_model_request import (
    WriteAuthorizationModelRequest,
)

from mowdeck_authz._client import FgaClient, to_json
from mowdeck_authz.errors import ModelNotFoundError, StoreNotFoundError
from mowdeck_authz.model import load_model, models_equal
from mowdeck_authz.settings import DEFAULT_STORE_NAME

_log = logging.getLogger("mowdeck_authz.bootstrap")

_PAGE_SIZE = 50
_MAX_PAGES = 20


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Outcome of :func:`bootstrap_model`."""

    store_id: str
    model_id: str
    store_created: bool
    model_written: bool


async def find_store_id(client: FgaClient, name: str = DEFAULT_STORE_NAME) -> str | None:
    """Return the id of the oldest store named ``name``, or None."""
    matches: list[tuple[str, str]] = []
    token: str | None = None
    for _ in range(_MAX_PAGES):
        options: dict[str, Any] = {"name": name, "page_size": _PAGE_SIZE}
        if token:
            options["continuation_token"] = token
        response = await client.list_stores(options)
        for store in response.stores or []:
            if store.name == name and not getattr(store, "deleted_at", None):
                matches.append((str(store.created_at), store.id))
        token = response.continuation_token or None
        if not token:
            break
    if not matches:
        return None
    if len(matches) > 1:
        _log.warning(
            "several OpenFGA stores share a name; using the oldest", extra={"store_name": name}
        )
    return min(matches)[1]


async def resolve_store_id(client: FgaClient, name: str = DEFAULT_STORE_NAME) -> str:
    """Like :func:`find_store_id` but raise :class:`StoreNotFoundError` when it is missing."""
    store_id = await find_store_id(client, name)
    if store_id is None:
        msg = f"OpenFGA store {name!r} not found; run the authz-bootstrap job"
        raise StoreNotFoundError(msg)
    return store_id


async def latest_model(client: FgaClient) -> tuple[str, dict[str, Any]] | None:
    """The latest model of the bound store as ``(id, json)``, or None if it has none."""
    response = await client.read_latest_authorization_model()
    model = getattr(response, "authorization_model", None)
    if model is None:
        return None
    return str(model.id), to_json(model)


async def resolve_model_id(client: FgaClient) -> str:
    """Id of the latest model of the bound store; :class:`ModelNotFoundError` if none."""
    latest = await latest_model(client)
    if latest is None:
        msg = "the OpenFGA store has no authorization model; run the authz-bootstrap job"
        raise ModelNotFoundError(msg)
    return latest[0]


async def bootstrap_model(
    client: FgaClient,
    *,
    model: Mapping[str, Any] | None = None,
    store_name: str = DEFAULT_STORE_NAME,
) -> BootstrapResult:
    """Create the store if missing and write ``model`` only if it differs from the latest one.

    Idempotent: a second run with the same model writes nothing and returns the same ids.
    ``model`` defaults to the packaged model (``load_model()``). The client is left bound to the
    store.
    """
    wanted = dict(model) if model is not None else load_model()
    store_id = await find_store_id(client, store_name)
    store_created = store_id is None
    if store_id is None:
        created = await client.create_store(CreateStoreRequest(name=store_name))  # type: ignore[no-untyped-call]
        store_id = str(created.id)
        _log.info("created OpenFGA store", extra={"store_id": store_id, "store_name": store_name})
    client.set_store_id(store_id)

    current = await latest_model(client)
    if current is not None and models_equal(current[1], wanted):
        _log.info("authorization model unchanged", extra={"model_id": current[0]})
        return BootstrapResult(store_id, current[0], store_created, model_written=False)

    request = WriteAuthorizationModelRequest(  # type: ignore[no-untyped-call]
        schema_version=wanted["schema_version"],
        type_definitions=wanted["type_definitions"],
        conditions=wanted.get("conditions"),
    )
    written = await client.write_authorization_model(request)
    model_id = str(written.authorization_model_id)
    _log.info("wrote authorization model", extra={"model_id": model_id})
    return BootstrapResult(store_id, model_id, store_created, model_written=True)
