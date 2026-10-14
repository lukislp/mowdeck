"""Mowers: register, list, read, change, move between groups, remove; the registry answer for
the Device-Gateway.

Cloud mowers (ADR 0034) come from a linked Mammotion account: a registration names the linked
account and the device; Fleet stores the device name and IoT id as options and merges the
account's credentials into the registration the Device-Gateway receives. One physical mower
(its IoT id) can be registered in one MowDeck account only, installation-wide."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from mowdeck_authz import account, group, site
from mowdeck_contracts.v1 import (
    MOWER_REGISTERED,
    MOWER_REMOVED,
    MOWER_UPDATED,
    DiscoveredDevice,
    MowerRegistration,
    ResolveTargetReply,
    ResolveTargetRequest,
    TargetMower,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.entitlements import Entitlements

from fleet.application.access import Access
from fleet.application.area_names import names_by_mower, names_of
from fleet.application.context import Actor, FleetConfig
from fleet.application.hierarchy import mower_group_parent, mower_site_parent, mower_tuples
from fleet.application.linked_accounts import (
    BulkItem,
    CredentialVault,
    find_device,
    public_registration,
    unique_names,
)
from fleet.application.plan import allow_all, require_limit
from fleet.application.ports import Clock, In, Store, Tx
from fleet.application.sites import SiteService
from fleet.application.work import UnitOfWork, Work
from fleet.domain.connections import LEGACY_CLOUD_SCHEMA, SCHEMAS, links_supported
from fleet.domain.errors import (
    ConflictError,
    FieldProblem,
    FleetError,
    InvalidInput,
    MowerAlreadyRegisteredError,
    NotFoundError,
)
from fleet.domain.models import LinkedAccount, MowDefaults, Mower, MowerGroup, MowerLink
from fleet.domain.rules import (
    CAN_MANAGE_MOWERS,
    clean_name,
    in_shard,
    keep_masked_secrets,
    plain_secret_options,
    validate_connection_options,
)

_SERIAL_EXISTS = "a mower with this serial number is registered in the account"
_ELSEWHERE = "this mower is already registered in MowDeck"
_CLOUD_MANAGED = frozenset({"device_name", "iot_id"})
"""Options of a linked cloud mower that Fleet sets from the device list."""
_LINKED_FORBIDDEN = frozenset({"account", "password", "credential_ref", "linked_account_id"})
"""Options a linked cloud mower must not carry (the linked account holds them)."""
_log = get_logger("fleet.mowers")


@dataclass(frozen=True, slots=True)
class MowerInput:
    """What a registration says."""

    name: str
    connection_kind: str
    connection_options: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    serial: str | None = None
    site_id: UUID | None = None
    group_ids: tuple[UUID, ...] = ()
    linked_account_id: UUID | None = None
    """``cloud``: the linked account the mower connects through."""
    device_name: str | None = None
    """``cloud``: the device of the linked account (as its device list names it)."""


@dataclass(frozen=True, slots=True)
class MowerView:
    """A mower with the ids of its groups, the custom names of its map areas and its
    additional links (ADR 0038)."""

    mower: Mower
    group_ids: list[UUID]
    area_names: dict[str, str] = field(default_factory=dict)
    links: list[MowerLink] = field(default_factory=list)


def audited_defaults(defaults: MowDefaults | None) -> dict[str, Any] | None:
    """Mowing defaults as the audit log records them (``{"pattern": {...}}``, JSON)."""
    if defaults is None:
        return None
    return {"pattern": defaults.pattern.model_dump(mode="json", exclude_none=True)}


def registration(item: Mower) -> MowerRegistration:
    """The published registration (never with secrets, see :func:`public_registration`)."""
    return public_registration(item)


class MowerService:
    """Use cases of mowers."""

    def __init__(
        self,
        uow: UnitOfWork,
        store: Store,
        access: Access,
        sites: SiteService,
        config: FleetConfig,
        clock: Clock,
        entitlements: Entitlements | None = None,
        vault: CredentialVault | None = None,
    ) -> None:
        self.uow = uow
        self.store = store
        self.access = access
        self.sites = sites
        self.config = config
        self.clock = clock
        self.entitlements = entitlements if entitlements is not None else allow_all()
        self.vault = vault

    # --- validation ----------------------------------------------------------------------

    async def _registration(self, tx: Tx, item: Mower) -> MowerRegistration:
        if self.vault is None:
            return registration(item)
        return await self.vault.registration(tx, item)

    def _allowed(self, kind: str) -> None:
        if kind not in self.config.allowed_connection_kinds:
            allowed = ", ".join(sorted(self.config.allowed_connection_kinds))
            msg = f"connection_kind {kind!r} is not enabled here (allowed: {allowed})"
            raise InvalidInput(msg, fields=[FieldProblem("connection_kind", msg, "not_allowed")])

    def _connection(
        self,
        kind: str,
        options: Mapping[str, Any],
        *,
        linked: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Validate ``options`` for ``kind``. ``linked``: a cloud mower of a linked account
        (only the adapter extras; the managed keys are set by the caller). Otherwise a cloud
        mower is one registered before linked accounts (old schema with its own account);
        :meth:`_legacy_cloud_patch` guards its updates."""
        self._allowed(kind)
        schema = None
        if kind == "cloud" and linked:
            forbidden = sorted((set(options) & _LINKED_FORBIDDEN) | (set(options) & _CLOUD_MANAGED))
            if forbidden:
                msg = "a mower of a linked account takes no credentials or device keys"
                raise InvalidInput(
                    msg,
                    fields=[
                        FieldProblem(f"connection_options/{key}", msg, "not_allowed")
                        for key in forbidden
                    ],
                )
            schema = SCHEMAS["cloud"]
        elif kind == "cloud":
            schema = LEGACY_CLOUD_SCHEMA
        checked = validate_connection_options(
            options, self.config.connection_options_max_bytes, kind, schema
        )
        plain = plain_secret_options(checked)
        if plain:
            # TODO(phase 3): reject plain secrets once a secret store exists (ADR 0019).
            _log.warning(
                "connection options store a secret in plain text; use credential_ref",
                connection_kind=kind,
                keys=plain,
            )
        return kind, checked

    @staticmethod
    def _legacy_cloud_patch(merged: Mapping[str, Any], stored: Mapping[str, Any]) -> None:
        """A cloud mower registered before linked accounts may keep its stored credentials,
        but a PATCH must not introduce a new plain password or change the Fleet-managed IoT id
        (422)."""
        password = merged.get("password")
        if password not in (None, "") and password != stored.get("password"):
            msg = "link the Mammotion account instead of storing its password with the mower"
            raise InvalidInput(
                msg,
                fields=[FieldProblem("connection_options/password", msg, "use_linked_account")],
            )
        if merged.get("iot_id") != stored.get("iot_id"):
            msg = "the IoT id is set by Fleet from a linked account's device list"
            raise InvalidInput(
                msg, fields=[FieldProblem("connection_options/iot_id", msg, "not_allowed")]
            )

    async def _check_targets(
        self, actor: Actor, account_id: UUID, site_id: UUID | None, group_ids: Iterable[UUID]
    ) -> None:
        """``can_manage_mowers`` on the target site (or the account for the default site) and
        on every group; foreign or unknown targets are 404."""
        if site_id is None:
            await self.access.require(
                actor.principal, CAN_MANAGE_MOWERS, account(account_id), hide=False
            )
        else:
            await self.access.require(actor.principal, CAN_MANAGE_MOWERS, site(site_id))
        for group_id in sorted(set(group_ids), key=str):
            await self.access.require(actor.principal, CAN_MANAGE_MOWERS, group(group_id))

    @staticmethod
    async def _groups_exist(tx: Tx, account_id: UUID, group_ids: Iterable[UUID]) -> None:
        wanted = set(group_ids)
        if not wanted:
            return
        found = await tx.groups.find(id=In(sorted(wanted, key=str)), account_id=account_id)
        if len(found) != len(wanted):
            raise NotFoundError("group not found")

    # --- reading -------------------------------------------------------------------------

    async def visible(
        self,
        actor: Actor,
        account_id: UUID,
        *,
        site_id: UUID | None = None,
        group_id: UUID | None = None,
    ) -> list[MowerView]:
        """Mowers of the account the actor may view, by name, optionally of a site or group."""
        visible = await self.access.visible_ids(actor.principal, "mower", account_id)
        where: dict[str, Any] = {"account_id": account_id}
        if site_id is not None:
            where["site_id"] = site_id
        async with self.store.transaction() as tx:
            rows = await tx.mowers.find(order_by="name", **where)
            memberships = await tx.mower_groups.find(account_id=account_id)
            names = await names_by_mower(tx, account_id)
            links = await tx.mower_links.find(account_id=account_id, order_by="kind")
        groups: dict[UUID, list[UUID]] = {}
        for membership in memberships:
            groups.setdefault(membership.mower_id, []).append(membership.group_id)
        links_of: dict[UUID, list[MowerLink]] = {}
        for link in links:
            links_of.setdefault(link.mower_id, []).append(link)
        result = []
        for row in rows:
            if visible is not None and row.id not in visible:
                continue
            member_of = sorted(groups.get(row.id, []), key=str)
            if group_id is not None and group_id not in member_of:
                continue
            result.append(
                MowerView(row, member_of, names.get(row.id, {}), links_of.get(row.id, []))
            )
        return result

    async def get(self, account_id: UUID, mower_id: UUID) -> MowerView:
        """One mower of the account (404 for any other account)."""
        async with self.store.transaction() as tx:
            row = await tx.mowers.get(id=mower_id, account_id=account_id)
            if row is None:
                raise NotFoundError("mower not found")
            memberships = await tx.mower_groups.find(mower_id=mower_id)
            names = await names_of(tx, mower_id)
            links = await tx.mower_links.find(mower_id=mower_id, order_by="kind")
        return MowerView(row, sorted((m.group_id for m in memberships), key=str), names, links)

    async def registrations_for_shard(
        self, shard_index: int, shard_count: int
    ) -> list[MowerRegistration]:
        """Every mower of shard ``index`` of ``count`` (system path: all accounts), with the
        credentials of linked accounts merged in."""
        cache: dict[UUID, LinkedAccount | None] = {}
        async with self.store.transaction() as tx:
            rows = await tx.mowers.find(order_by="id")
            mine = [m for m in rows if in_shard(m.id, shard_index, shard_count)]
            if self.vault is None:
                return [registration(m) for m in mine]
            links_of: dict[UUID, list[MowerLink]] = {}
            for link in await tx.mower_links.find(order_by="kind"):
                links_of.setdefault(link.mower_id, []).append(link)
            return [
                await self.vault.registration(tx, m, cache, links_of.get(m.id, [])) for m in mine
            ]

    async def resolve_target(self, request: ResolveTargetRequest) -> ResolveTargetReply:
        """``fleet.targets.resolve``: the mowers of a mower or group target of one account,
        with the time zone and coordinates of their sites (system path for the Scheduler)."""
        account_id = request.account_id
        async with self.store.transaction() as tx:
            if request.target_type == "mower":
                found = await tx.mowers.get(id=request.target_id, account_id=account_id)
                members = [found] if found is not None else []
                if found is None:
                    return ResolveTargetReply(found=False)
                home: UUID | None = found.site_id
            else:
                found_group = await tx.groups.get(id=request.target_id, account_id=account_id)
                if found_group is None:
                    return ResolveTargetReply(found=False)
                ids = [m.mower_id for m in await tx.mower_groups.find(group_id=found_group.id)]
                members = (
                    await tx.mowers.find(
                        id=In(sorted(ids, key=str)), account_id=account_id, order_by="name"
                    )
                    if ids
                    else []
                )
                home = found_group.site_id
            sites = {s.id: s for s in await tx.sites.find(account_id=account_id)}
        if home is None:  # a cross-site group: the account's default site
            default = next((s for s in sites.values() if s.is_default), None)
            home = default.id if default is not None else None
        home_site = sites.get(home) if home is not None else None
        timezone = home_site.timezone if home_site is not None else self.config.default_timezone
        return ResolveTargetReply(
            found=True,
            timezone=timezone,
            mowers=[
                TargetMower(
                    mower_id=m.id,
                    name=m.name,
                    site_id=m.site_id,
                    timezone=sites[m.site_id].timezone,
                    latitude=sites[m.site_id].latitude,
                    longitude=sites[m.site_id].longitude,
                    mow_pattern=m.mow_defaults.pattern if m.mow_defaults is not None else None,
                )
                for m in members
                if m.site_id in sites
            ],
        )

    # --- changes -------------------------------------------------------------------------

    async def register(self, actor: Actor, account_id: UUID, data: MowerInput) -> MowerView:
        """Register a mower: row, tuples (``account``, ``parent`` site and groups), event.

        Needs ``can_manage_mowers`` on the target site (the account when the default site is
        used) and on each group. The ``mower.registered`` event follows the commit, so the
        Device-Gateway only connects a mower whose tuples exist.
        """
        if actor.subject_id is None:
            raise NotFoundError("mower not found")
        self._allowed(data.connection_kind)
        # New cloud mowers always come from a linked account (the only discriminator).
        linked = data.connection_kind == "cloud"
        if data.connection_kind != "cloud" and (
            data.linked_account_id is not None or data.device_name is not None
        ):
            msg = "linked_account_id and device_name belong to cloud mowers only"
            raise InvalidInput(msg, fields=[FieldProblem("linked_account_id", msg, "not_allowed")])
        if linked:
            missing = [
                FieldProblem(key, f"{key} is required for a cloud mower", "required")
                for key, value in (
                    ("linked_account_id", data.linked_account_id),
                    ("device_name", data.device_name),
                )
                if not value
            ]
            if missing:
                raise InvalidInput("a cloud mower comes from a linked account", fields=missing)
        kind, options = self._connection(
            data.connection_kind, data.connection_options, linked=linked
        )
        group_ids = sorted(set(data.group_ids), key=str)
        await self._check_targets(actor, account_id, data.site_id, group_ids)
        now = self.clock()
        async with self.uow() as work:
            registered = await work.tx.mowers.count(account_id=account_id)
            if registered >= self.config.max_mowers_per_account:
                msg = f"the account has reached its limit of {self.config.max_mowers_per_account}"
                raise ConflictError(f"{msg} mowers")
            await require_limit(self.entitlements, account_id, "max_mowers", registered)
            if data.site_id is None:
                site_id = (await self.sites.ensure_default_in(work, account_id)).id
            else:
                found = await work.tx.sites.get(id=data.site_id, account_id=account_id)
                if found is None:
                    raise NotFoundError("site not found")
                site_id = found.id
            await self._groups_exist(work.tx, account_id, group_ids)
            model = data.model
            linked_account_id: UUID | None = None
            iot_id: str | None = None
            if linked:
                assert data.linked_account_id is not None and data.device_name is not None  # noqa: S101
                linked_account_id = data.linked_account_id
                device = await self._linked_device(
                    work.tx, account_id, data.linked_account_id, data.device_name
                )
                iot_id = device.iot_id
                options = {**options, "device_name": device.device_name}
                if device.iot_id:
                    options["iot_id"] = device.iot_id
                model = model or device.model
                await self._not_registered(
                    work.tx, account_id, iot_id, data.linked_account_id, device.device_name
                )
            item = Mower(
                id=uuid4(),
                account_id=account_id,
                site_id=site_id,
                name=clean_name(data.name or data.device_name or "", "mower"),
                connection_kind=kind,
                connection_options=options,
                model=model,
                serial=data.serial,
                created_by=actor.subject_id,
                created_at=now,
                updated_at=now,
                linked_account_id=linked_account_id,
                iot_id=iot_id or None,
            )
            try:
                await work.tx.mowers.add(item)
            except ConflictError:
                if iot_id and await work.tx.mowers.get(iot_id=iot_id) is not None:
                    raise MowerAlreadyRegisteredError(_ELSEWHERE) from None
                raise ConflictError(_SERIAL_EXISTS) from None
            for group_id in group_ids:
                await work.tx.mower_groups.add(MowerGroup(uuid4(), item.id, group_id, account_id))
            work.write(*mower_tuples(item, group_ids))
            await work.audit(
                actor,
                "mower.registered",
                account_id,
                "mower",
                item.id,
                {
                    "name": item.name,
                    "connection_kind": kind,
                    "site_id": str(site_id),
                    **({"linked_account_id": str(linked_account_id)} if linked_account_id else {}),
                },
            )
            work.emit(MOWER_REGISTERED, account_id, await self._registration(work.tx, item))
        return MowerView(item, group_ids)

    @staticmethod
    async def _linked_device(
        tx: Tx, account_id: UUID, linked_account_id: UUID, device_name: str
    ) -> DiscoveredDevice:
        linked = await tx.linked_accounts.get(id=linked_account_id, account_id=account_id)
        if linked is None:
            msg = "linked account not found"
            raise InvalidInput(msg, fields=[FieldProblem("linked_account_id", msg, "not_found")])
        device = find_device(linked, device_name)
        if device is None:
            msg = f"the linked account has no device {device_name!r}; read the account again"
            raise InvalidInput(msg, fields=[FieldProblem("device_name", msg, "unknown_device")])
        if not device.is_mower:
            msg = f"{device_name} is not a mower (RTK base stations cannot be registered)"
            raise InvalidInput(msg, fields=[FieldProblem("device_name", msg, "not_a_mower")])
        return device

    @staticmethod
    async def _not_registered(
        tx: Tx,
        account_id: UUID,
        iot_id: str | None,
        linked_account_id: UUID | None,
        device_name: str,
    ) -> None:
        """409 ``mower-already-registered`` when the physical mower is registered already: by
        its IoT id in any account (the other account is never named), or by device name through
        the same linked account."""
        if iot_id:
            holder = await tx.mowers.get(iot_id=iot_id)
            if holder is not None:
                if holder.account_id == account_id:
                    msg = f"this mower is already registered in the account as {holder.name}"
                    raise MowerAlreadyRegisteredError(msg)
                raise MowerAlreadyRegisteredError(_ELSEWHERE)
        if linked_account_id is not None:
            for other in await tx.mowers.find(
                account_id=account_id, linked_account_id=linked_account_id
            ):
                if other.connection_options.get("device_name") == device_name:
                    msg = f"this mower is already registered in the account as {other.name}"
                    raise MowerAlreadyRegisteredError(msg)

    async def register_from_linked_account(
        self,
        actor: Actor,
        account_id: UUID,
        linked_account_id: UUID,
        items: list[BulkItem],
    ) -> tuple[list[MowerView], list[tuple[str, FleetError]]]:
        """Register several mowers of one linked account, each in its own registration
        (tuples, event, audit). Returns the created mowers and the failures per device; when
        none was created the first failure is raised."""
        unique_names(items)
        async with self.store.transaction() as tx:
            if await tx.linked_accounts.get(id=linked_account_id, account_id=account_id) is None:
                raise NotFoundError("linked account not found")
        created: list[MowerView] = []
        failed: list[tuple[str, FleetError]] = []
        for item in items:
            try:
                created.append(
                    await self.register(
                        actor,
                        account_id,
                        MowerInput(
                            name=item.name or item.device_name,
                            connection_kind="cloud",
                            serial=item.serial,
                            site_id=item.site_id,
                            group_ids=item.group_ids,
                            linked_account_id=linked_account_id,
                            device_name=item.device_name,
                        ),
                    )
                )
            except FleetError as exc:
                failed.append((item.device_name, exc))
        if not created and failed:
            raise failed[0][1]
        return created, failed

    async def update(
        self, actor: Actor, account_id: UUID, mower_id: UUID, changes: Mapping[str, Any]
    ) -> MowerView:
        """Change name, model, serial, connection or site (``changes`` holds given fields).

        Moving to another site needs ``can_manage_mowers`` there and rewrites
        ``mower#parent@site``. A change of name, connection or site publishes ``mower.updated``.
        """
        new_site = changes.get("site_id")
        if new_site is not None:
            await self.access.require(actor.principal, CAN_MANAGE_MOWERS, site(new_site))
        async with self.uow() as work:
            item = await work.tx.mowers.get(id=mower_id, account_id=account_id)
            if item is None:
                raise NotFoundError("mower not found")
            before = (item.name, item.connection_kind, item.connection_options, item.site_id)
            if changes.get("name") is not None:
                item.name = clean_name(str(changes["name"]), "mower")
            if "model" in changes:
                item.model = changes["model"]
            if "serial" in changes:
                item.serial = changes["serial"]
            new_options = changes.get("connection_options")
            if changes.get("connection_kind") is not None or new_options is not None:
                kind = changes.get("connection_kind") or item.connection_kind
                if kind != "cloud":
                    item.linked_account_id = None
                    item.iot_id = None
                elif item.connection_kind != "cloud":
                    msg = "a cloud mower comes from a linked account; register it from there"
                    raise InvalidInput(
                        msg, fields=[FieldProblem("connection_kind", msg, "use_linked_account")]
                    )
                if kind == "cloud" and item.linked_account_id is not None:
                    managed = {
                        k: v for k, v in item.connection_options.items() if k in _CLOUD_MANAGED
                    }
                    extras = {
                        k: v
                        for k, v in (new_options or item.connection_options).items()
                        if k not in _CLOUD_MANAGED
                    }
                    item.connection_kind, checked = self._connection(kind, extras, linked=True)
                    item.connection_options = {**checked, **managed}
                else:
                    merged = (
                        keep_masked_secrets(new_options, item.connection_options)
                        if new_options is not None
                        else item.connection_options
                    )
                    if kind == "cloud":
                        self._legacy_cloud_patch(merged, item.connection_options)
                    item.connection_kind, item.connection_options = self._connection(kind, merged)
            if new_site is not None and new_site != item.site_id:
                if await work.tx.sites.get(id=new_site, account_id=account_id) is None:
                    raise NotFoundError("site not found")
                work.delete(mower_site_parent(item.id, item.site_id))
                work.write(mower_site_parent(item.id, new_site))
                item.site_id = new_site
            item.updated_at = self.clock()
            try:
                await work.tx.mowers.update(item)
            except ConflictError:
                raise ConflictError(_SERIAL_EXISTS) from None
            dropped = await self._drop_unsupported_links(work, actor, item)
            memberships = await work.tx.mower_groups.find(mower_id=mower_id)
            names = await names_of(work.tx, mower_id)
            links = await work.tx.mower_links.find(mower_id=mower_id, order_by="kind")
            await work.audit(
                actor, "mower.updated", account_id, "mower", mower_id, {"fields": sorted(changes)}
            )
            after = (item.name, item.connection_kind, item.connection_options, item.site_id)
            if after != before or dropped:
                work.emit(MOWER_UPDATED, account_id, await self._registration(work.tx, item))
        return MowerView(item, sorted((m.group_id for m in memberships), key=str), names, links)

    @staticmethod
    async def _drop_unsupported_links(work: Work, actor: Actor, item: Mower) -> bool:
        """After a change of the connection kind: links the new kind cannot carry are removed
        (audited as ``mower.link_removed``, ADR 0038). Returns True when any was removed."""
        allowed = links_supported(item.connection_kind)
        removed = False
        for link in await work.tx.mower_links.find(mower_id=item.id):
            if link.kind in allowed:
                continue
            await work.tx.mower_links.delete(id=link.id)
            await work.audit(
                actor,
                "mower.link_removed",
                item.account_id,
                "mower",
                item.id,
                {"kind": link.kind, "reason": "connection_kind_changed"},
            )
            removed = True
        return removed

    async def set_mow_defaults(
        self,
        actor: Actor,
        account_id: UUID,
        mower_id: UUID,
        defaults: MowDefaults | None,
    ) -> MowerView:
        """Keep ``defaults`` as the mower's mowing defaults; None drops them (clients fall back
        to stripes with one border lap). Nothing is sent to the mower. An unchanged value
        changes nothing (no audit row); a change is audited as ``mower.mow_defaults_changed``."""
        async with self.uow() as work:
            item = await work.tx.mowers.get(id=mower_id, account_id=account_id)
            if item is None:
                raise NotFoundError("mower not found")
            if item.mow_defaults != defaults:
                previous = item.mow_defaults
                await work.tx.mowers.update_where({"mow_defaults": defaults}, id=mower_id)
                item.mow_defaults = defaults
                await work.audit(
                    actor,
                    "mower.mow_defaults_changed",
                    account_id,
                    "mower",
                    mower_id,
                    {
                        "mow_defaults": audited_defaults(defaults),
                        "previous": audited_defaults(previous),
                    },
                )
            memberships = await work.tx.mower_groups.find(mower_id=mower_id)
            names = await names_of(work.tx, mower_id)
            links = await work.tx.mower_links.find(mower_id=mower_id, order_by="kind")
        return MowerView(item, sorted((m.group_id for m in memberships), key=str), names, links)

    async def set_groups(
        self, actor: Actor, account_id: UUID, mower_id: UUID, group_ids: Iterable[UUID]
    ) -> MowerView:
        """Make the mower a member of exactly ``group_ids``.

        Needs ``can_manage_mowers`` on every added or removed group; writes and deletes the
        ``mower#parent@group`` tuples in one transaction and publishes ``mower.updated``.
        """
        wanted = set(group_ids)
        async with self.store.transaction() as tx:
            if await tx.mowers.get(id=mower_id, account_id=account_id) is None:
                raise NotFoundError("mower not found")
            current = {link.group_id for link in await tx.mower_groups.find(mower_id=mower_id)}
        for group_id in sorted(wanted ^ current, key=str):
            await self.access.require(actor.principal, CAN_MANAGE_MOWERS, group(group_id))
        async with self.uow() as work:
            item = await work.tx.mowers.get(id=mower_id, account_id=account_id)
            if item is None:
                raise NotFoundError("mower not found")
            current = {link.group_id for link in await work.tx.mower_groups.find(mower_id=mower_id)}
            added, removed = wanted - current, current - wanted
            names = await names_of(work.tx, mower_id)
            await self._groups_exist(work.tx, account_id, added)
            for group_id in sorted(added, key=str):
                await work.tx.mower_groups.add(MowerGroup(uuid4(), mower_id, group_id, account_id))
                work.write(mower_group_parent(mower_id, group_id))
            for group_id in sorted(removed, key=str):
                await work.tx.mower_groups.delete(mower_id=mower_id, group_id=group_id)
                work.delete(mower_group_parent(mower_id, group_id))
            if added or removed:
                await work.audit(
                    actor,
                    "mower.moved",
                    account_id,
                    "mower",
                    mower_id,
                    {
                        "added": [str(g) for g in sorted(added, key=str)],
                        "removed": [str(g) for g in sorted(removed, key=str)],
                    },
                )
                work.emit(MOWER_UPDATED, account_id, await self._registration(work.tx, item))
            links = await work.tx.mower_links.find(mower_id=mower_id, order_by="kind")
        return MowerView(item, sorted(wanted, key=str), names, links)

    async def delete(self, actor: Actor, account_id: UUID, mower_id: UUID) -> None:
        """Remove the mower and all its tuples; publish ``mower.removed``. Command records stay
        until their retention ends."""
        async with self.uow() as work:
            item = await work.tx.mowers.get(id=mower_id, account_id=account_id)
            if item is None:
                raise NotFoundError("mower not found")
            memberships = await work.tx.mower_groups.find(mower_id=mower_id)
            await work.tx.mower_groups.delete(mower_id=mower_id)
            await work.tx.area_names.delete(mower_id=mower_id)
            await work.tx.mower_links.delete(mower_id=mower_id)
            await work.tx.mowers.delete(id=mower_id)
            work.delete(*mower_tuples(item, [m.group_id for m in memberships]))
            await work.audit(actor, "mower.removed", account_id, "mower", mower_id)
            work.emit(MOWER_REMOVED, account_id, registration(item))
