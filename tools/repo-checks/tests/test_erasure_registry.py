# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Every service that owns tables answers the account erasure (ADR 0082).

Deleting an account must reach every service that stores something of it, so a service with
tables that Identity does not ask would silently keep personal data. The test fails for:

* a **new** service with a database that is neither answering ``account.erase.<service>`` nor
  listed in :data:`PENDING`;
* a service in :data:`PENDING` that answers already (delete its entry: the list only shrinks);
* a service in :data:`PENDING` that has no tables (a stale entry);
* a contract list (``ERASURE_SERVICES``) that differs from the services with tables.

:data:`PENDING` was the work of the follow-up steps of ADR 0082 and is empty since step 2c:
Fleet, Scheduler, Telemetry, Planner and Entitlements answer since step 2a, Notifications and
Vision since step 2b, Billing since step 2c. With it empty the purge is on by default, here and
in the deployment files.

**Optional services.** A service that runs only in some installations (Billing: the Compose
profile, the chart's toggle) cannot be on the list every installation asks: an unanswered
request holds every purge back. It cannot be left to the operator either: Billing enabled and
not asked means a deleted account keeps its customer and its subscription at the payment
provider. So the deployment that enables the service is what tells Identity, and the checks at
the end keep the three places (Identity's default list, Compose, the chart) from diverging.
"""

import importlib.util
import inspect
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from mowdeck_contracts.v1 import ERASURE_SERVICES
from mowdeck_contracts.v1.subjects import ACCOUNT_ERASE_PREFIX

ORCHESTRATOR = "identity"
"""Identity owns the accounts and asks the others; it erases its own rows itself."""

PENDING: dict[str, str] = {}
"""Services that own tables and do not answer ``account.erase.<service>`` yet, with the step of
ADR 0082 that adds the handler. Empty since step 2c, and it stays empty: a new service with
tables answers from its first day (an entry here would switch the purge off for everybody)."""

PROFILES_VARIABLE = "${COMPOSE_PROFILES:-}"
"""What Compose passes to Identity and to every optional service that keeps account data: the
profiles the operator enabled, from ``.env`` or the environment."""


def _nats_auth(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "nats_auth.py"
    spec = importlib.util.spec_from_file_location("nats_auth_for_erasure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _services_with_tables(repo_root: Path) -> set[str]:
    """Services whose package has a ``adapters/db/tables.py`` (a database of their own)."""
    return {
        tables.parents[4].name
        for tables in (repo_root / "services").glob("*/src/*/adapters/db/tables.py")
    }


def _answering(repo_root: Path) -> set[str]:
    """Services that serve their ``account.erase.<service>`` subject (the NATS permission map
    is checked against the code by ``test_nats_auth``)."""
    users = _nats_auth(repo_root).USERS
    return {
        name for name, access in users.items() if f"{ACCOUNT_ERASE_PREFIX}.{name}" in access.serve
    }


def _unregistered(with_tables: set[str], answering: set[str], pending: set[str]) -> set[str]:
    """Services with tables that neither answer nor are pending (Identity asks, it is exempt)."""
    return with_tables - {ORCHESTRATOR} - answering - pending


@pytest.fixture(scope="module")
def with_tables(repo_root: Path) -> set[str]:
    return _services_with_tables(repo_root)


def test_the_scan_finds_the_services_with_a_database(with_tables: set[str]) -> None:
    assert {"identity", "fleet", "telemetry"} <= with_tables
    assert "assistant" not in with_tables  # it owns no data


def test_every_service_with_tables_answers_or_is_pending(
    repo_root: Path, with_tables: set[str]
) -> None:
    answering = _answering(repo_root)
    unregistered = _unregistered(with_tables, answering, set(PENDING))
    assert not unregistered, (
        f"{sorted(unregistered)} own tables but are neither answering account.erase.<service> "
        "nor listed in PENDING: add the handler (ADR 0082) or, for the follow-up steps only, "
        "the PENDING entry with its step"
    )


def test_pending_only_shrinks(repo_root: Path, with_tables: set[str]) -> None:
    answering = _answering(repo_root)
    done = sorted(set(PENDING) & answering)
    assert not done, f"{done} answer account.erase.<service> now: remove them from PENDING"
    stale = sorted(set(PENDING) - with_tables)
    assert not stale, f"{stale} own no tables: remove them from PENDING"


def test_the_contract_lists_exactly_the_services_with_tables(with_tables: set[str]) -> None:
    assert set(ERASURE_SERVICES) == with_tables - {ORCHESTRATOR}


def test_identity_may_ask_every_service(repo_root: Path) -> None:
    users = _nats_auth(repo_root).USERS
    assert f"{ACCOUNT_ERASE_PREFIX}.*" in users[ORCHESTRATOR].publish


def test_no_service_answers_an_erasure_it_is_not_registered_for(repo_root: Path) -> None:
    answering = _answering(repo_root)
    assert answering <= set(ERASURE_SERVICES)


def test_a_new_service_with_tables_fails_until_it_is_registered(with_tables: set[str]) -> None:
    """The guard is strict: a service nobody listed is reported, answering or pending is not."""
    assert _unregistered(with_tables | {"newsvc"}, set(), set(PENDING)) == {"newsvc"} | (
        with_tables - {ORCHESTRATOR} - set(PENDING)
    )
    assert _unregistered(with_tables | {"newsvc"}, {"newsvc"}, set(PENDING)) == (
        with_tables - {ORCHESTRATOR} - set(PENDING)
    )
    assert _unregistered(with_tables | {"newsvc"}, set(), set(PENDING) | {"newsvc"}) == (
        with_tables - {ORCHESTRATOR} - set(PENDING)
    )


def _erasure_default() -> bool:
    from identity.settings import Settings  # the workspace installs every service

    default = Settings.model_fields["identity_erasure_enabled"].default
    assert isinstance(default, bool)
    return default


def test_the_purge_is_off_exactly_while_services_are_pending() -> None:
    """The switch cannot be forgotten: ``MOWDECK_IDENTITY_ERASURE_ENABLED`` stays false by
    default until every service answers (step 2c empties PENDING), and then it must be true.

    A purge that runs while a service does not answer would lock an account for good without
    deleting it; a default that stays false afterwards would leave accounts undeleted."""
    default = _erasure_default()
    if PENDING:
        assert default is False, (
            f"{sorted(PENDING)} do not answer account.erase.<service> yet: the purge must stay "
            "off by default (identity_erasure_enabled)"
        )
    else:
        assert default is True, (
            "every service answers account.erase.<service>: switch the purge on by default "
            "(identity_erasure_enabled=True in identity.settings, .env.example and the values)"
        )


def test_the_deployment_files_carry_the_same_default(repo_root: Path) -> None:
    expected = "true" if _erasure_default() else "false"
    env = (repo_root / "infra/compose/.env.example").read_text("utf-8")
    assert f"IDENTITY_ERASURE_ENABLED={expected}\n" in env
    compose = (repo_root / "infra/compose/docker-compose.yml").read_text("utf-8")
    assert f"${{IDENTITY_ERASURE_ENABLED:-{expected}}}" in compose
    values = (repo_root / "infra/helm/mowdeck/values.yaml").read_text("utf-8")
    assert f"IDENTITY_ERASURE_ENABLED: {expected}\n" in values


# --- optional services: asked exactly where the installation runs them -------------------------


def _compose(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "infra" / "compose" / "docker-compose.yml"
    services: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))["services"]
    return services


def _optional(repo_root: Path) -> set[str]:
    """The erasure services Compose starts only with a profile."""
    compose = _compose(repo_root)
    return {name for name in ERASURE_SERVICES if compose[name].get("profiles")}


def _identity_settings(**values: str) -> Any:
    from identity.settings import Settings

    return Settings.model_validate({"environment": "test", **values})


def _variable(service: str) -> str:
    return f"MOWDECK_{service.upper().replace('-', '_')}_OPTIONAL_SERVICES"


def test_billing_is_the_optional_erasure_service(repo_root: Path) -> None:
    """The scan finds something: without it the checks below would pass on nothing."""
    assert _optional(repo_root) == {"billing"}


def test_the_default_list_is_exactly_the_services_every_installation_runs(
    repo_root: Path,
) -> None:
    """An installation without Billing must finish an erasure without asking it; one that
    lacked a core service would never be asked for what it stores."""
    asked = set(_identity_settings().erasure_config().services)
    assert asked == set(ERASURE_SERVICES) - _optional(repo_root)


def test_a_profile_carries_the_name_of_its_service(repo_root: Path) -> None:
    """Identity reads ``COMPOSE_PROFILES`` as service names, so the profile of an optional
    erasure service is called like the service."""
    compose = _compose(repo_root)
    for name in _optional(repo_root):
        assert compose[name]["profiles"] == [name]


def test_compose_tells_identity_and_the_service_the_same_thing(repo_root: Path) -> None:
    """Identity and the optional service read one variable. So Identity asks the service
    exactly when the service is willing to start, whatever the operator did."""
    compose = _compose(repo_root)
    identity = compose["identity"]["environment"]
    assert identity["MOWDECK_IDENTITY_OPTIONAL_SERVICES"] == PROFILES_VARIABLE
    # Nothing in the deployment replaces the list: it would have to be kept in step by hand.
    assert "MOWDECK_IDENTITY_ERASURE_SERVICES" not in identity
    for name in _optional(repo_root):
        assert compose[name]["environment"][_variable(name)] == PROFILES_VARIABLE
        snippet = repo_root / "services" / name / "compose.snippet.yml"
        own = yaml.safe_load(snippet.read_text("utf-8"))["services"][name]["environment"]
        assert own[_variable(name)] == PROFILES_VARIABLE


def test_identity_asks_an_optional_service_exactly_when_the_deployment_names_it(
    repo_root: Path,
) -> None:
    for name in _optional(repo_root):
        named = _identity_settings(identity_optional_services=f"relay,{name}")
        assert name in named.erasure_config().services
        assert (
            name
            not in _identity_settings(identity_optional_services="relay").erasure_config().services
        )


def test_billing_does_not_start_where_identity_would_not_ask_it() -> None:
    """The other half: ``docker compose --profile billing`` without ``COMPOSE_PROFILES`` starts
    Billing and tells Identity nothing. The container's entry point refuses that."""
    from billing import main as billing_main
    from billing.settings import Settings

    assert "ensure_identity_asks(settings)" in inspect.getsource(billing_main.main)
    unasked = Settings.model_validate({"environment": "test", "billing_provider": "fake"})
    with pytest.raises(ValueError, match="MOWDECK_BILLING_OPTIONAL_SERVICES"):
        billing_main.ensure_identity_asks(unasked)
    asked = Settings.model_validate(
        {"environment": "test", "billing_provider": "fake", "billing_optional_services": "billing"}
    )
    billing_main.ensure_identity_asks(asked)


def test_the_chart_names_billing_to_identity_from_its_own_toggle(repo_root: Path) -> None:
    templates = repo_root / "infra" / "helm" / "mowdeck" / "templates"
    identity = (templates / "identity.yaml").read_text("utf-8")
    assert (
        '"MOWDECK_IDENTITY_OPTIONAL_SERVICES" (ternary "billing" "" '
        ".Values.services.billing.enabled)"
    ) in identity
    assert '"MOWDECK_BILLING_OPTIONAL_SERVICES" "billing"' in (
        templates / "billing.yaml"
    ).read_text("utf-8")
    # "default true" would turn an operator's explicit false into true.
    assert '(ne (toString $c.IDENTITY_ERASURE_ENABLED) "false")' in identity
    assert "default true $c.IDENTITY_ERASURE_ENABLED" not in identity


def _rendered_settings(repo_root: Path, *sets: str) -> dict[str, str]:
    """The erasure settings of the rendered chart, by variable."""
    chart = repo_root / "infra" / "helm" / "mowdeck"
    command = ["helm", "template", "mowdeck", str(chart), "-f", str(chart / "ci/test-values.yaml")]
    for value in ("config.NOTIFICATIONS_VAPID_PUBLIC_KEY=placeholder", *sets):
        command += ["--set", value]
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
    found: dict[str, str] = {}
    for doc in yaml.safe_load_all(result.stdout):
        if doc and doc["kind"] == "ConfigMap":
            for key, value in (doc.get("data") or {}).items():
                if key.endswith(("_OPTIONAL_SERVICES", "IDENTITY_ERASURE_ENABLED")):
                    found[key] = value
    return found


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_rendered_chart_asks_billing_exactly_when_it_is_enabled(repo_root: Path) -> None:
    if not list((repo_root / "infra/helm/mowdeck/charts").glob("*.tgz")):
        pytest.skip("chart dependencies missing (helm dependency build infra/helm/mowdeck)")
    without = _rendered_settings(repo_root, "services.billing.enabled=false")
    assert without == {
        "MOWDECK_IDENTITY_ERASURE_ENABLED": "true",
        "MOWDECK_IDENTITY_OPTIONAL_SERVICES": "",
    }
    with_billing = _rendered_settings(repo_root, "services.billing.enabled=true")
    assert with_billing == {
        "MOWDECK_IDENTITY_ERASURE_ENABLED": "true",
        "MOWDECK_IDENTITY_OPTIONAL_SERVICES": "billing",
        "MOWDECK_BILLING_OPTIONAL_SERVICES": "billing",
    }
    off = _rendered_settings(repo_root, "config.IDENTITY_ERASURE_ENABLED=false")
    assert off["MOWDECK_IDENTITY_ERASURE_ENABLED"] == "false"
