"""The provider conformance kit (ADR 0069) on the fake Worx cloud."""

from mowdeck_control_worx.descriptor import BRAND, DEFAULT_BRAND, EMAIL, PASSWORD
from mowdeck_control_worx.fake_cloud import FAKE_CLOUD_DOMAIN, FAKE_CLOUD_PASSWORD
from mowdeck_control_worx.lexicon import NO_ERROR_CODES
from mowdeck_provider_testkit import ProviderConformance, ProviderUnderTest
from pyworxcloud.utils.state import ERROR_TO_DESCRIPTION


class TestWorxConformance(ProviderConformance):
    subject = ProviderUnderTest(
        distribution="mowdeck-control-worx",
        fake_credentials={
            BRAND: DEFAULT_BRAND,
            EMAIL: f"conformance@{FAKE_CLOUD_DOMAIN}",
            PASSWORD: FAKE_CLOUD_PASSWORD,
        },
        # The adapter reports pyworxcloud's error code as is (mapping.error_of); -1 and 0 are
        # "no error".
        emitted_error_codes=lambda: (
            str(code) for code in ERROR_TO_DESCRIPTION if code not in NO_ERROR_CODES
        ),
    )
