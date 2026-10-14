"""Pattern templates: saved chains of an account ("2 border laps + diagonal + re-mow").

Templates hold no mower data, only pattern keys and settings, so every principal of the
account may read them; saving and deleting needs ``can_control`` on the account (owners,
admins, and roles with ``mower.control`` on the whole account) and the plan feature
``spot_mowing`` (checked by the route).
"""

from typing import Any
from uuid import UUID, uuid4

from mowdeck_authz import account
from mowdeck_contracts.v1 import Principal
from mowdeck_patterns import InvalidSettings, PatternRegistry, UnknownPattern

from planner.application.access import CAN_CONTROL, Access
from planner.application.context import PlannerConfig
from planner.application.ports import Clock, Store
from planner.domain.errors import ConflictError, ForbiddenError, InvalidInput, NotFoundError
from planner.domain.models import Template

MAX_CHAIN = 8


class Templates:
    """Use cases of templates."""

    def __init__(
        self,
        store: Store,
        access: Access,
        registry: PatternRegistry,
        config: PlannerConfig,
        clock: Clock,
    ) -> None:
        self.store = store
        self.access = access
        self.registry = registry
        self.config = config
        self.clock = clock

    def check_chain(self, chain: list[dict[str, Any]]) -> None:
        """Every step names a registered pattern with valid settings."""
        if not 1 <= len(chain) <= MAX_CHAIN:
            raise InvalidInput(f"a template holds 1 to {MAX_CHAIN} patterns")
        errors: list[str] = []
        for index, step in enumerate(chain):
            try:
                pattern = self.registry.get(str(step["pattern"]))
            except UnknownPattern as exc:
                errors.append(f"chain.{index}: {exc}")
                continue
            parse = getattr(pattern, "parse", None)
            if parse is None:
                continue
            try:
                parse(step.get("settings") or {})
            except InvalidSettings as exc:
                errors.extend(f"chain.{index}.settings.{e}" for e in exc.errors)
        if errors:
            raise InvalidInput("invalid template", errors)

    async def all(self, account_id: UUID) -> list[Template]:
        """Templates of the account, by name."""
        async with self.store.transaction() as tx:
            return await tx.list_templates(account_id)

    async def get(self, account_id: UUID, name: str) -> Template:
        """One template (404 if missing)."""
        async with self.store.transaction() as tx:
            found = await tx.get_template(account_id, name)
        if found is None:
            raise NotFoundError("template not found")
        return found

    async def _require_edit(self, principal: Principal, account_id: UUID) -> None:
        if not await self.access.allowed(principal, CAN_CONTROL, account(account_id)):
            raise ForbiddenError("saving templates needs can_control on the account")

    async def put(
        self,
        principal: Principal,
        account_id: UUID,
        name: str,
        chain: list[dict[str, Any]],
        description: str | None,
    ) -> tuple[Template, bool]:
        """Create or replace the template ``name``; True when it was created."""
        await self._require_edit(principal, account_id)
        self.check_chain(chain)
        now = self.clock()
        async with self.store.transaction() as tx:
            existing = await tx.get_template(account_id, name)
            if existing is None:
                if await tx.count_templates(account_id) >= self.config.max_templates:
                    msg = f"an account may keep at most {self.config.max_templates} templates"
                    raise ConflictError(msg)
                template = Template(
                    id=uuid4(),
                    account_id=account_id,
                    name=name,
                    chain=chain,
                    created_by=principal.subject,
                    created_at=now,
                    updated_at=now,
                    description=description,
                )
            else:
                template = Template(
                    id=existing.id,
                    account_id=account_id,
                    name=name,
                    chain=chain,
                    created_by=existing.created_by,
                    created_at=existing.created_at,
                    updated_at=now,
                    description=description,
                )
            await tx.put_template(template)
        return template, existing is None

    async def delete(self, principal: Principal, account_id: UUID, name: str) -> None:
        """Delete the template ``name`` (404 if missing)."""
        await self._require_edit(principal, account_id)
        async with self.store.transaction() as tx:
            if not await tx.delete_template(account_id, name):
                raise NotFoundError("template not found")
