# mowdeck-authz

Authorization for MowDeck services. Services ask exactly one question, **may this subject do this on that object**, and OpenFGA answers it from the model in `infra/openfga/model.fga`. Licence: Apache-2.0. Depends on `mowdeck-contracts`, `openfga-sdk` (0.10.5 or newer, async client) and `prometheus-client`; the extra `fastapi` adds FastAPI and `mowdeck-service-kit` for the dependency helpers.

Decision record: [ADR 0014](../../docs/adr/0014-authorization-model-and-tuple-ownership.md). Model, tests and tuple ownership: [infra/openfga/README.md](../../infra/openfga/README.md).

| Module | Content |
| --- | --- |
| `refs` | `Subject`, `ObjectRef`, `user()`, `api_token()`, `account()`, `site()`, `group()`, `mower()`, `PLATFORM_OBJECT`, `principal_subject()` |
| `permissions` | `Relation` (computed relations), `PERMISSION_RELATIONS`, `grant_relation()` |
| `authorizer` | `Authorizer` protocol, `CheckItem`, `DecisionCache`, `readiness_check_authz()` |
| `openfga` | `OpenFgaAuthorizer`, `FgaConnection` |
| `writer` | `TupleWriter`, `TupleBackend`, `OpenFgaTupleBackend`, `TupleChange`, `tuple_owner()` |
| `bootstrap`, `model` | `bootstrap_model()`, `find_store_id()`, `load_model()`, `models_equal()`; `python -m mowdeck_authz bootstrap` |
| `evaluator`, `testing` | `ModelEvaluator`, `InMemoryAuthorizer`, `AllowAll`, `DenyAll`, `ManualClock` |
| `fastapi` | `require()`, `require_or_not_found()`, `authorize()` (relation chosen at runtime), `install_authz()` (optional token pre-filter), `path_object()`, `principal_account()`, `AuthzDependency`, `close_authorizer()`, `prefilter_denies()` |
| `events` | `subscribe_cache_invalidation()`, `apply_grants_changed()`: drop cached decisions on `identity.grants.changed` |

## Subjects and objects

OpenFGA strings, built from ids the owning service generated (never from raw user input):

| Kind | Form | Helper |
| --- | --- | --- |
| person or sub-user | `user:<uuid>` | `user(id)` |
| API token | `api_token:<uuid>` | `api_token(id)` |
| account, site, group, mower | `account:<uuid>` ... `mower:<uuid>` | `account(id)`, `site(id)`, `group(id)`, `mower(id)` |
| operator admins | `platform:main` | `PLATFORM_OBJECT` |
| userset | `group:<id>#member` | `Subject("group", id, "member")` |

`principal_subject(principal)` maps `USER` and `SUBUSER` to `user:<sub>` and `API_TOKEN` to `api_token:<sub>`. `SERVICE` principals raise `ValueError`: services never hold object rights in the model; a service acting on its own behalf gets dedicated internal routes.

Services check computed relations only (`Relation.CAN_CONTROL`, ...); `Relation` has no `grant_*` member, so inheritance and the tenant guard are never skipped.

## The question

```python
from mowdeck_authz import CheckItem, OpenFgaAuthorizer, Relation, mower, principal_subject

authz = OpenFgaAuthorizer.from_env(registry=app.state.metrics_registry)
subject = principal_subject(principal)

await authz.check(subject, Relation.CAN_CONTROL, mower(mower_id))  # bool
await authz.check_many([CheckItem(subject, Relation.CAN_VIEW, mower(m)) for m in ids])
await authz.list_objects(subject, Relation.CAN_VIEW, "mower")  # ["mower:<id>", ...]
```

Settings (`AuthzSettings.from_env()`):

| Variable | Meaning |
| --- | --- |
| `MOWDECK_OPENFGA_API_URL` | e.g. `http://openfga:8080` (required) |
| `MOWDECK_OPENFGA_API_TOKEN` or `MOWDECK_OPENFGA_API_TOKEN_FILE` | one of the OpenFGA pre-shared keys |
| `MOWDECK_OPENFGA_STORE_NAME` | default `mowdeck` |
| `MOWDECK_OPENFGA_STORE_ID`, `MOWDECK_OPENFGA_MODEL_ID` | optional pins for production |
| `MOWDECK_OPENFGA_CACHE_TTL_SECONDS` | default `5`, `0` disables the cache |
| `MOWDECK_OPENFGA_TIMEOUT_SECONDS` | default `2` |

### Store and model

`connect()` lists the stores, takes the oldest one named `mowdeck`, reads the latest authorization model and **pins its id** for every call of the process (a half-rolled deployment never mixes models). There is nothing to copy between the bootstrap job and the services, in Compose or in Kubernetes. `connect()` is also called lazily by the first check and by `ping()`, so a service starts even while OpenFGA comes up. After a model change, restart the services to pin the new model.

### Fail closed

Every error (no connection, timeout, HTTP error, a per-item error in a batch, unknown store or model) answers **`False`**, logs a warning on the `mowdeck_authz` logger and increments `mowdeck_authz_errors_total{operation}`. A check never raises, so there is no exception that a caller could mistake for "allowed". Errors are never cached. Readiness (`ping()`, the `authz` readiness check) reports the outage.

Metrics when a registry is passed: `mowdeck_authz_decisions_total{outcome="allowed|denied"}`, `mowdeck_authz_cache_hits_total`, `mowdeck_authz_errors_total{operation}`.

### Cache

Decisions (positive and negative) are cached in-process for `cache_ttl_seconds` (default 5 s), keyed by subject, relation, object and the caller's context, LRU bounded (10 000 entries). `invalidate()` drops everything, `invalidate_subject(subject)` drops one subject. `TupleWriter` calls them for its own process; other processes see a change after at most one TTL. The automatic `current_time` is not part of the key, so an expired support grant may live on for one TTL. `can_manage_users`, `can_manage_billing` and `can_manage_account` are never cached. `list_objects` is never cached.

### Batch

`check_many` serves cache hits locally and sends the rest in one `BatchCheck` (OpenFGA 1.8 or newer; the SDK splits into chunks of 50) with correlation ids, so results are mapped back in order. A single remaining item uses `Check`.

## FastAPI

```python
from typing import Annotated
from fastapi import Depends
from mowdeck_authz import OpenFgaAuthorizer, Relation
from mowdeck_authz.fastapi import (
    close_authorizer,
    install_authz,
    path_object,
    principal_account,
    require,
    require_or_not_found,
)

MOWER = path_object("mower", "mower_id")  # 404 unless the path parameter is a UUID
CanControl = Annotated[Principal, Depends(require_or_not_found(Relation.CAN_CONTROL, MOWER))]
CanManageUsers = Annotated[
    Principal, Depends(require(Relation.CAN_MANAGE_USERS, principal_account))
]


@router.post("/mowers/{mower_id}/commands")
async def send(mower_id: UUID, principal: CanControl, account_id: CurrentAccountId) -> ...:
    row = await load(mower_id, account_id)  # still filter by account: third isolation layer


app = create_app(settings, descriptor, routers=[router], on_shutdown=[close_authorizer])
install_authz(app, OpenFgaAuthorizer.from_env(registry=app.state.metrics_registry))
```

| Dependency | No principal | Service principal | Allowed | Visible but not allowed | Not visible |
| --- | --- | --- | --- | --- | --- |
| `require(relation, resolver)` | 401 | 403 | principal | 403 | 403 |
| `require_or_not_found(relation, resolver)` | 401 | 404 | principal | 403 | 404 |

`require_or_not_found` asks `relation` and `can_view` (configurable with `visible_relation`) in one batch, so a foreign object never reveals its existence. Use `require` where existence is not secret (account-wide endpoints). Resolvers are `Callable[[Request], ObjectRef | Awaitable[ObjectRef]]`.

When the relation depends on the request body (Fleet: `can_control` for a start, `can_drive` for manual drive, `can_emergency_stop` for an emergency stop), call `await authorize(request, relation, obj)` in the handler before any other I/O; it answers like `require_or_not_found` (or like `require` with `hide=False`).

`install_authz(app, authorizer)` sets `app.state.authorizer` (dependency `AuthzDependency`), registers the `authz` readiness check and installs a kit `PermissionChecker`, so existing `require_permission("mower.control")` call sites now ask OpenFGA for `can_control` on `account:<principal.account_id>`. Without `install_authz` the kit keeps its token level behaviour.

### Token pre-filter

`install_authz(app, authorizer, token_prefilter=True)` denies a relation without asking OpenFGA when the principal's `perms` claim (the account-wide upper bound in Identity's tokens) does not list its permission key (`can_control` needs `mower.control`, `can_emergency_stop` needs `mower.view`; `member` and `can_manage_account` are never filtered). It only ever denies; a listed key still needs the OpenFGA answer. Enable it only when principals come from Identity's tokens (`mowdeck-auth`); a newly granted key takes effect with the next access token. Identity enables it.

## Cache invalidation across services

Identity publishes `event.identity.grants.changed` (`GrantsChanged`: the subjects whose tuples changed, or `all_subjects`) after every committed change. Subscribe once at startup so a revoked right stops working at once in every service instead of after the cache TTL:

```python
from mowdeck_authz.events import subscribe_cache_invalidation

subscription = await subscribe_cache_invalidation(
    app.state.bus, authorizer
)  # ephemeral, per instance
```

Malformed events are dropped (no redelivery). A lost event costs at most one cache TTL (5 s).

## Writing tuples (Identity and Fleet only)

```python
from mowdeck_authz import OpenFgaTupleBackend, RelationshipTuple, TupleWriter, account, group, user

backend = OpenFgaTupleBackend(authz.connection)  # shares the client
identity = TupleWriter(backend, owner="identity", audit=write_audit_row, caches=[authz])

await identity.write([RelationshipTuple(user(lukas), "owner", account(a))])
await identity.replace_subject_grants(user(anna), group(north), ["mower.view", "mower.control"])
```

- **Ownership guard**: `owner="identity"` may write `account#owner/admin/member/support/platform`, every `grant_*` and `platform#operator_admin`; `owner="fleet"` may write `site#account`, `group#account/parent`, `mower#account/parent`. Anything else raises `TupleOwnershipError`.
- **Idempotent**: writes use the SDK's `ConflictOptions(on_duplicate_writes=IGNORE, on_missing_deletes=IGNORE)` (OpenFGA 1.10 or newer). Changes over 100 tuples are split into several transactions. A tuple in both lists is deleted first and written again (changing a condition).
- **`replace_subject_grants(subject, obj, permission_keys)`** maps keys to grant relations (validating level and subject type), reads the current grants and writes only the difference; membership and owner tuples are left alone.
- **Audit**: the `audit` hook (sync or async) receives a `TupleChange` (writes, deletes, owner, time) after every successful, non-empty change; its exceptions propagate.

## Bootstrap

```python
from mowdeck_authz import bootstrap_model, create_client

result = await bootstrap_model(
    create_client(settings)
)  # store_id, model_id, store_created, model_written
```

Creates the store if missing and writes the packaged model (`load_model()`, the JSON form of `model.fga`) only if it differs from the latest one; a second run writes nothing. Same rules as the `authz-bootstrap` Compose job; CLI: `python -m mowdeck_authz bootstrap`.

## Testing with the fake

```python
from mowdeck_authz import RelationshipTuple, account, mower, user
from mowdeck_authz.testing import InMemoryAuthorizer

authz = InMemoryAuthorizer(
    [
        RelationshipTuple(user(lukas), "owner", account(a)),
        RelationshipTuple(account(a).as_subject(), "account", mower(m1)),
    ]
)
assert await authz.check(user(lukas), "can_control", mower(m1))
```

`InMemoryAuthorizer` evaluates the packaged model (unions, intersections, parents, conditions with an injectable `now`), validates tuples against the model's type restrictions like OpenFGA does, records every check in `.checks`, and is also a `TupleBackend`, so a `TupleWriter` can write into it. A contract test runs every assertion of `infra/openfga/model.fga.yaml` through it. Unknown relations raise `ValueError` so typos fail tests. `AllowAll()` and `DenyAll()` are for tests that are not about authorization.

Integration tests against a real OpenFGA (`test_authz_integration.py`) run when `MOWDECK_TEST_OPENFGA_URL` (and `MOWDECK_TEST_OPENFGA_TOKEN`) is set; see `docs/dev/libraries.md`.
