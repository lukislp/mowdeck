"""Additional links of a mower (ADR 0038): a Bluetooth bridge next to the cloud connection.

A mower has one connection kind and may carry further links by kind, today ``ble_bridge``. Whether
a mower may carry a link is decided by the connection-kind registry
(:data:`fleet.domain.connections.SCHEMAS`, ``supports_links``), never by the name of the kind or
of a manufacturer. The options of a link are validated with the schema of the link kind itself.

What Fleet keeps per link: the public options as JSON, the secret options (the bridge's API
encryption key) AES-256-GCM encrypted with the credential vault of the linked accounts, and the
cached ``proxy_info`` of the last scan. What Fleet publishes: the public options in
``MowerRegistration.links`` with ``credentials: "fleet"`` and a ``credentials_rev``; the secrets
leave only in ``fleet.mowers.credentials`` (``link_secrets``). Setting or removing a link is
audited (``mower.link_set``, ``mower.link_removed``, never the key) and re-publishes the full
registration as ``mower.updated``, so the Device-Gateway reopens the connection with the link.

Scans and firmware checks are core NATS requests to the Device-Gateway that holds the mower
(``gateway.mower.<id>.bridge.scan``, ``gateway.mower.<id>.firmware.check``); Fleet relays them
and never speaks to a bridge or a vendor cloud itself. The HTTP layer checks the relation on the
mower (``can_manage_mowers`` for links, ``can_view`` for the firmware check).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    MOWER_UPDATED,
    BridgeScanErrorCode,
    BridgeScanReply,
    BridgeScanRequest,
    FirmwareCheckErrorCode,
    FirmwareCheckRequest,
    FirmwareInfo,
    ProxyInfo,
)
from mowdeck_service_kit import get_logger

from fleet.application.context import Actor, FleetConfig
from fleet.application.linked_accounts import CredentialVault
from fleet.application.ports import BridgeScanGateway, Clock, FirmwareCheckGateway, Store, Tx
from fleet.application.work import UnitOfWork
from fleet.domain.errors import (
    BridgeScanUnsupportedError,
    FieldProblem,
    FirmwareCheckUnsupportedError,
    InvalidInput,
    NotFoundError,
    Unavailable,
)
from fleet.domain.models import Mower, MowerLink
from fleet.domain.rules import (
    SECRET_MASK,
    require_link_supported,
    validate_link_options,
)

_log = get_logger("fleet.links")

LINK_SET: Final = "mower.link_set"
"""Audit action: a link was created or changed (details never carry the key)."""
LINK_REMOVED: Final = "mower.link_removed"
"""Audit action: a link was removed."""
NOT_HELD: Final = "the mower is not held by a device gateway right now"
NO_GATEWAY: Final = "the device gateway did not answer"
SCAN_TIMEOUT_S: Final = 30
"""How long the gateway looks for the mower in a relayed scan (the contract's maximum)."""


@dataclass(frozen=True, slots=True)
class ScanInput:
    """What a scan request says: where the bridge is and its key (``***``: the stored key)."""

    host: str
    port: int = 6053
    noise_psk: str = SECRET_MASK


class LinkService:
    """Use cases of the additional links of a mower and of the firmware check."""

    def __init__(
        self,
        uow: UnitOfWork,
        store: Store,
        vault: CredentialVault,
        scanner: BridgeScanGateway,
        firmware: FirmwareCheckGateway,
        config: FleetConfig,
        clock: Clock,
    ) -> None:
        self.uow = uow
        self.store = store
        self.vault = vault
        self.scanner = scanner
        self.firmware = firmware
        self.config = config
        self.clock = clock

    # --- loading -----------------------------------------------------------------------------

    @staticmethod
    async def _mower(tx: Tx, account_id: UUID, mower_id: UUID) -> Mower:
        item = await tx.mowers.get(id=mower_id, account_id=account_id)
        if item is None:
            raise NotFoundError("mower not found")
        return item

    @staticmethod
    async def _link(tx: Tx, item: Mower, kind: str) -> MowerLink | None:
        return await tx.mower_links.get(mower_id=item.id, account_id=item.account_id, kind=kind)

    async def get(self, account_id: UUID, mower_id: UUID, kind: str) -> MowerLink:
        """The link of ``kind`` (404 when the mower or the link does not exist)."""
        async with self.store.transaction() as tx:
            item = await self._mower(tx, account_id, mower_id)
            link = await self._link(tx, item, kind)
        if link is None:
            raise NotFoundError(f"the mower has no {kind} link")
        return link

    # --- secrets -----------------------------------------------------------------------------

    def _secrets(self, link: MowerLink) -> dict[str, str]:
        try:
            return self.vault.open_link(link)
        except ValueError as exc:
            raise Unavailable("the stored link key cannot be decrypted; enter it again") from exc

    def _stored_key(self, link: MowerLink | None, kind: str) -> str:
        """The stored ``noise_psk`` of ``link`` for a request that sent ``***``."""
        if link is None:
            msg = "noise_psk is required: the mower has no stored link key"
            raise InvalidInput(
                msg, fields=[FieldProblem(f"links/{kind}/noise_psk", msg, "required")]
            )
        key = self._secrets(link).get("noise_psk")
        if not key:
            msg = "noise_psk is required: the stored link carries no key"
            raise InvalidInput(
                msg, fields=[FieldProblem(f"links/{kind}/noise_psk", msg, "required")]
            )
        return key

    # --- changes -----------------------------------------------------------------------------

    async def set(
        self,
        actor: Actor,
        account_id: UUID,
        mower_id: UUID,
        kind: str,
        options: Mapping[str, Any],
    ) -> MowerLink:
        """Create or replace the link of ``kind`` with ``options`` (public and secret keys
        together; a secret sent as ``***`` keeps the stored value).

        409 ``link-not-applicable`` when the mower's connection kind carries no such link, 422
        for options the link kind's schema refuses (fields ``links/<kind>/<key>``), 503 without
        a credential encryption key. Audits ``mower.link_set`` and publishes ``mower.updated``
        with the full registration.
        """
        self.vault.require()
        async with self.uow() as work:
            item = await self._mower(work.tx, account_id, mower_id)
            schema = require_link_supported(item.connection_kind, kind)
            existing = await self._link(work.tx, item, kind)
            merged = dict(options)
            for key in schema.secret_keys:
                if merged.get(key) == SECRET_MASK:
                    merged[key] = self._stored_key(existing, kind) if key == "noise_psk" else None
            public, secret = validate_link_options(
                kind, merged, self.config.connection_options_max_bytes
            )
            now = self.clock()
            if existing is None:
                link = MowerLink(
                    id=uuid4(),
                    mower_id=item.id,
                    account_id=item.account_id,
                    kind=kind,
                    secrets_encrypted=b"",
                    created_at=now,
                    updated_at=now,
                    options=public,
                    created_by=actor.subject_id,
                )
                link.secrets_encrypted = self.vault.seal_link(link.id, secret)
                await work.tx.mower_links.add(link)
            else:
                link = existing
                same_bridge = _same_bridge(link.options, public)
                link.options = public
                # Sealed again only for a new secret (or a rotated key): the revision in the
                # registration then changes, and the gateway reopens the link only when it must.
                try:
                    current: dict[str, str] | None = self.vault.open_link(existing)
                except ValueError:
                    current = None
                if current != secret or self.vault.cipher.needs_reseal(link.secrets_encrypted):
                    link.secrets_encrypted = self.vault.seal_link(link.id, secret)
                link.updated_at = now
                if not same_bridge:
                    link.proxy_info = None  # another bridge: the cached information is stale
                await work.tx.mower_links.update(link)
            await work.audit(
                actor,
                LINK_SET,
                account_id,
                "mower",
                item.id,
                {
                    "kind": kind,
                    "created": existing is None,
                    **{k: v for k, v in public.items() if k != "device_name" or v},
                },
            )
            work.emit(MOWER_UPDATED, account_id, await self.vault.registration(work.tx, item))
        _log.info("mower link set", mower_id=str(mower_id), kind=kind, created=existing is None)
        return link

    async def delete(self, actor: Actor, account_id: UUID, mower_id: UUID, kind: str) -> None:
        """Remove the link of ``kind`` (404 without one); audits ``mower.link_removed`` and
        publishes ``mower.updated`` with the registration without it."""
        async with self.uow() as work:
            item = await self._mower(work.tx, account_id, mower_id)
            deleted = await work.tx.mower_links.delete(
                mower_id=item.id, account_id=item.account_id, kind=kind
            )
            if not deleted:
                raise NotFoundError(f"the mower has no {kind} link")
            await work.audit(actor, LINK_REMOVED, account_id, "mower", item.id, {"kind": kind})
            work.emit(MOWER_UPDATED, account_id, await self.vault.registration(work.tx, item))
        _log.info("mower link removed", mower_id=str(mower_id), kind=kind)

    # --- scans -------------------------------------------------------------------------------

    @staticmethod
    def _expected_name(item: Mower, link: MowerLink | None) -> str | None:
        name = (link.options.get("device_name") if link is not None else None) or (
            item.connection_options.get("device_name")
        )
        return str(name) if name else None

    async def _relay_scan(self, item: Mower, request: BridgeScanRequest) -> BridgeScanReply:
        """One request to the gateway; the relay errors become problems, the scan outcomes
        (``unreachable``, ``auth_failed``, ``timeout``, ``busy``) stay in the reply so the
        client sees the proxy and the devices seen so far."""
        try:
            reply = await self.scanner.request(request)
        except Exception as exc:
            _log.warning(
                "bridge scan request failed", mower_id=str(item.id), error=type(exc).__name__
            )
            raise Unavailable(NO_GATEWAY) from exc
        if reply.error is not None:
            match reply.error.code:
                case BridgeScanErrorCode.UNSUPPORTED:
                    raise BridgeScanUnsupportedError(reply.error.detail)
                case BridgeScanErrorCode.FORBIDDEN:
                    raise NotFoundError("mower not found")
                case BridgeScanErrorCode.NOT_FOUND:
                    raise Unavailable(NOT_HELD)
                case _:
                    pass
        return reply

    async def _remember_proxy(self, link: MowerLink | None, reply: BridgeScanReply) -> None:
        if link is None or reply.proxy is None:
            return
        async with self.store.transaction() as tx:
            await tx.mower_links.update_where(
                {"proxy_info": reply.proxy.model_dump(mode="json"), "updated_at": self.clock()},
                id=link.id,
            )

    async def scan(
        self, account_id: UUID, mower_id: UUID, kind: str, data: ScanInput
    ) -> BridgeScanReply:
        """Look for the mower through the bridge at ``data.host``:``data.port`` (the caller holds
        ``can_manage_mowers``): the gateway that holds the mower connects to the bridge with
        the key, reads its information and collects mower advertisements until the expected
        name shows up. ``***`` as key means the stored key of the existing link. When the
        existing link points at the same bridge, its ``proxy_info`` is refreshed.

        409 ``link-not-applicable`` for a connection kind without such a link, 409
        ``bridge-scan-unsupported`` when the gateway cannot scan through this bridge, 503 when no
        gateway answers in time or holds the mower.
        """
        async with self.store.transaction() as tx:
            item = await self._mower(tx, account_id, mower_id)
            require_link_supported(item.connection_kind, kind)
            link = await self._link(tx, item, kind)
        key = data.noise_psk
        if key == SECRET_MASK:
            key = self._stored_key(link, kind)
        request = BridgeScanRequest(
            mower_id=item.id,
            account_id=item.account_id,
            host=data.host,
            port=data.port,
            noise_psk=key,
            expected_name=self._expected_name(item, link),
            timeout_s=SCAN_TIMEOUT_S,
        )
        reply = await self._relay_scan(item, request)
        same_bridge = link is not None and _same_bridge(
            link.options, {"host": data.host, "port": data.port}
        )
        await self._remember_proxy(link if same_bridge else None, reply)
        return reply

    async def check(self, account_id: UUID, mower_id: UUID, kind: str) -> BridgeScanReply:
        """Scan again through the stored link ("re-check"): the stored host, port and key;
        refreshes ``proxy_info``. 404 without a link; otherwise as :meth:`scan`."""
        async with self.store.transaction() as tx:
            item = await self._mower(tx, account_id, mower_id)
            require_link_supported(item.connection_kind, kind)
            link = await self._link(tx, item, kind)
        if link is None:
            raise NotFoundError(f"the mower has no {kind} link")
        request = BridgeScanRequest(
            mower_id=item.id,
            account_id=item.account_id,
            host=str(link.options.get("host") or ""),
            port=int(link.options.get("port") or 6053),
            noise_psk=self._stored_key(link, kind),
            expected_name=self._expected_name(item, link),
            timeout_s=SCAN_TIMEOUT_S,
        )
        reply = await self._relay_scan(item, request)
        await self._remember_proxy(link, reply)
        return reply

    # --- firmware ----------------------------------------------------------------------------

    async def firmware_check(self, account_id: UUID, mower_id: UUID) -> FirmwareInfo:
        """Ask the gateway that holds the mower to check the vendor for firmware updates (the
        caller holds ``can_view``). 409 ``firmware-check-unsupported`` when the connection cannot
        ask the vendor, 503 when the mower is offline or no gateway answers."""
        async with self.store.transaction() as tx:
            item = await self._mower(tx, account_id, mower_id)
        request = FirmwareCheckRequest(mower_id=item.id, account_id=item.account_id)
        try:
            reply = await self.firmware.request(request)
        except Exception as exc:
            _log.warning(
                "firmware check request failed", mower_id=str(item.id), error=type(exc).__name__
            )
            raise Unavailable(NO_GATEWAY) from exc
        if reply.error is not None:
            detail = reply.error.detail
            match reply.error.code:
                case FirmwareCheckErrorCode.UNSUPPORTED:
                    raise FirmwareCheckUnsupportedError(detail)
                case FirmwareCheckErrorCode.FORBIDDEN:
                    raise NotFoundError("mower not found")
                case FirmwareCheckErrorCode.NOT_FOUND:
                    raise Unavailable(NOT_HELD)
                case _:
                    raise Unavailable(detail or "the firmware state is not available")
        info = reply.info
        if info is None:  # pragma: no cover - the reply model guarantees one of both
            raise Unavailable("the device gateway sent no firmware information")
        return info


def _same_bridge(stored: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """True when both option sets name the same bridge (host and port)."""
    return bool(
        stored.get("host") == other.get("host")
        and stored.get("port", 6053) == other.get("port", 6053)
    )


def proxy_info_of(link: MowerLink) -> ProxyInfo | None:
    """The cached bridge information of a link (None until a scan answered, or when the cached
    row no longer parses)."""
    if link.proxy_info is None:
        return None
    try:
        return ProxyInfo.model_validate(link.proxy_info)
    except ValueError:
        return None
