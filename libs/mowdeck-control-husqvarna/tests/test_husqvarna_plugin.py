"""The provider spec, its descriptor and how the gateway loads it (ADR 0066)."""

import subprocess
import sys
import textwrap
from collections.abc import Iterable
from importlib.metadata import EntryPoint

from mowdeck_contracts.v1 import CredentialFieldKind
from mowdeck_control import CloudProviderPlugin, ProviderContext, load_providers
from mowdeck_control.providers import installed_specs
from mowdeck_control_husqvarna.descriptor import (
    APP_KEY,
    APP_SECRET,
    DESCRIPTOR,
    DEVELOPER_PORTAL,
)
from mowdeck_control_husqvarna.fake_cloud import FAKE_APP_KEY, FAKE_APP_SECRET
from mowdeck_control_husqvarna.plugin import SPEC, create
from mowdeck_control_husqvarna.provider import HusqvarnaProvider


def test_spec_is_installed_and_off_by_default() -> None:
    assert installed_specs()["husqvarna"] is SPEC
    assert SPEC.provider == DESCRIPTOR.provider == "husqvarna"
    assert not SPEC.enabled_by_default
    assert not SPEC.unqualified_cloud


def test_descriptor_asks_for_application_key_and_secret() -> None:
    assert DESCRIPTOR.name == "Husqvarna Automower Connect"
    assert DESCRIPTOR.identity_field.key == APP_KEY
    assert DESCRIPTOR.identity_field.kind is CredentialFieldKind.TEXT
    assert DESCRIPTOR.secret_keys == frozenset({APP_SECRET})
    assert DESCRIPTOR.needs_developer_account
    assert not DESCRIPTOR.tested_on_hardware
    assert [link.url for link in DESCRIPTOR.links] == [DEVELOPER_PORTAL]
    assert any("Automower Connect API" in step for step in DESCRIPTOR.setup_steps)


def test_descriptor_validates_the_credentials() -> None:
    good = DESCRIPTOR.normalized({APP_KEY: f" {FAKE_APP_KEY} ", APP_SECRET: FAKE_APP_SECRET})
    assert good[APP_KEY] == FAKE_APP_KEY
    assert DESCRIPTOR.problems(good) == []
    uuid_key = {APP_KEY: "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d", APP_SECRET: "x"}
    assert DESCRIPTOR.problems(uuid_key) == []
    bad = DESCRIPTOR.problems({APP_KEY: "no spaces allowed", APP_SECRET: "x"})
    assert [(p.key, p.code) for p in bad] == [(APP_KEY, "format")]
    missing = DESCRIPTOR.problems({APP_KEY: FAKE_APP_KEY})
    assert [(p.key, p.code) for p in missing] == [(APP_SECRET, "required")]


def test_plugin_module_imports_without_the_vendor_sdk() -> None:
    script = textwrap.dedent(
        """
        import sys
        from importlib.abc import MetaPathFinder

        class Block(MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "aioautomower" or name.startswith("aioautomower."):
                    raise ImportError("blocked: " + name)
                return None

        sys.meta_path.insert(0, Block())
        import mowdeck_control_husqvarna
        from mowdeck_control_husqvarna.plugin import SPEC
        assert SPEC.provider == "husqvarna"
        assert "aioautomower" not in sys.modules
        print("ok")
        """
    )
    done = subprocess.run(  # noqa: S603 - the test's own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


async def test_create_builds_the_fake_or_the_real_plugin() -> None:
    fake = create(ProviderContext(fake=True))
    assert isinstance(fake, CloudProviderPlugin)
    assert isinstance(fake, HusqvarnaProvider)
    assert fake.descriptor is DESCRIPTOR
    await fake.close()
    sys.modules.pop("mowdeck_control_husqvarna.fake_cloud", None)
    real = create(ProviderContext())
    assert isinstance(real, HusqvarnaProvider)
    assert "mowdeck_control_husqvarna.fake_cloud" not in sys.modules
    await real.close()


def _only_husqvarna() -> Iterable[EntryPoint]:
    return [
        EntryPoint(
            name="husqvarna",
            value="mowdeck_control_husqvarna.plugin:SPEC",
            group="mowdeck.cloud_providers",
        )
    ]


async def test_gateway_loads_it_only_when_enabled() -> None:
    off = load_providers(ProviderContext(fake=True), source=_only_husqvarna)
    assert dict(off.plugins) == {}
    on = load_providers(ProviderContext(fake=True), enable=["husqvarna"], source=_only_husqvarna)
    assert list(on.plugins) == ["husqvarna"]
    assert on.unqualified_cloud is None
    assert on.for_registration({"cloud_provider": "husqvarna"}) is on.plugins["husqvarna"]
    assert on.for_registration({}) is None
    await on.close()


def test_descriptor_declares_every_option_the_connection_takes() -> None:
    """Fleet stores only what the descriptor declares (ADR 0066): every option the parser reads,
    except the credentials and the keys Fleet manages, is declared with its range."""
    import inspect
    import re

    from mowdeck_contracts.v1 import RESERVED_OPTION_KEYS
    from mowdeck_control_husqvarna import options as module

    source = inspect.getsource(module.HusqvarnaOptions.parse)
    parsed = set(re.findall(r'_text\(options, "([a-z_]+)"\)', source)) | {
        k for k in ("start_minutes",) if "START_MINUTES" in source
    }
    managed = {"device_name", "iot_id"}
    declared = {o.key for o in DESCRIPTOR.mower_options}
    assert parsed - managed == declared == {"start_minutes"}
    assert not declared & RESERVED_OPTION_KEYS
    assert not {f.key for f in DESCRIPTOR.fields} & RESERVED_OPTION_KEYS
    [start] = DESCRIPTOR.mower_options
    assert (start.type, start.minimum, start.maximum, start.default) == ("integer", 1, 10080, 60)
    parsed_options = module.HusqvarnaOptions.parse(
        {
            APP_KEY: FAKE_APP_KEY,
            APP_SECRET: FAKE_APP_SECRET,
            "iot_id": "x",
            "cloud_provider": "husqvarna",
            "linked_account_id": "y",
            "drive_dead_zone": 0.1,
        }
    )
    assert parsed_options.start_minutes == 60
