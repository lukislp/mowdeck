"""The provider conformance kit (ADR 0069) on the fake Husqvarna cloud."""

from aioautomower.model.model_mower import ERRORCODES
from mowdeck_control_husqvarna.descriptor import APP_KEY, APP_SECRET
from mowdeck_control_husqvarna.fake_cloud import FAKE_APP_KEY, FAKE_APP_SECRET
from mowdeck_provider_testkit import ProviderConformance, ProviderUnderTest


class TestHusqvarnaConformance(ProviderConformance):
    subject = ProviderUnderTest(
        distribution="mowdeck-control-husqvarna",
        fake_credentials={APP_KEY: FAKE_APP_KEY, APP_SECRET: FAKE_APP_SECRET},
        # The adapter reports aioautomower's error code as is (mapping.state_of); 0 is the API's
        # "no error" and never reported as a code.
        emitted_error_codes=lambda: (str(code) for code in ERRORCODES if code != 0),
    )
