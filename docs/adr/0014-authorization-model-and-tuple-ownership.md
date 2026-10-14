# ADR 0014: Authorization model, tuple ownership and the mowdeck-authz client

## Status

Accepted (2026-10-09)

## Context

[ADR 0001](0001-openfga-for-authorization.md) chose OpenFGA. The plan asks services one question through a thin library: may who do what on which object. Accounts have the hierarchy account, site, group, mower; roles are named bundles of permission keys, editable per account; sub-users get a role on one level (Anna is Operator on group North only); API tokens (Home Assistant) get chosen permissions; operator admins manage accounts but see no mower data; support access needs approval and expires. Every permission change is audited.

Open points for the first implementation:

- How do editable roles reach OpenFGA: as a `role` type in the model, or expanded by Identity?
- Which service writes which tuples, and how is a wrong tuple kept from leaking data into another account?
- How do services find the store and the model in Compose and Kubernetes without copying generated ids?
- What happens to a request when OpenFGA cannot answer?

The full design, including the model and its tests, is in [Design: identity and authorization](../dev/design-identity-authz.md).

## Decision

**Expanded grant tuples instead of a role type.** Roles and role assignments live in Identity's database. For each subject and level Identity writes the union of the permissions of all its assignments as direct `grant_<key>` tuples (`user:anna#grant_mower_control@group:north`). The model has no `role` type. Services check only computed `can_*` relations, which add inheritance from parents and the tenant guard; `mowdeck_authz.Relation` lists them and has no `grant_*` member.

**Owner and admin are fixed relations** (`account#owner`, `account#admin`), not editable roles, so editing a role can never weaken them. Manager, Operator, Viewer and custom roles are editable bundles.

**Tenant intersection.** Below the account every `can_*` is `(grant or inherited) and tenant from account`, with `tenant = member or support`. A grant written for a user of account B on an object of account A, or a mower of account B wired under a group of account A, grants nothing. The model tests in `infra/openfga/model.fga.yaml` cover both cases.

**API tokens are their own subject** (`api_token:<id>`): a member of exactly one account with explicit grants only, never inheriting the rights of their creator. `users.manage`, `billing.manage` and `audit.view` are restricted to `[user]` by type, so a token cannot hold them even if Identity had a bug.

**Operator admins and support.** Operator admins are `platform:main#operator_admin` and get `can_operator_manage` on accounts and nothing below. Support access is a conditional tuple (`not_expired`) that grants view only; services always send `current_time`, and the tuple's `expires_at` wins over request context. Emergency stop is `can_emergency_stop = can_view`: anyone who can see a mower can stop it.

**Tuple ownership.** Identity writes `account#owner/admin/member/support/platform`, every `grant_*` on any level and `platform#operator_admin`. Fleet writes `site#account`, `group#account/parent` and `mower#account/parent`. `TupleWriter(owner=...)` enforces this table as a guard against mistakes (pre-shared keys are all equivalent, so it is not a security boundary) and passes every change to an audit hook.

**Three isolation layers.** The account id only comes from the verified token; the object is checked in OpenFGA (with the tenant intersection); the database query filters by the account id. A foreign object answers 404.

**Fail closed.** `OpenFgaAuthorizer` answers `False` on every error (connection, timeout, HTTP error, per-item batch error), logs a warning and counts `mowdeck_authz_errors_total`. Errors are never cached. A small in-process cache (default 5 s, keyed by subject, relation, object and context) absorbs repeated checks; `TupleWriter` clears it for the touched subjects in its own process, so the worst case for another process is one TTL. `can_manage_users`, `can_manage_billing` and `can_manage_account` are never cached.

**Store resolved by name, model pinned at startup.** The one-shot Compose job `authz-bootstrap` creates the store `mowdeck` if missing and writes `infra/openfga/model.fga` only when it differs from the latest model. Services find the store by name (the oldest of that name), read the latest model once and pin its id for the process lifetime, so no generated id is copied into any configuration and a half-rolled deployment never mixes models. `MOWDECK_OPENFGA_STORE_ID` and `MOWDECK_OPENFGA_MODEL_ID` pin both when an operator wants to. The same logic is available in Python as `bootstrap_model()` and `python -m mowdeck_authz bootstrap` (for a Helm hook later).

**The model is code with tests.** `model.fga` is the source of truth; CI runs `fga model test` with the pinned CLI and checks that the JSON form packaged in `mowdeck-authz` is current. The in-memory fake `InMemoryAuthorizer` evaluates that packaged JSON and is tested against every assertion of the model test file, so tests of services cannot drift from the real model.

## Consequences

- The model stays small and reads like the business story; checks are one hop shorter than with a role type; new permission keys need a `grant_*` and `can_*` pair on their levels and a model change.
- Editing a role rewrites the grant tuples of every assignee (fan-out). Identity reconciles per subject (`replace_subject_grants` computes the diff), which is idempotent and order independent.
- The tenant guard costs one extra lookup per check; acceptable at our scale and the main defence against a wrong tuple.
- An OpenFGA outage looks like "denied" to users (404 or 403) and fails readiness (`authz` check); it never grants access.
- Revocations reach other processes within one cache TTL (5 s). A cache invalidation event over NATS is planned with Identity (Task 6).
- Phase 1 keeps one model file. Modules adding their own object types later switch to modular models.

## Alternatives considered

- **`role` and `role_assignment` types in the model.** A role edit would be one tuple, but the model doubles in size, checks get slower, every permission still needs relations on both types, and Identity must keep roles in its database anyway for the UI and anti-escalation. Rejected.
- **No tenant intersection, trust the writers.** Simpler model, but a single bug in Identity or Fleet would leak objects across accounts. Rejected.
- **API tokens inherit the creator's rights.** Fewer tuples, but a token would silently gain every right its creator gets later and could not be narrowed. Rejected.
- **Fail open or raise on errors.** Failing open is unacceptable; raising lets every service decide again and risks a `try` that treats an error as allowed. A plain `False` is the simplest safe contract.
- **Resolve "latest model" per request.** Always current, but two replicas could answer with different models during a rollout. Rejected in favour of pinning at startup.
- **Persist the store and model id from the bootstrap job into the services' configuration.** Needs a shared volume or a manual copy step in Compose and an extra secret or config map in Kubernetes. Resolving by name needs neither.
- **Bootstrap inside Identity's startup.** Several replicas would race, and Fleet would depend on Identity's start order. Rejected in favour of a one-shot job.
