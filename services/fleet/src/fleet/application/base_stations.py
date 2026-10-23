"""RTK base stations (ADR 0047): register, list, rename, remove; link mowers to them; ask for and
install their firmware through a linked mower.

A base station is its own record next to the mowers (``base_stations``), never a mower: it has
no connection of its own in MowDeck. The Device-Gateway learns about it through the
registrations of the mowers linked to it (``MowerRegistration.base_station``) and reports it in
their state (``MowerState.base_station``). Two sources:

* ``cloud``: a device of a linked vendor account whose device list marks it ``rtk_base``. The
  physical base station (its IoT id) is registered in one MowDeck account only,
  installation-wide (409 ``base-station-already-registered``, the other account is never
  named). It serves cloud mowers of the same linked account: its firmware is asked for with
  that account's cloud session.
* ``simulated``: the simulator's base station for simulated mowers (when the installation
  allows ``simulated`` mowers).

Who may do what: listing and reading base stations, and the firmware check, need ``can_view``
on the account; registering, renaming, removing and the firmware update need
``can_manage_mowers`` on the account (403 otherwise; the caller's own account is no secret).
Linking a mower needs ``can_manage_mowers`` on the mower (the route checks it) and a base
station of the same account. Every change is audited (``base_station.registered``,
``base_station.updated``, ``base_station.removed``, ``mower.base_station_set``,
``mower.base_station_removed``) and re-publishes the registrations of the affected mowers as
``mower.updated``. The firmware update is an ordinary command of a linked mower
(``base_station_firmware_update``) with its command record.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from mowdeck_authz import account
from mowdeck_contracts.v1 import (
    MOWER_UPDATED,
    BaseStationFirmwareUpdate,
    CommandAccepted,
    CommandRequest,
    DeviceType,
    FirmwareCheckErrorCode,
    FirmwareCheckRequest,
    FirmwareInfo,
)
from mowdeck_service_kit import get_logger

from fleet.application.access import Access
from fleet.application.commands import CommandService
from fleet.application.context import Actor, FleetConfig
from fleet.application.linked_accounts import CredentialVault, find_device
from fleet.application.ports import Clock, FirmwareCheckGateway, Store, Tx
from fleet.application.work import UnitOfWork, Work
from fleet.domain.errors import (
    BaseStationAlreadyRegisteredError,
    BaseStationNotApplicableError,
    BaseStationNotLinkedError,
    ConflictError,
    FieldProblem,
    InvalidInput,
    NotFoundError,
    Unavailable,
)
from fleet.domain.models import BaseStation, Mower
from fleet.domain.rules import CAN_MANAGE_MOWERS, CAN_VIEW, clean_name

_log = get_logger("fleet.base_stations")

SOURCE_CLOUD: Final = "cloud"
SOURCE_SIMULATED: Final = "simulated"
_ELSEWHERE: Final = "this base station is already registered in MowDeck"
NO_GATEWAY: Final = "no device gateway holding a linked mower answered"


@dataclass(frozen=True, slots=True)
class BaseStationInput:
    """What a registration says: a device of a linked account, or the simulated one."""

    name: str | None = None
    source: str = SOURCE_CLOUD
    linked_account_id: UUID | None = None
    device_name: str | None = None


@dataclass(frozen=True, slots=True)
class BaseStationView:
    """A base station with the ids of the mowers linked to it (by name)."""

    base: BaseStation
    mower_ids: list[UUID] = field(default_factory=list)


def serves(base: BaseStation, mower: Mower) -> bool:
    """Whether ``base`` can serve ``mower``: a cloud base station a cloud mower of the same
    linked account, the simulated one a simulated mower."""
    if base.source == SOURCE_SIMULATED:
        return mower.connection_kind == "simulated"
    return (
        mower.connection_kind == "cloud"
        and mower.linked_account_id is not None
        and mower.linked_account_id == base.linked_account_id
    )


class BaseStationService:
    """Use cases of RTK base stations."""

    def __init__(
        self,
        uow: UnitOfWork,
        store: Store,
        access: Access,
        vault: CredentialVault,
        firmware: FirmwareCheckGateway,
        commands: CommandService,
        config: FleetConfig,
        clock: Clock,
    ) -> None:
        self.uow = uow
        self.store = store
        self.access = access
        self.vault = vault
        self.firmware = firmware
        self.commands = commands
        self.config = config
        self.clock = clock

    # --- helpers -----------------------------------------------------------------------------

    async def _require(self, actor: Actor, relation: str, account_id: UUID) -> None:
        await self.access.require(actor.principal, relation, account(account_id), hide=False)

    @staticmethod
    async def _load(tx: Tx, account_id: UUID, base_station_id: UUID) -> BaseStation:
        found = await tx.base_stations.get(id=base_station_id, account_id=account_id)
        if found is None:
            raise NotFoundError("base station not found")
        return found

    @staticmethod
    async def _linked(tx: Tx, base: BaseStation) -> list[Mower]:
        return await tx.mowers.find(
            account_id=base.account_id, base_station_id=base.id, order_by="name"
        )

    async def _republish(self, work: Work, mowers: list[Mower]) -> None:
        for item in mowers:
            work.emit(MOWER_UPDATED, item.account_id, await self.vault.registration(work.tx, item))

    # --- reading -----------------------------------------------------------------------------

    async def visible(self, actor: Actor, account_id: UUID) -> list[BaseStationView]:
        """The base stations of the caller's account, by name (``can_view`` on the account)."""
        await self._require(actor, CAN_VIEW, account_id)
        async with self.store.transaction() as tx:
            rows = await tx.base_stations.find(account_id=account_id, order_by="name")
            mowers = await tx.mowers.find(account_id=account_id, order_by="name")
        linked: dict[UUID, list[UUID]] = {}
        for item in mowers:
            if item.base_station_id is not None:
                linked.setdefault(item.base_station_id, []).append(item.id)
        return [BaseStationView(row, linked.get(row.id, [])) for row in rows]

    async def get(self, actor: Actor, account_id: UUID, base_station_id: UUID) -> BaseStationView:
        """One base station of the caller's account (404 for any other)."""
        await self._require(actor, CAN_VIEW, account_id)
        async with self.store.transaction() as tx:
            row = await self._load(tx, account_id, base_station_id)
            mowers = await self._linked(tx, row)
        return BaseStationView(row, [m.id for m in mowers])

    # --- changes -----------------------------------------------------------------------------

    async def register(
        self, actor: Actor, account_id: UUID, data: BaseStationInput
    ) -> BaseStationView:
        """Register a base station (``can_manage_mowers`` on the account).

        ``cloud``: the device must be in the linked account's cached device list as
        ``rtk_base`` (422 ``unknown_device``, ``not_a_base_station``; read the account again to
        refresh it); 409 ``base-station-already-registered`` when its IoT id is registered
        already. ``simulated``: only when the installation allows simulated mowers. At most
        ``max_mowers_per_account`` base stations per account.
        """
        await self._require(actor, CAN_MANAGE_MOWERS, account_id)
        now = self.clock()
        async with self.uow() as work:
            limit = self.config.max_mowers_per_account
            if await work.tx.base_stations.count(account_id=account_id) >= limit:
                msg = f"the account has reached its limit of {limit} base stations"
                raise ConflictError(msg)
            base = await self._new(work.tx, account_id, data, actor, now)
            try:
                await work.tx.base_stations.add(base)
            except ConflictError:
                raise BaseStationAlreadyRegisteredError(_ELSEWHERE) from None
            await work.audit(
                actor,
                "base_station.registered",
                account_id,
                "base_station",
                base.id,
                {
                    "name": base.name,
                    "source": base.source,
                    **(
                        {"linked_account_id": str(base.linked_account_id)}
                        if base.linked_account_id
                        else {}
                    ),
                },
            )
        _log.info("base station registered", base_station_id=str(base.id), source=base.source)
        return BaseStationView(base)

    async def _new(
        self, tx: Tx, account_id: UUID, data: BaseStationInput, actor: Actor, now: datetime
    ) -> BaseStation:
        if data.source == SOURCE_SIMULATED:
            if data.linked_account_id is not None or data.device_name is not None:
                msg = "a simulated base station takes no linked account or device"
                raise InvalidInput(
                    msg, fields=[FieldProblem("linked_account_id", msg, "not_allowed")]
                )
            if "simulated" not in self.config.allowed_connection_kinds:
                msg = "simulated devices are not enabled here"
                raise InvalidInput(msg, fields=[FieldProblem("source", msg, "not_allowed")])
            return BaseStation(
                id=uuid4(),
                account_id=account_id,
                name=clean_name(data.name or "Simulated base station", "base station"),
                source=SOURCE_SIMULATED,
                created_at=now,
                updated_at=now,
                created_by=actor.subject_id,
            )
        missing = [
            FieldProblem(
                key, f"{key} is required for a base station of a linked account", "required"
            )
            for key, value in (
                ("linked_account_id", data.linked_account_id),
                ("device_name", data.device_name),
            )
            if not value
        ]
        if missing:
            raise InvalidInput("a base station comes from a linked account", fields=missing)
        assert data.linked_account_id is not None and data.device_name is not None  # noqa: S101
        linked = await tx.linked_accounts.get(id=data.linked_account_id, account_id=account_id)
        if linked is None:
            msg = "linked account not found"
            raise InvalidInput(msg, fields=[FieldProblem("linked_account_id", msg, "not_found")])
        device = find_device(linked, data.device_name)
        if device is None:
            msg = f"the linked account has no device {data.device_name!r}; read the account again"
            raise InvalidInput(msg, fields=[FieldProblem("device_name", msg, "unknown_device")])
        if device.device_type is not DeviceType.RTK_BASE:
            msg = f"{data.device_name} is not an RTK base station; read the account again"
            raise InvalidInput(msg, fields=[FieldProblem("device_name", msg, "not_a_base_station")])
        if device.iot_id:
            holder = await tx.base_stations.get(iot_id=device.iot_id)
            if holder is not None:
                if holder.account_id == account_id:
                    msg = f"this base station is already registered as {holder.name}"
                    raise BaseStationAlreadyRegisteredError(msg)
                raise BaseStationAlreadyRegisteredError(_ELSEWHERE)
        for other in await tx.base_stations.find(
            account_id=account_id, linked_account_id=linked.id
        ):
            if other.device_name == device.device_name:
                msg = f"this base station is already registered as {other.name}"
                raise BaseStationAlreadyRegisteredError(msg)
        return BaseStation(
            id=uuid4(),
            account_id=account_id,
            name=clean_name(data.name or device.device_name, "base station"),
            source=SOURCE_CLOUD,
            created_at=now,
            updated_at=now,
            linked_account_id=linked.id,
            device_name=device.device_name,
            iot_id=device.iot_id or None,
            model=device.model,
            created_by=actor.subject_id,
        )

    async def rename(
        self, actor: Actor, account_id: UUID, base_station_id: UUID, name: str
    ) -> BaseStationView:
        """Give the base station another name (``can_manage_mowers`` on the account); the linked
        mowers' registrations carry it and are published again."""
        await self._require(actor, CAN_MANAGE_MOWERS, account_id)
        async with self.uow() as work:
            base = await self._load(work.tx, account_id, base_station_id)
            cleaned = clean_name(name, "base station")
            mowers = await self._linked(work.tx, base)
            if cleaned != base.name:
                previous = base.name
                base.name = cleaned
                base.updated_at = self.clock()
                await work.tx.base_stations.update(base)
                await work.audit(
                    actor,
                    "base_station.updated",
                    account_id,
                    "base_station",
                    base.id,
                    {"name": cleaned, "previous": previous},
                )
                await self._republish(work, mowers)
        return BaseStationView(base, [m.id for m in mowers])

    async def delete(self, actor: Actor, account_id: UUID, base_station_id: UUID) -> None:
        """Remove the base station (``can_manage_mowers`` on the account); its mowers lose the
        link (and their registrations are published again without it)."""
        await self._require(actor, CAN_MANAGE_MOWERS, account_id)
        async with self.uow() as work:
            base = await self._load(work.tx, account_id, base_station_id)
            mowers = await self._linked(work.tx, base)
            for item in mowers:
                item.base_station_id = None
                item.updated_at = self.clock()
                await work.tx.mowers.update(item)
            await work.tx.base_stations.delete(id=base.id, account_id=account_id)
            await work.audit(
                actor,
                "base_station.removed",
                account_id,
                "base_station",
                base.id,
                {"mowers": [str(m.id) for m in mowers]},
            )
            await self._republish(work, mowers)
        _log.info("base station removed", base_station_id=str(base_station_id))

    async def link_mower(
        self, actor: Actor, account_id: UUID, mower_id: UUID, base_station_id: UUID | None
    ) -> Mower:
        """Link the mower to a base station of its account, or unlink it with None (the
        caller holds ``can_manage_mowers`` on the mower). 404 for a base station of another
        account; 409 ``base-station-not-applicable`` when the base station cannot serve the
        mower (:func:`serves`). Audits and publishes ``mower.updated`` when it changed."""
        async with self.uow() as work:
            item = await work.tx.mowers.get(id=mower_id, account_id=account_id)
            if item is None:
                raise NotFoundError("mower not found")
            if base_station_id is not None:
                base = await self._load(work.tx, account_id, base_station_id)
                if not serves(base, item):
                    msg = (
                        "a simulated base station serves simulated mowers only"
                        if base.source == SOURCE_SIMULATED
                        else "a base station serves cloud mowers of its own linked account only"
                    )
                    raise BaseStationNotApplicableError(msg)
            if item.base_station_id == base_station_id:
                return item
            previous = item.base_station_id
            item.base_station_id = base_station_id
            item.updated_at = self.clock()
            await work.tx.mowers.update(item)
            if base_station_id is None:
                await work.audit(
                    actor,
                    "mower.base_station_removed",
                    account_id,
                    "mower",
                    item.id,
                    {"base_station_id": str(previous)},
                )
            else:
                await work.audit(
                    actor,
                    "mower.base_station_set",
                    account_id,
                    "mower",
                    item.id,
                    {"base_station_id": str(base_station_id)},
                )
            await self._republish(work, [item])
        return item

    # --- firmware ----------------------------------------------------------------------------

    async def firmware_check(
        self, actor: Actor, account_id: UUID, base_station_id: UUID
    ) -> FirmwareInfo:
        """Ask the vendor for the base station's firmware through a linked mower
        (``can_view`` on the account): each linked mower's gateway in turn until one answers.
        409 ``base-station-not-linked`` without a linked mower, 503 when none answered."""
        await self._require(actor, CAN_VIEW, account_id)
        async with self.store.transaction() as tx:
            base = await self._load(tx, account_id, base_station_id)
            mowers = await self._linked(tx, base)
        if not mowers:
            raise BaseStationNotLinkedError(
                "link a mower first: the base station is asked through it"
            )
        last = NO_GATEWAY
        for item in mowers:
            request = FirmwareCheckRequest(
                mower_id=item.id, account_id=item.account_id, target="base_station"
            )
            try:
                reply = await self.firmware.request(request)
            except Exception as exc:
                _log.info(
                    "base station firmware check through a mower failed",
                    mower_id=str(item.id),
                    error=type(exc).__name__,
                )
                continue
            if reply.info is not None:
                return reply.info
            if reply.error is not None and reply.error.code is not FirmwareCheckErrorCode.FORBIDDEN:
                last = reply.error.detail or last
        raise Unavailable(last)

    async def firmware_update(
        self,
        actor: Actor,
        account_id: UUID,
        base_station_id: UUID,
        version: str,
        mower_id: UUID | None = None,
    ) -> CommandAccepted:
        """Install the offered firmware on the base station (``can_manage_mowers`` on the
        account): the command ``base_station_firmware_update`` goes to ``mower_id`` (a mower
        linked to the base station) or the first linked mower by name, with its command record.
        409 ``base-station-not-linked`` without a linked mower, 422 for a mower that is not
        linked to it."""
        await self._require(actor, CAN_MANAGE_MOWERS, account_id)
        async with self.store.transaction() as tx:
            base = await self._load(tx, account_id, base_station_id)
            mowers = await self._linked(tx, base)
        if not mowers:
            raise BaseStationNotLinkedError("link a mower first: the update is sent through it")
        target = mowers[0]
        if mower_id is not None:
            chosen = next((m for m in mowers if m.id == mower_id), None)
            if chosen is None:
                msg = "the mower is not linked to this base station"
                raise InvalidInput(msg, fields=[FieldProblem("mower_id", msg, "not_linked")])
            target = chosen
        request = CommandRequest(command=BaseStationFirmwareUpdate(version=version))
        return await self.commands.send(actor, account_id, target.id, request)
