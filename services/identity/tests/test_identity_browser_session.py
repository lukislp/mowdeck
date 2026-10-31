"""Browser session support: cookie-only refresh transport, the refresh reuse grace and the
web app's ``ui-areas``."""

from datetime import timedelta

from identity_support import API, GATEWAY, PASSWORD, FakeModules, harness
from mowdeck_contracts.v1 import ModuleDescriptor, PermissionDef, UiAreaDef

COOKIE_ONLY = {"X-Mowdeck-Refresh": "cookie-only"}
CSRF = {"X-Requested-With": "fetch"}


async def test_cookie_only_login_keeps_the_refresh_token_out_of_the_body() -> None:
    async with harness() as h:
        await h.register("lukas")
        h.client.cookies.clear()
        login = await h.client.post(
            f"{API}/session",
            json={"username": "lukas", "password": PASSWORD},
            headers=COOKIE_ONLY,
        )
        cookie = h.client.cookies.get("mowdeck_refresh")
        refreshed = await h.client.post(f"{API}/session/refresh", headers={**CSRF, **COOKIE_ONLY})
        rotated_cookie = h.client.cookies.get("mowdeck_refresh")
        plain = await h.client.post(f"{API}/session/refresh", headers=CSRF)
    assert login.status_code == 200
    assert login.json()["refresh_token"] is None
    assert login.json()["access_token"]
    assert login.json()["refresh_expires_at"] is not None
    assert cookie
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] is None
    assert rotated_cookie and rotated_cookie != cookie
    assert plain.status_code == 200
    assert plain.json()["refresh_token"]  # without the header the body still carries it


def _cookie(response: object, name: str) -> str:
    """The ``Set-Cookie`` line of ``name`` in a response."""
    lines = response.headers.get_list("set-cookie")  # type: ignore[attr-defined]
    return next(line for line in lines if line.startswith(f"{name}="))


async def test_the_refresh_cookie_has_a_readable_companion_dropped_with_it() -> None:
    async with harness() as h:
        await h.register("lukas")
        h.client.cookies.clear()
        login = await h.client.post(
            f"{API}/session",
            json={"username": "lukas", "password": PASSWORD},
            headers=COOKIE_ONLY,
        )
        refreshed = await h.client.post(f"{API}/session/refresh", headers={**CSRF, **COOKIE_ONLY})
        logout = await h.client.post(f"{API}/session/logout", headers=CSRF)
    for response in (login, refreshed):
        hint = _cookie(response, "mowdeck_session")
        refresh = _cookie(response, "mowdeck_refresh")
        # Readable by the page (no secret in it), for every path, with the refresh cookie's age.
        assert hint.startswith("mowdeck_session=1;")
        assert "HttpOnly" not in hint and "Path=/;" in hint and "SameSite=lax" in hint
        age = next(p for p in refresh.split("; ") if p.startswith("Max-Age="))
        assert age in hint
    assert logout.status_code == 204
    dropped = _cookie(logout, "mowdeck_session")
    assert "Max-Age=0" in dropped and "Path=/" in dropped
    assert "Max-Age=0" in _cookie(logout, "mowdeck_refresh")


async def test_cookie_refresh_ignores_a_stale_access_token_on_the_request() -> None:
    async with harness() as h:
        await h.register("lukas")
        h.client.cookies.clear()
        login = await h.client.post(
            f"{API}/session",
            json={"username": "lukas", "password": PASSWORD},
            headers=COOKIE_ONLY,
        )
        old = login.json()["access_token"]
        stale = {"Authorization": f"Bearer {old}", **CSRF, **COOKIE_ONLY}
        garbage = {"Authorization": "Bearer not-a-token", **CSRF, **COOKIE_ONLY}
        first = await h.client.post(f"{API}/session/refresh", headers=stale)
        second = await h.client.post(f"{API}/session/refresh", headers=garbage)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text  # the refresh endpoint reads the cookie only


async def test_refresh_grace_reissues_after_a_lost_response() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        lost = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        h.clock.advance(timedelta(seconds=5))
        retried = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        graces = await h.audit(lukas, action="session.refresh_grace")
        # Only the immediate predecessor of the newest token qualifies: the original token's
        # successor is now used up, so presenting the original token once more is a reuse.
        replay = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        newest = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": retried.json()["refresh_token"]}
        )
    assert lost.status_code == 200
    assert retried.status_code == 200
    assert retried.json()["refresh_token"] not in (
        lost.json()["refresh_token"],
        lukas.refresh_token,
    )
    assert retried.json()["session_id"] == lost.json()["session_id"]
    assert len(graces) == 1
    assert replay.status_code == 401
    assert newest.status_code == 401  # the reuse revoked the whole family


async def test_refresh_grace_ends_after_ten_seconds() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        first = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        h.clock.advance(timedelta(seconds=11))
        late = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        successor = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": first.json()["refresh_token"]}
        )
    assert first.status_code == 200
    assert late.status_code == 401
    assert successor.status_code == 401  # the family is revoked


async def test_refresh_grace_does_not_apply_once_the_successor_was_used() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        first = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        second = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": first.json()["refresh_token"]}
        )
        replay = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
        newest = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": second.json()["refresh_token"]}
        )
    assert second.status_code == 200
    assert replay.status_code == 401
    assert newest.status_code == 401


async def test_refresh_grace_can_be_disabled() -> None:
    async with harness(identity_refresh_reuse_grace_seconds=0) as h:
        lukas = await h.register("lukas")
        await h.client.post(f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token})
        again = await h.client.post(
            f"{API}/session/refresh", json={"refresh_token": lukas.refresh_token}
        )
    assert again.status_code == 401


FLEET = ModuleDescriptor(
    name="fleet",
    version="0.2.0",
    permissions=[PermissionDef(key="mower.view", description="View mowers")],
    ui_areas=[
        UiAreaDef(
            route="/mowers",
            menu_label="Mowers",
            required_permission="mower.view",
            labels={"de": "Mäher"},
        ),
        UiAreaDef(route="/fleet/admin", menu_label="Admin", required_permission="fleet.admin"),
    ],
)


async def test_ui_areas_merge_descriptors_and_filter_by_permission() -> None:
    async with harness(modules=FakeModules(GATEWAY, FLEET)) as h:
        owner = await h.register("lukas")
        owner_areas = await h.client.get(f"{API}/ui-areas", headers=owner.headers)
        _, viewer_token = await h.subuser(owner, "vera")
        viewer_areas = await h.client.get(
            f"{API}/ui-areas", headers={"Authorization": f"Bearer {viewer_token}"}
        )
        anonymous = await h.client.get(f"{API}/ui-areas")
    assert owner_areas.status_code == 200
    assert owner_areas.headers["cache-control"] == "private, max-age=300"
    by_route = {a["route"]: a for a in owner_areas.json()}
    assert by_route["/mowers"]["module"] == "fleet"
    assert by_route["/mowers"]["labels"] == {"de": "Mäher"}
    assert by_route["/settings/users"]["module"] == "identity"
    assert "/fleet/admin" not in by_route  # nobody holds fleet.admin
    assert viewer_areas.status_code == 200
    assert viewer_areas.json() == []  # a sub-user without roles sees nothing
    assert anonymous.status_code == 401


async def test_ui_areas_keep_the_last_descriptor_of_a_module_that_went_down() -> None:
    modules = FakeModules(GATEWAY, FLEET)
    async with harness(modules=modules) as h:
        owner = await h.register("lukas")
        modules.down.add("fleet")
        await h.app.state.identity.deps.catalogue.refresh(fetch_modules=True)
        areas = await h.client.get(f"{API}/ui-areas", headers=owner.headers)
    assert "/mowers" in {a["route"] for a in areas.json()}
