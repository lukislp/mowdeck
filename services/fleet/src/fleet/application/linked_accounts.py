"""Linked vendor cloud accounts (ADR 0034): link, list, re-read, delete; their device lists;
the withheld credentials the Device-Gateway fetches for their mowers.

Fleet never talks to the Mammotion cloud (that code is GPL-3.0 and lives in the
Device-Gateway): every login and device listing is a ``gateway.cloud.discover`` request. The
password is stored encrypted (:class:`CredentialVault`) and leaves Fleet only in two internal
core NATS replies: the discovery request and ``fleet.mowers.credentials``, which the gateway
sends each time it opens a mower's connection. Registrations (``mower.*`` events, which
JetStream keeps for 30 days and several services read, and ``fleet.mowers.list``) never carry
a password, an ESPHome key or the vendor account (:func:`public_registration`); the same
holds for the additional links of a mower (ADR 0038): the registration carries their public
options, the credentials reply their secrets (``link_secrets``).

Who may do what: managing linked accounts (and seeing their e-mail and device lists) needs
``can_manage_mowers`` on the account (403 otherwise; existence of the caller's own account is
no secret). Registering a mower from a linked account needs what any registration needs (the
target site and groups). Failed vendor logins are throttled per account and e-mail
(:mod:`fleet.application.throttle`).
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

from mowdeck_authz import account
from mowdeck_contracts.v1 import (
    CREDENTIALS_FROM_FLEET,
    CREDENTIALS_OPTION,
    MOWER_UPDATED,
    SECRET_OPTION_KEYS,
    BaseStationRef,
    CloudDiscoverErrorCode,
    CloudDiscoverRequest,
    CloudProvider,
    ConnectionKind,
    DeviceType,
    DiscoveredDevice,
    MowerCredentialsReply,
    MowerCredentialsRequest,
    MowerRegistration,
)
from mowdeck_contracts.v1.links import CREDENTIALS_REV_OPTION
from mowdeck_service_kit import get_logger

from fleet.application.access import Access
from fleet.application.context import Actor
from fleet.application.ports import Clock, CloudDiscovery, In, SecretCipher, Store, Tx
from fleet.application.throttle import LoginThrottle
from fleet.application.work import UnitOfWork
from fleet.domain.errors import (
    ConflictError,
    FieldProblem,
    InvalidInput,
    LinkedAccountInUseError,
    LinkedAccountLoginError,
    NotFoundError,
    Unavailable,
)
from fleet.domain.models import BaseStation, LinkedAccount, Mower, MowerLink
from fleet.domain.rules import CAN_MANAGE_MOWERS, validate_email

_log = get_logger("fleet.linked_accounts")

_DISABLED = (
    "linked Mammotion accounts need MOWDECK_CREDENTIAL_ENCRYPTION_KEY in Fleet's environment"
)

LOGIN_REFUSED: Final = "the Mammotion cloud refused the e-mail or password"
"""Canned message (and ``last_sync_error``) of a refused login."""
CLOUD_UNAVAILABLE: Final = "the Mammotion cloud or the Device-Gateway did not answer"
"""Canned message (and ``last_sync_error``) when no device list came back."""
REVISION_OPTION: Final = "credentials_rev"
"""Non-secret fingerprint of the stored password in a registration: a new password changes it,
so the Device-Gateway reconnects and fetches the new one."""


@dataclass(frozen=True, slots=True)
class LinkedAccountView:
    """A linked account with the number of mowers that connect through it."""

    account: LinkedAccount
    mower_count: int


@dataclass(frozen=True, slots=True)
class DeviceView:
    """A device of a linked account and where it is registered.

    ``mower_id`` is set when the device is a mower of the caller's account; ``elsewhere`` when
    another MowDeck account registered it (its name and account stay hidden).
    """

    device: DiscoveredDevice
    mower_id: UUID | None = None
    elsewhere: bool = False
    base_station_id: UUID | None = None
    """Set when the device is an RTK base station of the caller's account (ADR 0047)."""


@dataclass(frozen=True, slots=True)
class BulkItem:
    """One mower of a bulk registration from a linked account."""

    device_name: str
    name: str | None = None
    site_id: UUID | None = None
    group_ids: tuple[UUID, ...] = ()
    serial: str | None = None


def devices_of(item: LinkedAccount) -> list[DiscoveredDevice]:
    """The cached device list of a linked account (rows that no longer parse are skipped)."""
    found: list[DiscoveredDevice] = []
    for raw in item.devices:
        try:
            found.append(DiscoveredDevice.model_validate(raw))
        except ValueError:
            continue
    return found


def find_device(item: LinkedAccount, device_name: str) -> DiscoveredDevice | None:
    """The cached device named ``device_name`` (exact, as the adapter selects it)."""
    return next((d for d in devices_of(item) if d.device_name == device_name), None)


def _revision(sealed: bytes) -> str:
    return hashlib.sha256(sealed).hexdigest()[:12]


def public_link(link: MowerLink) -> dict[str, Any]:
    """The options of a link as a registration carries them: the public options, never the
    secrets, plus ``credentials: "fleet"`` and ``credentials_rev`` (a fingerprint of the sealed
    secrets: a new key changes it, so the Device-Gateway fetches the secrets again)."""
    return {
        **{k: v for k, v in link.options.items() if k not in SECRET_OPTION_KEYS},
        CREDENTIALS_OPTION: CREDENTIALS_FROM_FLEET,
        CREDENTIALS_REV_OPTION: _revision(link.secrets_encrypted),
    }


def base_station_ref(base: BaseStation) -> BaseStationRef:
    """The base station as a registration carries it (ADR 0047; no secrets in it)."""
    return BaseStationRef(
        base_station_id=base.id,
        name=base.name,
        source="simulated" if base.source == "simulated" else "cloud",
        device_name=base.device_name,
        iot_id=base.iot_id,
    )


def public_registration(
    item: Mower,
    linked: LinkedAccount | None = None,
    links: Sequence[MowerLink] = (),
    base: BaseStation | None = None,
) -> MowerRegistration:
    """The registration as Fleet publishes it: without passwords, ESPHome keys and the vendor
    account. When anything was withheld (or the mower uses a linked account) the options say
    ``credentials: "fleet"`` and the gateway fetches the secrets on ``fleet.mowers.credentials``.
    ``links`` are the mower's additional links (ADR 0038), published by kind under the same
    rule (:func:`public_link`); ``base`` is the mower's RTK base station (ADR 0047).
    """
    stored = item.connection_options
    options: dict[str, Any] = {k: v for k, v in stored.items() if k not in SECRET_OPTION_KEYS}
    withheld = any(stored.get(k) not in (None, "") for k in SECRET_OPTION_KEYS)
    if item.linked_account_id is not None:
        options["linked_account_id"] = str(item.linked_account_id)
        if linked is not None:
            options[REVISION_OPTION] = _revision(linked.password_encrypted)
        withheld = True
    if withheld:
        options[CREDENTIALS_OPTION] = CREDENTIALS_FROM_FLEET
    return MowerRegistration(
        mower_id=item.id,
        account_id=item.account_id,
        name=item.name,
        connection_kind=ConnectionKind(item.connection_kind),
        connection_options=options,
        links={link.kind: public_link(link) for link in sorted(links, key=lambda x: x.kind)},
        base_station=base_station_ref(base)
        if base is not None and base.id == item.base_station_id
        else None,
    )


class CredentialVault:
    """Seals and opens linked account passwords; builds registrations and credential replies."""

    def __init__(self, cipher: SecretCipher) -> None:
        self.cipher = cipher

    @property
    def enabled(self) -> bool:
        """False without ``MOWDECK_CREDENTIAL_ENCRYPTION_KEY``."""
        return self.cipher.enabled

    def require(self) -> None:
        """Raise :class:`Unavailable` (503) when no key is configured."""
        if not self.enabled:
            raise Unavailable(_DISABLED)

    def seal(self, linked_account_id: UUID, password: str) -> bytes:
        """The stored form of ``password`` (bound to the linked account id)."""
        return self.cipher.encrypt(str(linked_account_id), password)

    def open(self, item: LinkedAccount) -> str:
        """The password of ``item``; ``ValueError`` for a wrong key or a disabled vault."""
        return self.cipher.decrypt(str(item.id), item.password_encrypted)

    def seal_link(self, link_id: UUID, secrets: Mapping[str, str]) -> bytes:
        """The stored form of a link's secret options (a JSON object bound to the link id)."""
        return self.cipher.encrypt(str(link_id), json.dumps(dict(secrets), sort_keys=True))

    def open_link(self, link: MowerLink) -> dict[str, str]:
        """The secret options of ``link``; ``ValueError`` for a wrong key, a disabled vault or
        a blob that is no JSON object of strings."""
        raw = json.loads(self.cipher.decrypt(str(link.id), link.secrets_encrypted))
        if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            msg = "the link secrets are not a JSON object of strings"
            raise ValueError(msg)
        return raw

    async def registration(
        self,
        tx: Tx,
        item: Mower,
        cache: dict[UUID, LinkedAccount | None] | None = None,
        links: Sequence[MowerLink] | None = None,
        bases: Mapping[UUID, BaseStation] | None = None,
    ) -> MowerRegistration:
        """:func:`public_registration` with the linked account loaded (for its revision), the
        mower's links (loaded unless ``links`` are given) and its base station (from ``bases``
        or loaded)."""
        linked_id = item.linked_account_id
        linked: LinkedAccount | None = None
        if linked_id is not None:
            if cache is not None and linked_id in cache:
                linked = cache[linked_id]
            else:
                linked = await tx.linked_accounts.get(id=linked_id, account_id=item.account_id)
                if cache is not None:
                    cache[linked_id] = linked
        if links is None:
            links = await tx.mower_links.find(mower_id=item.id, account_id=item.account_id)
        base: BaseStation | None = None
        if item.base_station_id is not None:
            if bases is not None:
                base = bases.get(item.base_station_id)
            else:
                base = await tx.base_stations.get(
                    id=item.base_station_id, account_id=item.account_id
                )
        return public_registration(item, linked, links, base)

    async def credentials(self, tx: Tx, request: MowerCredentialsRequest) -> MowerCredentialsReply:
        """``fleet.mowers.credentials``: the withheld options of one mower of the account.

        A linked mower gets the account's e-mail and decrypted password; an older mower its
        stored ``account``, ``password`` or ``noise_psk``. Unknown mowers and other accounts:
        ``found`` False. Nothing is logged but the mower id and the outcome.
        """
        item = await tx.mowers.get(id=request.mower_id, account_id=request.account_id)
        if item is None:
            return MowerCredentialsReply(found=False)
        secrets: dict[str, str] = {}
        if item.linked_account_id is not None:
            linked = await tx.linked_accounts.get(
                id=item.linked_account_id, account_id=item.account_id
            )
            if linked is None:
                _log.warning("mower without its linked account", mower_id=str(item.id))
                return MowerCredentialsReply(found=False)
            try:
                secrets = {"account": linked.email, "password": self.open(linked)}
            except ValueError:
                _log.warning(
                    "cannot decrypt the linked account password (wrong or missing key)",
                    mower_id=str(item.id),
                    linked_account_id=str(linked.id),
                )
                return MowerCredentialsReply(found=False)
        else:
            for key in sorted(SECRET_OPTION_KEYS):
                value = item.connection_options.get(key)
                if isinstance(value, str) and value:
                    secrets[key] = value
        link_secrets: dict[str, dict[str, str]] = {}
        for link in await tx.mower_links.find(mower_id=item.id, account_id=item.account_id):
            try:
                link_secrets[link.kind] = self.open_link(link)
            except ValueError:
                # The link stays in the registration; the gateway cannot open it until the
                # owner enters the key again (the connection itself still works).
                _log.warning(
                    "cannot decrypt the link secrets (wrong or missing key)",
                    mower_id=str(item.id),
                    link_kind=link.kind,
                )
        return MowerCredentialsReply(found=True, secret_options=secrets, link_secrets=link_secrets)


class LinkedAccountService:
    """Use cases of linked accounts."""

    def __init__(
        self,
        uow: UnitOfWork,
        store: Store,
        access: Access,
        vault: CredentialVault,
        discovery: CloudDiscovery,
        clock: Clock,
        throttle: LoginThrottle | None = None,
    ) -> None:
        self.uow = uow
        self.store = store
        self.access = access
        self.vault = vault
        self.discovery = discovery
        self.clock = clock
        self.throttle = throttle or LoginThrottle(clock)

    async def _require_manage(self, actor: Actor, account_id: UUID) -> None:
        await self.access.require(
            actor.principal, CAN_MANAGE_MOWERS, account(account_id), hide=False
        )

    @staticmethod
    async def _load(tx: Tx, account_id: UUID, linked_account_id: UUID) -> LinkedAccount:
        found = await tx.linked_accounts.get(id=linked_account_id, account_id=account_id)
        if found is None:
            raise NotFoundError("linked account not found")
        return found

    # --- the cloud ---------------------------------------------------------------------------

    @staticmethod
    def _refused() -> LinkedAccountLoginError:
        return LinkedAccountLoginError(
            LOGIN_REFUSED, fields=[FieldProblem("password", LOGIN_REFUSED, "login_failed")]
        )

    async def _discover(
        self, account_id: UUID, email: str, password: str, linked_account_id: UUID | None
    ) -> list[DiscoveredDevice]:
        """Log in through the gateway, throttled per account and e-mail."""
        key = self.throttle.key(account_id, email)
        if not self.throttle.check(key, password):
            raise self._refused()  # the same wrong password again: the vendor is not asked
        request = CloudDiscoverRequest(
            provider=CloudProvider.MAMMOTION,
            account_id=account_id,
            linked_account_id=linked_account_id,
            email=email,
            password=password,
        )
        try:
            reply = await self.discovery.discover(request)
        except Exception as exc:
            _log.warning("cloud discovery request failed", error=type(exc).__name__)
            raise Unavailable(CLOUD_UNAVAILABLE) from exc
        if reply.error is not None:
            if reply.error.code is CloudDiscoverErrorCode.LOGIN_FAILED:
                self.throttle.failed(key, password)
                raise self._refused()
            _log.warning("cloud discovery failed", code=reply.error.code.value)
            raise Unavailable(CLOUD_UNAVAILABLE)
        self.throttle.succeeded(key)
        return list(reply.devices or [])

    # --- reading -----------------------------------------------------------------------------

    async def visible(self, actor: Actor, account_id: UUID) -> list[LinkedAccountView]:
        """The linked accounts of the caller's account, by e-mail."""
        await self._require_manage(actor, account_id)
        async with self.store.transaction() as tx:
            rows = await tx.linked_accounts.find(account_id=account_id, order_by="email")
            mowers = await tx.mowers.find(account_id=account_id)
        counts: dict[UUID, int] = {}
        for mower in mowers:
            if mower.linked_account_id is not None:
                counts[mower.linked_account_id] = counts.get(mower.linked_account_id, 0) + 1
        return [LinkedAccountView(row, counts.get(row.id, 0)) for row in rows]

    async def get(
        self, actor: Actor, account_id: UUID, linked_account_id: UUID
    ) -> LinkedAccountView:
        """One linked account of the caller's account (404 for any other)."""
        await self._require_manage(actor, account_id)
        async with self.store.transaction() as tx:
            row = await self._load(tx, account_id, linked_account_id)
            count = await tx.mowers.count(account_id=account_id, linked_account_id=row.id)
        return LinkedAccountView(row, count)

    async def devices(
        self, actor: Actor, account_id: UUID, linked_account_id: UUID
    ) -> tuple[LinkedAccount, list[DeviceView]]:
        """The cached device list with the registration of each device."""
        await self._require_manage(actor, account_id)
        async with self.store.transaction() as tx:
            row = await self._load(tx, account_id, linked_account_id)
            views = await self._device_views(tx, row)
        return row, views

    @staticmethod
    async def _device_views(tx: Tx, row: LinkedAccount) -> list[DeviceView]:
        devices = devices_of(row)
        iot_ids = sorted({d.iot_id for d in devices if d.iot_id})
        # System query across accounts: only "registered elsewhere" leaves it, never a name.
        holders = await tx.mowers.find(iot_id=In(iot_ids)) if iot_ids else []
        by_iot = {m.iot_id: m for m in holders}
        own = await tx.mowers.find(account_id=row.account_id, linked_account_id=row.id)
        by_name = {str(m.connection_options.get("device_name")): m for m in own}
        # Base stations (ADR 0047): the same rule, from the base_stations table.
        base_holders = await tx.base_stations.find(iot_id=In(iot_ids)) if iot_ids else []
        bases_by_iot = {b.iot_id: b for b in base_holders}
        own_bases = await tx.base_stations.find(account_id=row.account_id, linked_account_id=row.id)
        bases_by_name = {b.device_name: b for b in own_bases if b.device_name}
        views: list[DeviceView] = []
        for device in devices:
            if device.device_type is DeviceType.RTK_BASE:
                base = bases_by_iot.get(device.iot_id) if device.iot_id else None
                base = base or bases_by_name.get(device.device_name)
                if base is None:
                    views.append(DeviceView(device))
                elif base.account_id == row.account_id:
                    views.append(DeviceView(device, base_station_id=base.id))
                else:
                    views.append(DeviceView(device, elsewhere=True))
                continue
            holder = by_iot.get(device.iot_id) if device.iot_id else None
            if holder is None:
                holder = by_name.get(device.device_name)
            if holder is None:
                views.append(DeviceView(device))
            elif holder.account_id == row.account_id:
                views.append(DeviceView(device, mower_id=holder.id))
            else:
                views.append(DeviceView(device, elsewhere=True))
        return views

    # --- changes -----------------------------------------------------------------------------

    async def create(
        self, actor: Actor, account_id: UUID, email: str, password: str
    ) -> tuple[LinkedAccountView, list[DeviceView]]:
        """Link a Mammotion account: log in through the gateway, store it with its devices.

        409 when the account already links this address; 422 ``linked-account-login-failed``
        when the cloud refuses the credentials; 429 after too many failed logins; 503 without a
        key or a gateway.
        """
        await self._require_manage(actor, account_id)
        self.vault.require()
        address = validate_email(email)
        async with self.store.transaction() as tx:
            await self._ensure_unlinked(tx, account_id, address)
        devices = await self._discover(account_id, address, password, None)
        now = self.clock()
        new_id = uuid4()
        row = LinkedAccount(
            id=new_id,
            account_id=account_id,
            provider=CloudProvider.MAMMOTION.value,
            email=address,
            password_encrypted=self.vault.seal(new_id, password),
            created_by=actor.subject_id,
            created_at=now,
            updated_at=now,
            devices=[d.model_dump(mode="json") for d in devices],
            last_synced_at=now,
        )
        async with self.uow() as work:
            await self._ensure_unlinked(work.tx, account_id, address)
            try:
                await work.tx.linked_accounts.add(row)
            except ConflictError:
                raise ConflictError("this Mammotion account is linked already") from None
            await work.audit(
                actor,
                "linked_account.created",
                account_id,
                "linked_account",
                row.id,
                {"provider": row.provider, "devices": len(devices)},
            )
            views = await self._device_views(work.tx, row)
        return LinkedAccountView(row, 0), views

    @staticmethod
    async def _ensure_unlinked(tx: Tx, account_id: UUID, email: str) -> None:
        for other in await tx.linked_accounts.find(account_id=account_id):
            if other.email.lower() == email.lower():
                raise ConflictError("this Mammotion account is linked already")

    async def sync(
        self,
        actor: Actor,
        account_id: UUID,
        linked_account_id: UUID,
        password: str | None = None,
    ) -> tuple[LinkedAccountView, list[DeviceView]]:
        """Re-read the device list ("Konto neu einlesen"), optionally with a new password.

        A failure is stored in ``last_sync_error`` as a canned message (the cached devices
        stay) and answered like on create. On success a password sealed with a previous key is
        sealed again with the current one (key rotation); a new password replaces the stored
        one and the account's mowers are re-published with a new ``credentials_rev``, so the
        gateway reconnects and fetches it.
        """
        await self._require_manage(actor, account_id)
        self.vault.require()
        async with self.store.transaction() as tx:
            row = await self._load(tx, account_id, linked_account_id)
        try:
            secret = password if password is not None else self.vault.open(row)
        except ValueError as exc:
            raise Unavailable("the stored password cannot be decrypted; enter it again") from exc
        try:
            devices = await self._discover(account_id, row.email, secret, row.id)
        except (LinkedAccountLoginError, Unavailable) as exc:
            refused = isinstance(exc, LinkedAccountLoginError)
            canned = LOGIN_REFUSED if refused else CLOUD_UNAVAILABLE
            async with self.store.transaction() as tx:
                await tx.linked_accounts.update_where(
                    {"last_sync_error": canned, "updated_at": self.clock()},
                    id=row.id,
                    account_id=account_id,
                )
            raise
        async with self.uow() as work:
            row = await self._load(work.tx, account_id, linked_account_id)
            now = self.clock()
            row.devices = [d.model_dump(mode="json") for d in devices]
            row.last_synced_at = now
            row.last_sync_error = None
            row.updated_at = now
            if password is not None or self.vault.cipher.needs_reseal(row.password_encrypted):
                row.password_encrypted = self.vault.seal(row.id, secret)
            await work.tx.linked_accounts.update(row)
            mowers = await work.tx.mowers.find(account_id=account_id, linked_account_id=row.id)
            if password is not None:
                cache: dict[UUID, LinkedAccount | None] = {row.id: row}
                for mower in mowers:
                    payload = await self.vault.registration(work.tx, mower, cache)
                    work.emit(MOWER_UPDATED, account_id, payload)
            await work.audit(
                actor,
                "linked_account.synced",
                account_id,
                "linked_account",
                row.id,
                {"devices": len(devices), "password_changed": password is not None},
            )
            views = await self._device_views(work.tx, row)
        return LinkedAccountView(row, len(mowers)), views

    async def delete(self, actor: Actor, account_id: UUID, linked_account_id: UUID) -> None:
        """Remove the link; 409 ``linked-account-in-use`` while mowers connect through it."""
        await self._require_manage(actor, account_id)
        async with self.uow() as work:
            row = await self._load(work.tx, account_id, linked_account_id)
            users = await work.tx.mowers.find(
                account_id=account_id, linked_account_id=row.id, order_by="name"
            )
            if users:
                names = ", ".join(m.name for m in users)
                raise LinkedAccountInUseError(
                    f"mowers still connect through this account: {names}; remove them first",
                    [(str(m.id), m.name) for m in users],
                )
            bases = await work.tx.base_stations.find(
                account_id=account_id, linked_account_id=row.id, order_by="name"
            )
            if bases:
                names = ", ".join(b.name for b in bases)
                msg = f"base stations come from this account: {names}; remove them first"
                raise ConflictError(msg)
            await work.tx.linked_accounts.delete(id=row.id)
            await work.audit(actor, "linked_account.removed", account_id, "linked_account", row.id)

    async def credentials(self, request: MowerCredentialsRequest) -> MowerCredentialsReply:
        """Answer ``fleet.mowers.credentials`` (system path for the Device-Gateway)."""
        async with self.store.transaction() as tx:
            reply = await self.vault.credentials(tx, request)
        _log.info(
            "mower credentials answered",
            mower_id=str(request.mower_id),
            found=reply.found,
            keys=sorted(reply.secret_options),
            link_kinds=sorted(reply.link_secrets),
        )
        return reply


def unique_names(items: Iterable[BulkItem]) -> None:
    """A device may appear once in one bulk registration (422 otherwise)."""
    seen: set[str] = set()
    for index, item in enumerate(items):
        if item.device_name in seen:
            msg = f"device {item.device_name} appears twice"
            problem = FieldProblem(f"mowers/{index}/device_name", msg, "duplicate")
            raise InvalidInput(msg, fields=[problem])
        seen.add(item.device_name)
