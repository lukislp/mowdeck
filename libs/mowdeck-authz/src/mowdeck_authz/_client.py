"""Thin typed boundary around the untyped ``openfga-sdk`` async client.

Everything else in the package talks to :class:`FgaClient`, so tests can pass a fake and mypy
strict never sees an untyped SDK call outside this module.
"""

from collections.abc import Callable
from datetime import date, datetime
from enum import Enum
from typing import Any, Protocol

from openfga_sdk import ClientConfiguration, OpenFgaClient
from openfga_sdk.client.models import ClientTuple
from openfga_sdk.configuration import RetryParams
from openfga_sdk.credentials import CredentialConfiguration, Credentials
from openfga_sdk.models.relationship_condition import RelationshipCondition

from mowdeck_authz.refs import Subject
from mowdeck_authz.settings import AuthzSettings
from mowdeck_authz.tuples import RelationshipTuple, TupleCondition

Options = dict[str, Any]


class FgaClient(Protocol):
    """The subset of ``openfga_sdk.OpenFgaClient`` (0.10.x, async) used by this package."""

    def set_store_id(self, value: str) -> None:
        """Bind the client to a store."""
        ...

    def set_authorization_model_id(self, value: str) -> None:
        """Pin the model used by every call."""
        ...

    async def list_stores(self, options: Options | None = None) -> Any:
        """``ListStoresResponse`` with ``stores`` and ``continuation_token``."""
        ...

    async def create_store(self, body: Any, options: Options | None = None) -> Any:
        """``CreateStoreResponse`` with ``id``."""
        ...

    async def get_store(self, options: Options | None = None) -> Any:
        """``GetStoreResponse`` of the bound store."""
        ...

    async def read_latest_authorization_model(self, options: Options | None = None) -> Any:
        """``ReadAuthorizationModelResponse``; ``authorization_model`` is None if none exists."""
        ...

    async def write_authorization_model(self, body: Any, options: Options | None = None) -> Any:
        """``WriteAuthorizationModelResponse`` with ``authorization_model_id``."""
        ...

    async def check(self, body: Any, options: Options | None = None) -> Any:
        """``CheckResponse`` with ``allowed``."""
        ...

    async def batch_check(self, body: Any, options: Options | None = None) -> Any:
        """``ClientBatchCheckResponse`` with ``result`` (correlation id, allowed, error)."""
        ...

    async def list_objects(self, body: Any, options: Options | None = None) -> Any:
        """``ListObjectsResponse`` with ``objects``."""
        ...

    async def read(self, body: Any, options: Options | None = None) -> Any:
        """``ReadResponse`` with ``tuples`` and ``continuation_token``."""
        ...

    async def write(self, body: Any, options: Options | None = None) -> Any:
        """Write and delete tuples in one transaction."""
        ...

    async def close(self) -> None:
        """Close the HTTP session."""
        ...


ClientFactory = Callable[[AuthzSettings], FgaClient]
"""Builds a client (not yet bound to a store) from settings; tests pass a fake."""


def create_client(settings: AuthzSettings) -> FgaClient:
    """Build the real async SDK client: pre-shared key, short timeout, one retry."""
    credentials = None
    if settings.api_token:
        credentials = Credentials(
            method="api_token",
            configuration=CredentialConfiguration(api_token=settings.api_token),
        )
    configuration = ClientConfiguration(
        api_url=settings.api_url,
        credentials=credentials,
        retry_params=RetryParams(max_retry=1, min_wait_in_ms=50),  # type: ignore[no-untyped-call]
        timeout_millisec=int(settings.timeout_seconds * 1000),
    )
    client: FgaClient = OpenFgaClient(configuration)
    return client


def to_json(value: Any) -> Any:
    """Serialise an SDK model to the JSON shape of the OpenFGA API (camelCase keys).

    Mirrors ``ApiClient.sanitize_for_serialization`` without needing an ``ApiClient``.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    attribute_map: dict[str, str] | None = getattr(value, "attribute_map", None)
    if attribute_map is None:
        return value
    return {
        attribute_map[attr]: to_json(getattr(value, attr))
        for attr in getattr(value, "openapi_types", {})
        if getattr(value, attr) is not None
    }


def to_client_tuple(item: RelationshipTuple) -> ClientTuple:
    """Convert to the SDK's ``ClientTuple``."""
    condition = None
    if item.condition is not None:
        condition = RelationshipCondition(  # type: ignore[no-untyped-call]
            name=item.condition.name, context=item.condition.context_dict()
        )
    return ClientTuple(
        user=str(item.subject),
        relation=item.relation,
        object=str(item.object),
        condition=condition,
    )


def from_tuple_key(key: Any) -> RelationshipTuple:
    """Convert an SDK ``TupleKey`` (from a read) to a :class:`RelationshipTuple`."""
    condition = None
    raw_condition = getattr(key, "condition", None)
    if raw_condition is not None and getattr(raw_condition, "name", None):
        condition = TupleCondition.of(raw_condition.name, raw_condition.context or {})
    return RelationshipTuple(
        Subject.parse(key.user),
        key.relation,
        Subject.parse(key.object).as_object(),
        condition,
    )
