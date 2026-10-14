"""OpenFGA-backed :class:`~mowdeck_authz.authorizer.Authorizer`.

* **Store and model.** ``connect()`` resolves the store by name (or uses the pinned id) and the
  latest model (or the pinned id) and pins that model id for every call of the process, so a
  half-rolled deployment never mixes models. ``connect()`` is also attempted lazily by the first
  check and by the readiness check, so a service starts even while OpenFGA is still coming up.
* **Fail closed.** Any error (connection, timeout, HTTP error, per-item batch error) answers
  ``False``, logs a warning and increments ``mowdeck_authz_errors_total``. Errors are never
  cached.
* **Cache.** Decisions are cached for ``cache_ttl_seconds`` (default 5) per
  ``(subject, relation, object, context)``. The automatically added ``current_time`` is not part
  of the key, so a support grant may outlive its expiry by at most one TTL. The administrative
  relations in ``UNCACHED_RELATIONS`` are always asked fresh.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from openfga_sdk.client.models import (
    ClientBatchCheckItem,
    ClientBatchCheckRequest,
    ClientCheckRequest,
    ClientListObjectsRequest,
)
from prometheus_client import CollectorRegistry, Counter

from mowdeck_authz._client import ClientFactory, FgaClient, create_client
from mowdeck_authz.authorizer import (
    CheckItem,
    CheckLike,
    Context,
    DecisionCache,
    as_check_item,
    cache_key,
)
from mowdeck_authz.bootstrap import resolve_model_id, resolve_store_id
from mowdeck_authz.errors import AuthzUnavailableError
from mowdeck_authz.permissions import Relation
from mowdeck_authz.refs import ObjectRef, Subject
from mowdeck_authz.settings import AuthzSettings
from mowdeck_authz.tuples import rfc3339

_log = logging.getLogger("mowdeck_authz.openfga")

UNCACHED_RELATIONS: frozenset[str] = frozenset(
    {Relation.CAN_MANAGE_USERS, Relation.CAN_MANAGE_BILLING, Relation.CAN_MANAGE_ACCOUNT}
)
"""Rare, security sensitive relations that are never served from the cache."""


class FgaConnection:
    """One SDK client bound to the resolved store and pinned model.

    Shared by :class:`OpenFgaAuthorizer` and :class:`~mowdeck_authz.writer.OpenFgaTupleBackend`
    when a service needs both.
    """

    def __init__(
        self, settings: AuthzSettings, *, client_factory: ClientFactory = create_client
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._client: FgaClient | None = None
        self._lock = asyncio.Lock()
        self.store_id: str | None = None
        self.model_id: str | None = None

    @property
    def is_connected(self) -> bool:
        """True once store and model are resolved."""
        return self._client is not None

    @property
    def client(self) -> FgaClient:
        """The bound client; raises :class:`AuthzUnavailableError` before ``connect()``."""
        if self._client is None:
            msg = "not connected to OpenFGA"
            raise AuthzUnavailableError(msg)
        return self._client

    async def connect(self) -> None:
        """Resolve store and model and pin them. Raises :class:`AuthzUnavailableError`."""
        async with self._lock:
            if self._client is not None:
                return
            client = self._client_factory(self.settings)
            try:
                store_id = self.settings.store_id or await resolve_store_id(
                    client, self.settings.store_name
                )
                client.set_store_id(store_id)
                model_id = self.settings.model_id or await resolve_model_id(client)
                client.set_authorization_model_id(model_id)
            except Exception as exc:
                await _close_quietly(client)
                if isinstance(exc, AuthzUnavailableError):
                    raise
                msg = f"cannot connect to OpenFGA: {type(exc).__name__}"
                raise AuthzUnavailableError(msg) from exc
            self.store_id, self.model_id, self._client = store_id, model_id, client
            _log.info("connected to OpenFGA", extra={"store_id": store_id, "model_id": model_id})

    async def ensure_connected(self) -> FgaClient | None:
        """The client, connecting first if needed; None (and a warning) if that fails."""
        if self._client is not None:
            return self._client
        try:
            await self.connect()
        except AuthzUnavailableError as exc:
            _log.warning("OpenFGA unavailable", extra={"error": str(exc)})
            return None
        return self._client

    async def ping(self) -> bool:
        """True if the store can be read with the pinned settings."""
        client = await self.ensure_connected()
        if client is None:
            return False
        try:
            await client.get_store()
        except Exception as exc:
            _log.warning("OpenFGA ping failed", extra={"error": type(exc).__name__})
            return False
        return True

    async def close(self) -> None:
        """Close the client; a later call connects again."""
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            await _close_quietly(client)


async def _close_quietly(client: FgaClient) -> None:
    try:
        await client.close()
    except Exception:  # pragma: no cover - closing an HTTP session rarely fails
        _log.debug("closing the OpenFGA client failed", exc_info=True)


class _Metrics:
    def __init__(self, registry: CollectorRegistry | None) -> None:
        self.enabled = registry is not None
        if registry is None:
            return
        self.decisions = Counter(
            "mowdeck_authz_decisions_total",
            "Authorization decisions by outcome (allowed, denied, error).",
            ["outcome"],
            registry=registry,
        )
        self.cache_hits = Counter(
            "mowdeck_authz_cache_hits_total",
            "Authorization decisions answered from the in-process cache.",
            registry=registry,
        )
        self.errors = Counter(
            "mowdeck_authz_errors_total",
            "Failed OpenFGA calls; each one was answered with deny (fail closed).",
            ["operation"],
            registry=registry,
        )

    def decision(self, outcome: str, count: int = 1) -> None:
        if self.enabled and count:
            self.decisions.labels(outcome).inc(count)

    def cache_hit(self, count: int) -> None:
        if self.enabled and count:
            self.cache_hits.inc(count)

    def error(self, operation: str) -> None:
        if self.enabled:
            self.errors.labels(operation).inc()


class OpenFgaAuthorizer:
    """Async authorizer backed by OpenFGA. Construct once per process and share it."""

    def __init__(
        self,
        settings: AuthzSettings | FgaConnection,
        *,
        registry: CollectorRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        client_factory: ClientFactory = create_client,
        uncached_relations: frozenset[str] = UNCACHED_RELATIONS,
    ) -> None:
        if isinstance(settings, FgaConnection):
            self.connection = settings
        else:
            self.connection = FgaConnection(settings, client_factory=client_factory)
        conf = self.connection.settings
        self.cache = DecisionCache(conf.cache_ttl_seconds, conf.cache_max_entries, clock)
        self._now = now
        self._uncached = uncached_relations
        self._metrics = _Metrics(registry)

    @classmethod
    def from_env(cls, *, registry: CollectorRegistry | None = None) -> "OpenFgaAuthorizer":
        """Build from ``MOWDECK_OPENFGA_*`` environment variables."""
        return cls(AuthzSettings.from_env(), registry=registry)

    @property
    def store_id(self) -> str | None:
        """The resolved store id (after ``connect()``)."""
        return self.connection.store_id

    @property
    def model_id(self) -> str | None:
        """The pinned model id (after ``connect()``)."""
        return self.connection.model_id

    async def connect(self) -> None:
        """Resolve store and model now (raises :class:`AuthzUnavailableError`)."""
        await self.connection.connect()

    async def close(self) -> None:
        """Close the client."""
        await self.connection.close()

    async def ping(self) -> bool:
        """Readiness: True if OpenFGA answers with the resolved store."""
        return await self.connection.ping()

    def invalidate(self) -> None:
        """Drop all cached decisions."""
        self.cache.invalidate()

    def invalidate_subject(self, subject: Subject | str) -> None:
        """Drop the cached decisions of one subject."""
        self.cache.invalidate_subject(subject)

    def _context(self, context: Context | None) -> dict[str, Any]:
        return {"current_time": rfc3339(self._now()), **(context or {})}

    def _cacheable(self, item: CheckItem) -> bool:
        return self.cache.enabled and item.relation not in self._uncached

    async def check(
        self,
        subject: Subject,
        relation: str,
        obj: ObjectRef,
        *,
        context: Context | None = None,
    ) -> bool:
        """True only if OpenFGA says so; False on deny and on any error."""
        return (await self.check_many([CheckItem(subject, relation, obj, context)]))[0]

    async def check_many(self, items: Sequence[CheckLike]) -> list[bool]:
        """Answer several checks with one ``BatchCheck`` call (cache hits are skipped)."""
        checks = [as_check_item(item) for item in items]
        results: list[bool | None] = [None] * len(checks)
        hits = 0
        for index, item in enumerate(checks):
            if self._cacheable(item):
                cached = self.cache.get(cache_key(item))
                if cached is not None:
                    results[index] = cached
                    hits += 1
        self._metrics.cache_hit(hits)
        pending = [i for i, result in enumerate(results) if result is None]
        if pending:
            answers = await self._ask(checks, pending)
            for index, answer in answers.items():
                results[index] = answer
                if self._cacheable(checks[index]):
                    self.cache.put(cache_key(checks[index]), answer)
        final = [result is True for result in results]
        allowed = sum(final)
        self._metrics.decision("allowed", allowed)
        self._metrics.decision("denied", len(final) - allowed)
        return final

    async def _ask(self, checks: list[CheckItem], pending: list[int]) -> dict[int, bool]:
        """Successful answers by index; failed items are missing (and count as deny)."""
        client = await self.connection.ensure_connected()
        if client is None:
            self._fail("connect", None, len(pending))
            return {}
        if len(pending) == 1:
            index = pending[0]
            item = checks[index]
            try:
                response = await client.check(
                    ClientCheckRequest(
                        user=str(item.subject),
                        relation=item.relation,
                        object=str(item.object),
                        context=self._context(item.context),
                    )
                )
            except Exception as exc:
                self._fail("check", exc)
                return {}
            return {index: getattr(response, "allowed", None) is True}
        request = ClientBatchCheckRequest(
            checks=[
                ClientBatchCheckItem(
                    user=str(checks[i].subject),
                    relation=checks[i].relation,
                    object=str(checks[i].object),
                    correlation_id=str(i),
                    context=self._context(checks[i].context),
                )
                for i in pending
            ]
        )
        try:
            response = await client.batch_check(request)
        except Exception as exc:
            self._fail("batch_check", exc)
            return {}
        answers: dict[int, bool] = {}
        wanted = set(pending)
        for single in getattr(response, "result", None) or []:
            try:
                index = int(single.correlation_id)
            except (TypeError, ValueError):
                continue
            if index not in wanted:
                continue
            if single.error is not None:
                self._fail("batch_check_item", single.error)
                continue
            answers[index] = single.allowed is True
        return answers

    def _fail(self, operation: str, error: object, count: int = 1) -> None:
        for _ in range(count):
            self._metrics.error(operation)
        _log.warning(
            "authorization check failed; denying (fail closed)",
            extra={"operation": operation, "error": _describe(error)},
        )

    async def list_objects(
        self,
        subject: Subject,
        relation: str,
        object_type: str,
        *,
        context: Context | None = None,
    ) -> list[str]:
        """Objects of ``object_type`` with ``relation`` for ``subject``; ``[]`` on error.

        Not cached. OpenFGA caps the result size and duration (server settings
        ``OPENFGA_LIST_OBJECTS_MAX_RESULTS`` and ``OPENFGA_LIST_OBJECTS_DEADLINE``); callers
        with large fleets use an account-level check first.
        """
        client = await self.connection.ensure_connected()
        if client is None:
            self._fail("connect", None)
            return []
        try:
            response = await client.list_objects(
                ClientListObjectsRequest(
                    user=str(subject),
                    relation=relation,
                    type=object_type,
                    context=self._context(context),
                )
            )
        except Exception as exc:
            self._fail("list_objects", exc)
            return []
        return [str(obj) for obj in getattr(response, "objects", None) or []]


def _describe(error: object) -> str:
    if error is None:
        return "unavailable"
    if isinstance(error, BaseException):
        return type(error).__name__
    message = getattr(error, "message", None)
    return str(message) if message else str(error)[:200]
