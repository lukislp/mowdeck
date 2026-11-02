"""A scriptable stand-in for ``openfga_sdk.OpenFgaClient`` (shared by the authz unit tests)."""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, ClassVar

from mowdeck_authz.settings import AuthzSettings


class FakeModel:
    """Looks like an SDK ``AuthorizationModel`` to ``mowdeck_authz._client.to_json``."""

    attribute_map: ClassVar[dict[str, str]] = {
        "id": "id",
        "schema_version": "schema_version",
        "type_definitions": "type_definitions",
        "conditions": "conditions",
    }
    openapi_types: ClassVar[dict[str, str]] = attribute_map

    def __init__(self, model_id: str, body: dict[str, Any]) -> None:
        self.id = model_id
        self.schema_version = body["schema_version"]
        self.type_definitions = body["type_definitions"]
        self.conditions = body.get("conditions")


class FakeFgaClient:
    """Records calls; behaviour is set through attributes."""

    def __init__(self) -> None:
        self.stores: list[SimpleNamespace] = []
        self.models: dict[str, list[FakeModel]] = {}
        self.store_id: str | None = None
        self.model_id: str | None = None
        self.calls: list[tuple[str, Any]] = []
        self.allowed: Callable[[str, str, str, dict[str, Any] | None], bool] = (
            lambda user, relation, obj, ctx: False
        )
        self.fail: dict[str, Exception] = {}
        self.batch_errors: set[str] = set()
        self.batch_drop: set[str] = set()
        self.objects: list[str] = []
        self.read_pages: list[SimpleNamespace] = []
        self.closed = False
        self.page_size_stores = 100
        # When set, check and batch_check wait for it (to hold questions in flight).
        self.gate: asyncio.Event | None = None

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    async def _wait_gate(self) -> None:
        if self.gate is not None:
            await self.gate.wait()

    def set_store_id(self, value: str) -> None:
        self.store_id = value

    def set_authorization_model_id(self, value: str) -> None:
        self.model_id = value

    async def list_stores(self, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("list_stores", options))
        self._maybe_fail("list_stores")
        options = options or {}
        start = int(options.get("continuation_token") or 0)
        page = self.stores[start : start + self.page_size_stores]
        end = start + len(page)
        token = str(end) if end < len(self.stores) else ""
        return SimpleNamespace(stores=page, continuation_token=token)

    async def create_store(self, body: Any, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("create_store", body.name))
        store = SimpleNamespace(
            id=f"store{len(self.stores) + 1}", name=body.name, created_at=f"t{len(self.stores)}"
        )
        self.stores.append(store)
        return SimpleNamespace(id=store.id, name=store.name)

    async def get_store(self, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("get_store", self.store_id))
        self._maybe_fail("get_store")
        return SimpleNamespace(id=self.store_id)

    async def read_latest_authorization_model(self, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("read_latest_authorization_model", self.store_id))
        self._maybe_fail("read_latest_authorization_model")
        models = self.models.get(self.store_id or "", [])
        return SimpleNamespace(authorization_model=models[-1] if models else None)

    async def write_authorization_model(
        self, body: Any, options: dict[str, Any] | None = None
    ) -> Any:
        self.calls.append(("write_authorization_model", self.store_id))
        models = self.models.setdefault(self.store_id or "", [])
        model_id = f"model{len(models) + 1}"
        models.append(
            FakeModel(
                model_id,
                {
                    "schema_version": body.schema_version,
                    "type_definitions": body.type_definitions,
                    "conditions": body.conditions,
                },
            )
        )
        return SimpleNamespace(authorization_model_id=model_id)

    async def check(self, body: Any, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("check", body))
        await self._wait_gate()
        self._maybe_fail("check")
        return SimpleNamespace(
            allowed=self.allowed(body.user, body.relation, body.object, body.context)
        )

    async def batch_check(self, body: Any, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("batch_check", body))
        await self._wait_gate()
        self._maybe_fail("batch_check")
        results = []
        for item in reversed(body.checks):  # order must not matter
            if item.correlation_id in self.batch_drop:
                continue
            error = None
            if item.correlation_id in self.batch_errors:
                error = SimpleNamespace(message="boom")
            results.append(
                SimpleNamespace(
                    correlation_id=item.correlation_id,
                    allowed=self.allowed(item.user, item.relation, item.object, item.context),
                    error=error,
                )
            )
        return SimpleNamespace(result=results)

    async def list_objects(self, body: Any, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("list_objects", body))
        self._maybe_fail("list_objects")
        return SimpleNamespace(objects=list(self.objects))

    async def read(self, body: Any, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("read", (body, dict(options or {}))))
        index = int((options or {}).get("continuation_token") or 0)
        return self.read_pages[index]

    async def write(self, body: Any, options: dict[str, Any] | None = None) -> Any:
        self.calls.append(("write", (body, options)))
        self._maybe_fail("write")
        return SimpleNamespace()

    async def close(self) -> None:
        self.closed = True

    def names(self) -> list[str]:
        """Names of the calls made so far."""
        return [name for name, _ in self.calls]


def factory(client: FakeFgaClient) -> Callable[[AuthzSettings], FakeFgaClient]:
    """A client factory returning ``client``."""

    def build(settings: AuthzSettings) -> FakeFgaClient:
        client.closed = False
        return client

    return build


def store(store_id: str, name: str = "mowdeck", created_at: str = "t0") -> SimpleNamespace:
    """A store entry as returned by ``list_stores``."""
    return SimpleNamespace(id=store_id, name=name, created_at=created_at, deleted_at=None)


def connected_client(model_id: str = "01MODEL") -> FakeFgaClient:
    """A client with a store ``mowdeck`` (id ``s1``) whose latest model is ``model_id``."""
    client = FakeFgaClient()
    client.stores = [store("s1")]
    client.models["s1"] = [FakeModel(model_id, {"schema_version": "1.1", "type_definitions": []})]
    return client
