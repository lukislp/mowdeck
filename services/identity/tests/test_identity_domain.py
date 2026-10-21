"""Domain rules: catalogue, role validation, token claims, credentials, rate limiter."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import ModuleDescriptor, PermissionDef

from identity.application.catalogue import merge_descriptors, model_entries
from identity.application.ratelimit import TokenBucketLimiter
from identity.domain.credentials import (
    new_api_token,
    normalize_email,
    normalize_username,
    parse_api_token,
    same_hash,
    sha256_hex,
    slugify,
    validate_password,
    validate_username,
)
from identity.domain.errors import InvalidInput
from identity.domain.models import (
    ApiToken,
    Level,
    LevelType,
    Membership,
    Role,
    RoleKind,
    Status,
    SubjectType,
    TokenGrant,
)
from identity.domain.permissions import (
    Catalogue,
    HeldRole,
    held_on_account,
    token_permissions,
    validate_role_permissions,
    validate_token_grant,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
GATEWAY = ModuleDescriptor(
    name="device-gateway",
    version="0.1.0",
    permissions=[
        PermissionDef(key="device_gateway.view", description="View", implied_by=["mower.view"])
    ],
)


def catalogue() -> Catalogue:
    return Catalogue(merge_descriptors([], [GATEWAY], NOW))


def role(*keys: str, kind: RoleKind = RoleKind.CUSTOM, key: str = "custom") -> Role:
    return Role(uuid4(), uuid4(), key, key.title(), kind, list(keys), NOW, NOW)


def membership(**flags: bool) -> Membership:
    return Membership(uuid4(), uuid4(), uuid4(), Status.ACTIVE, NOW, **flags)


def test_catalogue_levels_and_subject_rules() -> None:
    cat = catalogue()
    assert cat.grantable("mower.control", LevelType.MOWER, SubjectType.API_TOKEN)
    assert not cat.grantable("fleet.bulk_command", LevelType.MOWER, SubjectType.USER)
    assert not cat.grantable("users.manage", LevelType.ACCOUNT, SubjectType.API_TOKEN)
    assert not cat.grantable("device_gateway.view", LevelType.GROUP, SubjectType.USER)
    assert cat.grantable("device_gateway.view", LevelType.ACCOUNT, SubjectType.API_TOKEN)
    assert not cat.grantable("mower.fly", LevelType.ACCOUNT, SubjectType.USER)
    assert "mower.view" in cat and len(cat) == len(cat.keys)
    assert "billing.manage" not in cat.admin_keys()
    with pytest.raises(InvalidInput, match="unknown permission"):
        cat.entry("mower.fly")


def test_role_validation_rules() -> None:
    cat = catalogue()
    assert validate_role_permissions(["mower.view", "mower.control", "mower.view"], cat) == [
        "mower.control",
        "mower.view",
    ]
    with pytest.raises(InvalidInput, match=r"require mower.view"):
        validate_role_permissions(["mower.drive"], cat)
    with pytest.raises(InvalidInput, match=r"require mower.view"):
        validate_role_permissions(["schedule.edit"], cat)
    assert validate_role_permissions(["fleet.manage"], cat) == ["fleet.manage"]
    with pytest.raises(InvalidInput):
        validate_role_permissions(["nope.nope"], cat)


def test_token_grant_validation() -> None:
    cat = catalogue()
    assert validate_token_grant(["mower.view"], LevelType.SITE, cat) == ["mower.view"]
    with pytest.raises(InvalidInput, match="at least one"):
        validate_token_grant([], LevelType.ACCOUNT, cat)
    with pytest.raises(InvalidInput, match="API token"):
        validate_token_grant(["audit.view"], LevelType.ACCOUNT, cat)
    with pytest.raises(InvalidInput, match="cannot be granted on mower"):
        validate_token_grant(["fleet.bulk_command"], LevelType.MOWER, cat)


def test_token_permissions_owner_admin_and_levels() -> None:
    cat = catalogue()
    roles, perms = token_permissions(membership(is_owner=True), [], cat)
    assert roles == ["owner"] and "billing.manage" in perms and "device_gateway.view" in perms
    roles, perms = token_permissions(membership(is_admin=True), [], cat)
    assert roles == ["admin"] and "billing.manage" not in perms and "users.manage" in perms
    operator = role(
        "mower.view", "mower.control", "mower.drive", kind=RoleKind.DEFAULT, key="operator"
    )
    roles, perms = token_permissions(None, [HeldRole(operator, LevelType.GROUP)], cat)
    assert roles == ["operator"]
    assert perms == ["mower.control", "mower.drive", "mower.view"]
    _, perms = token_permissions(None, [HeldRole(operator, LevelType.ACCOUNT)], cat)
    assert "device_gateway.view" in perms
    admin_role = role(kind=RoleKind.SYSTEM_ADMIN, key="admin")
    roles, _ = token_permissions(membership(), [HeldRole(admin_role, LevelType.ACCOUNT)], cat)
    assert roles == []  # system roles appear through the membership flags only


def test_held_on_account() -> None:
    cat = catalogue()
    viewer = role("mower.view", key="viewer")
    assert held_on_account(None, [HeldRole(viewer, LevelType.ACCOUNT)], cat) == {
        "mower.view",
        "device_gateway.view",
    }
    assert held_on_account(None, [HeldRole(viewer, LevelType.SITE)], cat) == set()
    assert "billing.manage" in held_on_account(membership(is_owner=True), [], cat)
    assert "billing.manage" not in held_on_account(membership(is_admin=True), [], cat)


def test_catalogue_merge_retires_dropped_keys_and_keeps_unreachable_modules() -> None:
    first = merge_descriptors([], [GATEWAY], NOW)
    assert {e.key for e in first} >= {"device_gateway.view", "mower.view"}
    later = NOW + timedelta(minutes=10)
    shrunk = ModuleDescriptor(name="device-gateway", version="0.2.0", permissions=[])
    second = merge_descriptors(first, [shrunk], later)
    entry = next(e for e in second if e.key == "device_gateway.view")
    assert entry.retired
    unreachable = merge_descriptors(first, [], later)
    assert not next(e for e in unreachable if e.key == "device_gateway.view").retired
    model_key = next(e for e in second if e.key == "mower.view")
    assert model_key.in_model and not model_key.retired
    fleet = ModuleDescriptor(
        name="fleet",
        version="1.0.0",
        permissions=[PermissionDef(key="mower.view", description="View mowers")],
    )
    third = merge_descriptors(second, [fleet], later)
    view = next(e for e in third if e.key == "mower.view")
    assert (view.module, view.description, view.in_model) == ("fleet", "View mowers", True)
    assert set(model_entries(NOW)) == {e.key for e in third if e.in_model}


def test_usernames_emails_and_passwords() -> None:
    assert normalize_username("  Lukas ") == "lukas"
    assert validate_username("Anna.M_1") == "anna.m_1"
    for bad in ("ab", "a b", "-x" * 40, "-lead", "x" * 65):
        with pytest.raises(InvalidInput):
            validate_username(bad)
    assert normalize_email(None) is None and normalize_email(" ") is None
    assert normalize_email(" Eve@Example.ORG ") == "eve@example.org"
    for bad in ("no-at", "@x", "x@"):
        with pytest.raises(InvalidInput):
            normalize_email(bad)
    validate_password("correct horse battery")
    for bad in ("short", "x" * 257, "1234567890", "PASSWORD123"):
        with pytest.raises(InvalidInput):
            validate_password(bad)
    with pytest.raises(InvalidInput):
        validate_password("lukas.long", username="Lukas.Long")


def test_opaque_tokens_and_hashes() -> None:
    token_id = uuid4()
    token, secret = new_api_token(token_id)
    assert parse_api_token(token) == (token_id, secret)
    for bad in ("x", "mdk_short_x", f"mdk_{'z' * 32}_{'s' * 43}", f"mdk_{token_id.hex}"):
        assert parse_api_token(bad) is None
    # Found by fuzzing: UUID() accepted a sign, whitespace and upper case in the id part.
    for alias in (f"+{token_id.hex[1:]}", f"{token_id.hex[:-1]} ", token_id.hex.upper()):
        if alias != token_id.hex:
            assert parse_api_token(f"mdk_{alias}_{secret}") is None
    assert same_hash(sha256_hex(secret), secret)
    assert not same_hash(sha256_hex(secret), secret + "x")
    assert slugify("Gärten & Höfe", "abc").startswith("garten-hofe-")
    assert slugify("!!!", "abc") == "account-abc"


def test_token_grants_and_usability() -> None:
    level = Level(LevelType.GROUP, uuid4())
    grant = TokenGrant(level, ("mower.view",))
    assert TokenGrant.from_json(grant.to_json()) == grant
    assert str(level) == f"group:{level.id}"
    token = ApiToken(uuid4(), uuid4(), "ha", "mdk_", "h", uuid4(), [grant.to_json()], NOW)
    assert token.permissions == ["mower.view"]
    assert token.is_usable(NOW)
    token.expires_at = NOW
    assert not token.is_usable(NOW)
    token.expires_at, token.revoked_at = None, NOW
    assert not token.is_usable(NOW)


def test_token_bucket() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(per_minute=60, capacity=2, max_keys=2, clock=lambda: now[0])
    assert limiter.take("a") == 0 and limiter.take("a") == 0
    assert limiter.take("a") == pytest.approx(1.0)
    now[0] += 1.0
    assert limiter.take("a") == 0
    limiter.take("b")
    limiter.take("c")  # evicts "a"
    assert limiter.take("a") == 0
    with pytest.raises(ValueError, match="positive"):
        TokenBucketLimiter(per_minute=0, capacity=1)
