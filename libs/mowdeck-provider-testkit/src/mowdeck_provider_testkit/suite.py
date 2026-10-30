"""The pytest suite a provider package inherits: :class:`ProviderConformance`.

Usage in a provider package's tests (the class name must start with ``Test``)::

    from mowdeck_provider_testkit import ProviderConformance, ProviderUnderTest

    class TestAcmeConformance(ProviderConformance):
        subject = ProviderUnderTest(
            distribution="mowdeck-control-acme",
            fake_credentials={"email": "garden@fake.example", "password": "fake"},
        )

The async tests need ``pytest-asyncio`` with ``asyncio_mode = "auto"``. Checks that do not
apply to the package (no cloud provider, no settings, ...) are skipped with the reason; a known
violation is listed in ``ProviderUnderTest.known_violations`` and then runs as a strict xfail.
"""

from collections.abc import AsyncIterator
from typing import ClassVar

import pytest

from mowdeck_provider_testkit import checks
from mowdeck_provider_testkit.harness import MowerUnderTest, open_mower, provider_specs
from mowdeck_provider_testkit.package import InstalledPackage, installed, is_installed
from mowdeck_provider_testkit.subject import ProviderUnderTest


class ProviderConformance:
    """Every conformance check, as pytest tests on :attr:`subject`."""

    subject: ClassVar[ProviderUnderTest]

    # --- Fixtures -----------------------------------------------------------------------------

    @pytest.fixture(autouse=True)
    def _known_violation(self, request: pytest.FixtureRequest) -> None:
        name = getattr(request.node, "originalname", None) or request.node.name
        reason = self.subject.known_violations.get(name)
        if reason:
            request.applymarker(pytest.mark.xfail(reason=reason, strict=True))

    @pytest.fixture
    def package(self) -> InstalledPackage:
        """The installed distribution under test."""
        if not is_installed(self.subject.distribution):
            pytest.fail(f"{self.subject.distribution} is not installed")
        return installed(self.subject.distribution)

    @pytest.fixture
    async def mower(self, package: InstalledPackage) -> AsyncIterator[MowerUnderTest]:
        """A fresh connection on the fake vendor client (closed afterwards)."""
        async with open_mower(self.subject, package) as mower:
            yield mower

    def _require_cloud_provider(self, package: InstalledPackage) -> None:
        if not provider_specs(package):
            pytest.skip("the package registers no cloud provider")
        if self.subject.fake_credentials is None:
            pytest.skip("no fake_credentials given for the cloud provider")

    # --- The package --------------------------------------------------------------------------

    def test_package_layout(self, package: InstalledPackage) -> None:
        checks.check_package_layout(package)

    def test_entry_points_declared(self, package: InstalledPackage) -> None:
        checks.check_entry_points_declared(package)

    def test_entry_points_load(self, package: InstalledPackage) -> None:
        checks.check_entry_points_load(package)

    def test_vendor_modules_declared(self, package: InstalledPackage) -> None:
        if package.adapter_table() is None:
            pytest.skip("no pyproject.toml next to the package (installed from a wheel)")
        checks.check_vendor_modules_declared(package)

    def test_no_other_adapter_imported(self, package: InstalledPackage) -> None:
        checks.check_no_other_adapter_imported(package)

    def test_provider_spec_imports_without_vendor_sdk(self, package: InstalledPackage) -> None:
        if not provider_specs(package):
            pytest.skip("the package registers no cloud provider")
        if package.adapter_table() is None:
            pytest.skip("no pyproject.toml next to the package (vendor-modules unknown)")
        checks.check_specs_import_without_the_vendor_sdk(package)

    # --- The cloud provider -------------------------------------------------------------------

    async def test_provider_descriptor(self, package: InstalledPackage) -> None:
        if not provider_specs(package):
            pytest.skip("the package registers no cloud provider")
        await checks.check_provider_descriptor(package, self.subject)

    async def test_provider_discovery(self, package: InstalledPackage) -> None:
        self._require_cloud_provider(package)
        await checks.check_provider_discovery(package, self.subject)

    # --- The connection -----------------------------------------------------------------------

    async def test_connection_lifecycle(self, mower: MowerUnderTest) -> None:
        await checks.check_connection_lifecycle(mower)

    async def test_capabilities_and_state(
        self, package: InstalledPackage, mower: MowerUnderTest
    ) -> None:
        await checks.check_capabilities_and_state(mower, checks.package_lexicons(package))

    async def test_mower_can_be_started(self, mower: MowerUnderTest) -> None:
        await checks.check_mower_can_be_started(mower)

    async def test_base_job_flow(self, package: InstalledPackage, mower: MowerUnderTest) -> None:
        await checks.check_base_job_flow(mower, checks.package_lexicons(package))

    async def test_undeclared_commands_refused(self, mower: MowerUnderTest) -> None:
        await checks.check_undeclared_commands_refused(mower)

    async def test_declared_commands_accepted(self, mower: MowerUnderTest) -> None:
        if not await checks.check_declared_commands_accepted(mower):
            pytest.skip("no blade height, speed or light declared")

    async def test_start_plans(self, mower: MowerUnderTest) -> None:
        if not await checks.check_start_plans(mower):
            pytest.skip("no start_plans declared")

    async def test_keep_job_on_return(self, mower: MowerUnderTest) -> None:
        if not await checks.check_keep_job_on_return(mower):
            pytest.skip("dock_keeps_job is not declared")

    async def test_native_pattern_start(self, mower: MowerUnderTest) -> None:
        if await checks.check_native_pattern_start(mower) is None:
            pytest.skip("no map areas or no native_patterns")

    async def test_settings_round_trip(self, mower: MowerUnderTest) -> None:
        if not await checks.check_settings_round_trip(mower):
            pytest.skip("no settings declared")

    async def test_manual_drive(self, mower: MowerUnderTest) -> None:
        outcome = await checks.check_manual_drive(mower)
        if outcome.startswith("refused in the fake mower's state"):
            pytest.skip(outcome)

    # --- Safety (ADR 0012) --------------------------------------------------------------------

    async def test_emergency_stop_at_rest(self, mower: MowerUnderTest) -> None:
        await checks.check_emergency_stop_at_rest(mower)

    async def test_emergency_stop_while_busy(self, mower: MowerUnderTest) -> None:
        await checks.check_emergency_stop_while_busy(mower)

    async def test_emergency_stop_offline_mower(self, package: InstalledPackage) -> None:
        self._require_cloud_provider(package)
        if not await checks.check_emergency_stop_offline(package, self.subject):
            pytest.skip("the fake account has no offline mower the connection can reach")

    # --- The error lexicon (ADR 0067) ---------------------------------------------------------

    def test_lexicon(self, package: InstalledPackage) -> None:
        checks.check_lexicon(package)

    def test_lexicon_covers_emitted_codes(self, package: InstalledPackage) -> None:
        codes = self.subject.emitted_error_codes
        if codes is None:
            pytest.skip("ProviderUnderTest.emitted_error_codes not given")
        if not checks.check_lexicon_covers(package, codes()):
            pytest.skip("the adapter names no error codes")
