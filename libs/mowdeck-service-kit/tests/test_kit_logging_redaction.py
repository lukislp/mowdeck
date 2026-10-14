import copy
from dataclasses import dataclass
from typing import cast

from mowdeck_service_kit.logging import redact_secrets


def run(event: dict[str, object]) -> dict[str, object]:
    return cast("dict[str, object]", redact_secrets(None, "info", event))


def test_top_level_and_case_insensitive() -> None:
    out = run({"event": "x", "Password": "p", "API_KEY": "k", "Authorization": "Bearer a"})
    assert out == {"event": "x", "Password": "***", "API_KEY": "***", "Authorization": "***"}


def test_nested_dicts_and_lists() -> None:
    out = run({"data": {"a": [{"token": "t", "name": "n"}, ("x", {"psk": "s"})]}})
    assert out == {"data": {"a": [{"token": "***", "name": "n"}, ("x", {"psk": "***"})]}}


def test_none_stays_none() -> None:
    assert run({"password": None}) == {"password": None}


def test_connection_options() -> None:
    out = run(
        {"connection_options": {"host": "h", "noise_psk": "abc", "api_password": "z", "port": 1}}
    )
    assert out["connection_options"] == {
        "host": "h",
        "noise_psk": "***",
        "api_password": "***",
        "port": 1,
    }


def test_key_suffix_rules() -> None:
    out = run({"product_key": "p", "mower_key": "m", "encryption_key": "e", "my_api_key": "a"})
    assert out == {
        "product_key": "p",
        "mower_key": "m",
        "encryption_key": "***",
        "my_api_key": "***",
    }


def test_no_mutation() -> None:
    inner = {"secret": "s", "ok": 1}
    items = [inner]
    event: dict[str, object] = {"x": items, "credentials": {"a": 1}}
    snapshot = copy.deepcopy(event)
    out = run(event)
    assert event == snapshot
    assert out["x"] == [{"secret": "***", "ok": 1}]
    assert out["credentials"] == "***"


def test_ordinary_event_is_passed_through_untouched() -> None:
    nested = {"a": [1, {"b": 2}]}
    event: dict[str, object] = {"event": "hello", "n": nested}
    out = run(event)
    assert out is event
    assert out["n"] is nested


@dataclass
class Registration:
    password: str


def test_objects_are_left_untouched() -> None:
    reg = Registration(password="p")  # noqa: S106
    out = run({"registration": reg})
    assert out["registration"] is reg
    assert reg.password == "p"  # noqa: S105
