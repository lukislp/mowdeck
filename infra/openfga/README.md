# OpenFGA authorization model

The authorization model of MowDeck as code. Decision record: [ADR 0014](../../docs/adr/0014-authorization-model-and-tuple-ownership.md); full design: [docs/dev/design-identity-authz.md](../../docs/dev/design-identity-authz.md).

| File | Content |
| --- | --- |
| `model.fga` | The model (DSL, schema 1.1). **Source of truth.** |
| `model.fga.yaml` | Model tests: tuples of a small world (two accounts, a sub-user, an API token, an operator admin, a support grant) and 58 check and 2 list assertions |
| `bootstrap/` | The `authz-bootstrap` job of the Compose stack (`Dockerfile`, `bootstrap.sh`) |
| `sync_model_json.py` | Regenerates or checks the JSON form packaged in `mowdeck-authz` (`libs/mowdeck-authz/src/mowdeck_authz/model.json`) |

## What the model expresses

- **Hierarchy** `account > site > group > mower`. `site#account`, `group#account`, `group#parent` (a site, optional for groups across sites), `mower#account`, `mower#parent` (the site and any groups).
- **Subjects**: `user` (persons and sub-users alike), `api_token` (its own subject with explicit grants), `platform:main` (holder of the operator admins).
- **Fixed account relations**: `owner`, `admin`, `member` (also implied by owner and admin), `support` (conditional on `not_expired`), `platform`.
- **Grants**: each permission key maps to a directly assignable `grant_<key>` relation on the levels where it makes sense, written by Identity from the subject's roles. `users.manage`, `billing.manage` and `audit.view` exist only on the account and only for `user`.
- **Computed relations** services check: `can_view`, `can_control`, `can_drive`, `can_edit_schedule`, `can_bulk_command`, `can_manage_mowers`, `can_manage_users`, `can_manage_billing`, `can_view_audit`, `can_manage_account`, `can_emergency_stop` (equals `can_view`), `can_operator_manage`.
- **Tenant guard**: below the account every `can_*` is `(grant or inherited) and tenant from account`, so a tuple written into the wrong account grants nothing.
- **Support access** is view only and expires: the tuple stores `expires_at`, every check sends `current_time`.

| Permission key | Grant relation | Computed relation | Levels |
| --- | --- | --- | --- |
| `mower.view` | `grant_mower_view` | `can_view` | account, site, group, mower |
| `mower.control` | `grant_mower_control` | `can_control` | account, site, group, mower |
| `mower.drive` | `grant_mower_drive` | `can_drive` | account, site, group, mower |
| `schedule.edit` | `grant_schedule_edit` | `can_edit_schedule` | account, site, group, mower |
| `fleet.bulk_command` | `grant_fleet_bulk_command` | `can_bulk_command` | account, site, group |
| `fleet.manage` | `grant_fleet_manage` | `can_manage_mowers` | account, site, group, mower |
| `users.manage` | `grant_users_manage` | `can_manage_users` | account |
| `billing.manage` | `grant_billing_manage` | `can_manage_billing` | account |
| `audit.view` | `grant_audit_view` | `can_view_audit` | account |

The table is mirrored as data in `mowdeck_authz.PERMISSION_RELATIONS`; a test checks it against the model.

## Tuple ownership

| Tuples | Written by |
| --- | --- |
| `account#owner`, `account#admin`, `account#member`, `account#support`, `account#platform`; every `grant_*` on any level; `platform#operator_admin` | Identity |
| `site#account`; `group#account`, `group#parent`; `mower#account`, `mower#parent` | Fleet |

`mowdeck_authz.TupleWriter(owner="identity" | "fleet")` refuses anything else. No other service writes tuples.

## Run the model tests

With Docker, from the repository root:

```bash
docker run --rm -v "$PWD/infra/openfga:/w" -w /w openfga/cli:v0.8.1 model test --tests model.fga.yaml
# or
uv run poe fga-test
```

Expected: `Tests 8/8 passing, Checks 58/58 passing, ListObjects 2/2 passing`. CI runs the same in the `openfga-model` job, checks that the packaged `model.json` is current and runs the bootstrap job twice against a throwaway OpenFGA. The same YAML is also run through the in-memory fake (`test_authz_fake_matches_model.py`) and, in CI, through `OpenFgaAuthorizer` against a real server.

## Changing the model

1. Edit `model.fga` and add or adjust assertions in `model.fga.yaml`.
2. `uv run poe fga-test`.
3. `uv run poe fga-json` regenerates the packaged JSON; commit it with the change.
4. Update `PERMISSION_RELATIONS` (and `Relation`) in `mowdeck-authz` for new keys; `uv run poe test`.
5. Deploy: the bootstrap job writes the new model; restart the services so they pin it.

Rules: add types and relations only; never rename or remove a relation that has tuples without a tuple migration; a new permission key means a new `grant_*` plus `can_*` pair on its levels.

## Bootstrap job

`bootstrap/bootstrap.sh` runs in a small image (the static `fga` binary from `openfga/cli:v0.8.1`, `jq` from `ghcr.io/jqlang/jq:1.8.1`, on `public.ecr.aws/docker/library/busybox:1.37.0`; the official CLI image has no shell). It finds the store by name (creating it if missing) and writes the model only if the latest model, rendered by the same CLI, differs. Environment: `FGA_API_URL`, `FGA_API_TOKEN`, `FGA_STORE_NAME` (default `mowdeck`), `FGA_MODEL_FILE` (default `/model/model.fga`). The same logic is available in Python as `mowdeck_authz.bootstrap_model()` and `python -m mowdeck_authz bootstrap`.
