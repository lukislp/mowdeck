"""The provider conformance kit (ADR 0069) on the fake Mammotion cloud."""

from collections.abc import Iterator

from mowdeck_control_mammotion import errors, lexicon
from mowdeck_control_mammotion.fake_cloud import FAKE_CLOUD_DOMAIN, FAKE_CLOUD_PASSWORD
from mowdeck_provider_testkit import ProviderConformance, ProviderUnderTest


def _reported_codes() -> Iterator[str]:
    """The codes the mapping reports: PyMammotion's bundled table and the codes MowDeck acts on
    (lift and tilt, recoverable), without the table's "Request succeeded"."""
    codes = {*errors.known_codes(), *errors.LIFT_TILT_CODES, *errors.RECOVERABLE_CODES}
    return (str(code) for code in sorted(codes) if str(code) not in lexicon.SKIPPED_CODES)


class TestMammotionConformance(ProviderConformance):
    subject = ProviderUnderTest(
        distribution="mowdeck-control-mammotion",
        fake_credentials={
            "email": f"conformance@{FAKE_CLOUD_DOMAIN}",
            "password": FAKE_CLOUD_PASSWORD,
        },
        emitted_error_codes=_reported_codes,
        known_violations={
            "test_provider_spec_imports_without_vendor_sdk": (
                "the package __init__ imports the connections, which import PyMammotion, so "
                "loading the spec loads the SDK; harmless while the provider is on by default "
                "(the gateway creates it anyway), a lazy package __init__ would fix it"
            ),
        },
    )
