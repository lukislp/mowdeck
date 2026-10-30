"""The provider spec, its descriptor, the mower options and how the gateway loads it."""

import inspect
import re
import subprocess
import sys
import textwrap
import tomllib
from collections.abc import Iterable
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from mowdeck_contracts.v1 import RESERVED_OPTION_KEYS, CredentialFieldKind
from mowdeck_control import (
    CloudLoginRefused,
    CloudProviderPlugin,
    ProviderContext,
    load_providers,
)
from mowdeck_control.providers import installed_specs
from mowdeck_control_worx import options as options_module
from mowdeck_control_worx.descriptor import (
    BRAND,
    BRANDS,
    COMMAND_TIMEOUT,
    DESCRIPTOR,
    EMAIL,
    OFFER_ZONES,
    PASSWORD,
    WARNING_BAN,
    WARNING_UNOFFICIAL,
)
from mowdeck_control_worx.fake_cloud import (
    FAKE_CLOUD_PASSWORD,
    FAKE_MOWER_OFFLINE,
    FAKE_MOWER_ONLINE,
)
from mowdeck_control_worx.options import WorxOptions, login_of
from mowdeck_control_worx.plugin import SPEC, create
from mowdeck_control_worx.provider import WorxProvider

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FAKE_EMAIL = "garden@fake.mowdeck.test"


def test_spec_is_installed_and_off_by_default() -> None:
    assert installed_specs()["worx"] is SPEC
    assert SPEC.provider == DESCRIPTOR.provider == "worx"
    assert not SPEC.enabled_by_default and not SPEC.unqualified_cloud


def test_pyproject_declares_the_adapter() -> None:
    data = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text("utf-8"))
    project = data["project"]
    assert project["license"] == "GPL-3.0-only"
    assert "pyworxcloud==6.4.2" in project["dependencies"]
    assert project["entry-points"]["mowdeck.cloud_providers"] == {
        "worx": "mowdeck_control_worx.plugin:SPEC"
    }
    adapter = data["tool"]["mowdeck"]["adapter"]
    assert adapter["vendor-modules"] == ["pyworxcloud"]
    assert {"landroid", "kress", "landxcape"} <= set(adapter["brand-names"])
    assert (
        (PACKAGE_ROOT / "LICENSE")
        .read_text("utf-8")
        .lstrip()
        .startswith("GNU GENERAL PUBLIC LICENSE")
    )


def test_descriptor_asks_for_brand_email_and_password() -> None:
    assert [(f.key, f.kind) for f in DESCRIPTOR.fields] == [
        (BRAND, CredentialFieldKind.CHOICE),
        (EMAIL, CredentialFieldKind.EMAIL),
        (PASSWORD, CredentialFieldKind.SECRET),
    ]
    assert DESCRIPTOR.identity_field.key == EMAIL
    assert DESCRIPTOR.secret_keys == frozenset({PASSWORD})
    brand = DESCRIPTOR.fields[0]
    assert [c.value for c in brand.choices] == list(BRANDS) == ["worx", "kress", "landxcape"]
    assert not DESCRIPTOR.needs_developer_account and not DESCRIPTOR.tested_on_hardware
    assert DESCRIPTOR.warnings == [WARNING_UNOFFICIAL, WARNING_BAN]
    assert "Unofficial API" in WARNING_UNOFFICIAL["en"] and "block" in WARNING_BAN["en"]
    assert "Inoffizielle API" in WARNING_UNOFFICIAL["de"] and "Kontosperre" in WARNING_BAN["de"]


def test_descriptor_validates_the_credentials() -> None:
    good = DESCRIPTOR.normalized({BRAND: " kress ", EMAIL: f" {FAKE_EMAIL} ", PASSWORD: "x"})
    assert good == {BRAND: "kress", EMAIL: FAKE_EMAIL, PASSWORD: "x"}
    assert DESCRIPTOR.problems(good) == []
    bad = DESCRIPTOR.problems({BRAND: "acme", EMAIL: "no-mail", PASSWORD: "x"})
    assert sorted((p.key, p.code) for p in bad) == [(BRAND, "choice"), (EMAIL, "format")]
    missing = DESCRIPTOR.problems({BRAND: "worx", EMAIL: FAKE_EMAIL})
    assert [(p.key, p.code) for p in missing] == [(PASSWORD, "required")]


def test_plugin_module_imports_without_the_vendor_sdk() -> None:
    script = textwrap.dedent(
        """
        import asyncio, sys
        from importlib.abc import MetaPathFinder

        class Block(MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "pyworxcloud" or name.startswith("pyworxcloud."):
                    raise ImportError("blocked: " + name)
                return None

        sys.meta_path.insert(0, Block())
        import mowdeck_control_worx
        from mowdeck_control import ProviderContext
        from mowdeck_control_worx.plugin import SPEC
        from mowdeck_control_worx import lexicon
        assert SPEC.provider == "worx"
        lexicon.worx_lexicon()
        real = SPEC.create(ProviderContext())
        fake = SPEC.create(ProviderContext(fake=True))
        asyncio.run(real.close())
        asyncio.run(fake.close())
        assert "pyworxcloud" not in sys.modules
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
    assert isinstance(fake, CloudProviderPlugin) and isinstance(fake, WorxProvider)
    assert fake.descriptor is DESCRIPTOR
    await fake.close()
    sys.modules.pop("mowdeck_control_worx.fake_cloud", None)
    real = create(ProviderContext())
    assert isinstance(real, WorxProvider)
    assert "mowdeck_control_worx.fake_cloud" not in sys.modules
    await real.close()


async def test_the_fake_discovery_lists_both_mowers_and_reuses_the_session() -> None:
    plugin = create(ProviderContext(fake=True))
    credentials = {BRAND: "landxcape", EMAIL: FAKE_EMAIL, PASSWORD: FAKE_CLOUD_PASSWORD}
    first = await plugin.discover(credentials)
    assert [(d.device_name, d.iot_id, d.online) for d in first.devices] == [
        ("Kurt", FAKE_MOWER_OFFLINE, False),
        ("Wanda", FAKE_MOWER_ONLINE, True),
    ]
    assert not first.reused_session
    again = await plugin.discover(credentials)
    assert again.reused_session
    with pytest.raises(CloudLoginRefused):
        await plugin.discover({**credentials, PASSWORD: "wrong"})
    with pytest.raises(CloudLoginRefused, match="missing"):
        await plugin.discover({BRAND: "worx", EMAIL: FAKE_EMAIL})
    await plugin.close()


def _only_worx() -> Iterable[EntryPoint]:
    return [
        EntryPoint(
            name="worx", value="mowdeck_control_worx.plugin:SPEC", group="mowdeck.cloud_providers"
        )
    ]


async def test_gateway_loads_it_only_when_enabled() -> None:
    off = load_providers(ProviderContext(fake=True), source=_only_worx)
    assert dict(off.plugins) == {}
    on = load_providers(ProviderContext(fake=True), enable=["worx"], source=_only_worx)
    assert list(on.plugins) == ["worx"] and on.unqualified_cloud is None
    assert on.for_registration({"cloud_provider": "worx"}) is on.plugins["worx"]
    assert on.for_registration({}) is None
    await on.close()


def test_descriptor_declares_every_option_the_connection_takes() -> None:
    """Fleet stores only what the descriptor declares (ADR 0066): every option the parser reads,
    except the credentials and the keys Fleet manages, is declared with its range."""
    source = inspect.getsource(options_module.WorxOptions.parse)
    texts = set(re.findall(r'_text\(options, "([a-z_]+)"\)', source))
    constants = {
        k
        for k, name in ((OFFER_ZONES, "OFFER_ZONES"), (COMMAND_TIMEOUT, "COMMAND_TIMEOUT"))
        if name in source
    }
    declared = {o.key for o in DESCRIPTOR.mower_options}
    assert (texts | constants) - {"iot_id", "device_name"} == declared
    assert declared == {OFFER_ZONES, COMMAND_TIMEOUT}
    assert not declared & RESERVED_OPTION_KEYS
    assert not {f.key for f in DESCRIPTOR.fields} & RESERVED_OPTION_KEYS
    by_key = {o.key: o for o in DESCRIPTOR.mower_options}
    assert (by_key[OFFER_ZONES].type, by_key[OFFER_ZONES].default) == ("boolean", True)
    timeout = by_key[COMMAND_TIMEOUT]
    assert (timeout.type, timeout.minimum, timeout.maximum, timeout.default) == (
        "number",
        5,
        120,
        30,
    )
    assert timeout.advanced


def test_options_parse_and_refuse_without_naming_credentials() -> None:
    base = {BRAND: "kress", EMAIL: FAKE_EMAIL, PASSWORD: "s3cret", "iot_id": "S1"}
    parsed = WorxOptions.parse({**base, "cloud_provider": "worx", "drive_dead_zone": 0.1})
    assert parsed.serial_number == "S1" and parsed.offer_zones and parsed.command_timeout_s == 30
    assert parsed.brand_name == "Kress" and parsed.login.brand == "kress"
    assert "s3cret" not in repr(parsed) and "s3cret" not in repr(parsed.login)
    assert login_of({EMAIL: FAKE_EMAIL, PASSWORD: "x"}).brand == "worx"  # older rows
    for broken, match in (
        ({**base, PASSWORD: ""}, "missing"),
        ({**base, BRAND: "acme"}, "unknown brand"),
        ({BRAND: "worx", EMAIL: FAKE_EMAIL, PASSWORD: "s3cret"}, "iot_id"),
        ({**base, OFFER_ZONES: "yes"}, OFFER_ZONES),
        ({**base, COMMAND_TIMEOUT: 1}, COMMAND_TIMEOUT),
        ({**base, COMMAND_TIMEOUT: True}, COMMAND_TIMEOUT),
    ):
        with pytest.raises(ValueError, match=match) as caught:
            WorxOptions.parse(broken)
        assert "s3cret" not in str(caught.value)
