"""HTTP API: operational endpoints, every route, validation and cross-account negatives."""

from uuid import uuid4

from mowdeck_contracts.v1 import MODULE_DESCRIPTOR_PATH, Principal, PrincipalKind
from notifications_support import API, AUTH, P256DH, Harness, Person, harness

from notifications.module import DESCRIPTOR, MANAGE_PERMISSION


async def push(h: Harness, person: Person, endpoint: str = "https://push.example.test/s1") -> str:
    response = await h.client.post(
        f"{API}/push-subscriptions",
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": P256DH, "auth": AUTH},
            "expirationTime": None,
        },
        headers={**person.headers, "User-Agent": "Firefox"},
    )
    assert response.status_code == 201, response.text
    subscription_id: str = response.json()["id"]
    return subscription_id


async def test_notifications_api_health_descriptor_and_auth() -> None:
    async with harness() as h:
        assert (await h.client.get("/healthz")).json() == {"status": "ok"}
        assert (await h.client.get("/readyz")).status_code == 200
        descriptor = (await h.client.get(MODULE_DESCRIPTOR_PATH)).json()
        anonymous = await h.client.get(f"{API}/preferences")
        token = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.API_TOKEN)
        api_token = await h.client.get(
            f"{API}/preferences", headers={"Authorization": f"Bearer {h.keys.mint(token)}"}
        )
        owner = await h.owner(perms=[])  # no perms in the token, but owner can view mowers
        no_mowers = await h.client.get(f"{API}/preferences", headers=owner.headers)
        await h.add_mower(owner)
        with_mower = await h.client.get(f"{API}/preferences", headers=owner.headers)
    assert descriptor == DESCRIPTOR.model_dump(mode="json")
    permission = descriptor["permissions"][0]
    assert permission["key"] == MANAGE_PERMISSION and permission["implied_by"] == ["mower.view"]
    assert descriptor["events"][0]["type"] == "notifications.sent"
    assert anonymous.status_code == 401
    assert api_token.status_code == 403
    assert no_mowers.status_code == 403
    assert with_mower.status_code == 200


async def test_notifications_api_preferences_round_trip_and_validation() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        defaults = (await h.client.get(f"{API}/preferences", headers=owner.headers)).json()
        body = {
            "enabled_kinds": ["battery_low", "job_started"],
            "mower_ids": [str(mower_id)],
            "quiet_hours": {"start": "22:00", "end": "07:00"},
            "timezone": "Europe/Berlin",
            "language": "de",
            "channels": ["webpush"],
        }
        saved = await h.client.put(f"{API}/preferences", json=body, headers=owner.headers)
        again = (await h.client.get(f"{API}/preferences", headers=owner.headers)).json()
        bad = {
            "kind": {**body, "enabled_kinds": ["test"]},
            "zone": {**body, "timezone": "Mars/Base"},
            "lang": {**body, "language": "fr"},
            "channel": {**body, "channels": ["pigeon"]},
            "time": {**body, "quiet_hours": {"start": "25:00", "end": "07:00"}},
            "extra": {**body, "account_id": str(uuid4())},
        }
        answers = {
            name: (
                await h.client.put(f"{API}/preferences", json=b, headers=owner.headers)
            ).status_code
            for name, b in bad.items()
        }
    assert defaults["stored"] is False and defaults["all_mowers"] is False
    assert "job_started" not in defaults["enabled_kinds"] and defaults["channels"]
    assert defaults["available_channels"] == ["log"] and defaults["languages"] == ["de", "en"]
    assert saved.status_code == 200
    assert again["stored"] is True and again["mower_ids"] == [str(mower_id)]
    assert again["quiet_hours"] == {"start": "22:00", "end": "07:00"}
    assert again["enabled_kinds"] == ["battery_low", "job_started"]
    assert answers == dict.fromkeys(bad, 422)


async def test_notifications_api_mowers_of_other_accounts_cannot_be_selected() -> None:
    async with harness() as h:
        owner = await h.owner()
        intruder = await h.owner()
        mower_id = await h.add_mower(owner)
        unannounced = await h.add_mower(intruder, announce=False)
        own = await h.add_mower(intruder)
        member = await h.member(owner)
        await h.grant_view(member, await h.add_mower(owner))  # sees another mower only

        async def select(person: Person, mower: object) -> int:
            response = await h.client.put(
                f"{API}/preferences",
                json={"enabled_kinds": ["battery_low"], "mower_ids": [str(mower)]},
                headers=person.headers,
            )
            return response.status_code

        foreign = await select(intruder, mower_id)
        unknown = await select(intruder, uuid4())
        not_in_directory = await select(intruder, unannounced)
        not_visible = await select(member, mower_id)
        allowed = await select(intruder, own)
    assert (foreign, unknown, not_in_directory, not_visible) == (404, 404, 404, 404)
    assert allowed == 200


async def test_notifications_api_push_subscriptions_are_per_user() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        other = await h.owner()
        await h.add_mower(other)
        subscription_id = await push(h, owner)
        same_endpoint = await push(h, owner)  # re-subscribing replaces, does not duplicate
        listed = (await h.client.get(f"{API}/push-subscriptions", headers=owner.headers)).json()
        foreign_list = (
            await h.client.get(f"{API}/push-subscriptions", headers=other.headers)
        ).json()
        foreign_delete = await h.client.delete(
            f"{API}/push-subscriptions/{subscription_id}", headers=other.headers
        )
        invalid = [
            await h.client.post(
                f"{API}/push-subscriptions",
                json={"endpoint": endpoint, "keys": keys},
                headers=owner.headers,
            )
            for endpoint, keys in (
                ("http://push.example.test/x", {"p256dh": P256DH, "auth": AUTH}),
                ("https://evil.example.com/x", {"p256dh": P256DH, "auth": AUTH}),
                ("https://push.example.test/x", {"p256dh": "AAAA", "auth": AUTH}),
                ("https://push.example.test/x", {"p256dh": P256DH, "auth": "!!!"}),
            )
        ]
        deleted = await h.client.delete(
            f"{API}/push-subscriptions/{subscription_id}", headers=owner.headers
        )
        missing = await h.client.delete(
            f"{API}/push-subscriptions/{subscription_id}", headers=owner.headers
        )
    assert same_endpoint == subscription_id
    assert [i["endpoint_host"] for i in listed["items"]] == ["push.example.test"]
    assert listed["items"][0]["user_agent"] == "Firefox"
    assert foreign_list == {"items": []} and foreign_delete.status_code == 404
    assert [r.status_code for r in invalid] == [422, 422, 422, 422]
    assert deleted.status_code == 204 and missing.status_code == 404


async def test_notifications_api_ntfy_target_is_per_user_and_allow_listed() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        other = await h.owner()
        await h.add_mower(other)
        empty = await h.client.get(f"{API}/ntfy", headers=owner.headers)
        saved = await h.client.put(
            f"{API}/ntfy",
            json={"server_url": "https://NTFY.example.test/", "topic": "lawn-1", "token": "tk_x"},
            headers=owner.headers,
        )
        read = await h.client.get(f"{API}/ntfy", headers=owner.headers)
        foreign = await h.client.get(f"{API}/ntfy", headers=other.headers)
        rejected = [
            (await h.client.put(f"{API}/ntfy", json=body, headers=owner.headers)).status_code
            for body in (
                {"server_url": "https://ntfy.sh", "topic": "x"},
                {"server_url": "http://169.254.169.254", "topic": "x"},
                {"server_url": "https://ntfy.example.test/path", "topic": "x"},
                {"server_url": "ftp://ntfy.example.test", "topic": "x"},
                {"server_url": "https://ntfy.example.test", "topic": "a/b"},
            )
        ]
        foreign_delete = await h.client.delete(f"{API}/ntfy", headers=other.headers)
        deleted = await h.client.delete(f"{API}/ntfy", headers=owner.headers)
        gone = await h.client.get(f"{API}/ntfy", headers=owner.headers)
    assert empty.status_code == 404
    assert saved.status_code == 200
    assert read.json()["server_url"] == "https://ntfy.example.test"
    assert read.json()["has_token"] is True and "token" not in read.json()
    assert foreign.status_code == 404
    assert rejected == [422, 422, 422, 422, 422]
    assert foreign_delete.status_code == 404 and deleted.status_code == 204
    assert gone.status_code == 404


async def test_notifications_api_ntfy_token_is_kept_unless_cleared() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        url = f"{API}/ntfy"
        body = {"server_url": "https://ntfy.example.test", "topic": "lawn-1"}
        await h.client.put(url, json={**body, "token": "tk_x"}, headers=owner.headers)
        kept = await h.client.put(url, json={**body, "topic": "lawn-2"}, headers=owner.headers)
        cleared = await h.client.put(url, json={**body, "token": ""}, headers=owner.headers)
    assert kept.json()["has_token"] is True and kept.json()["topic"] == "lawn-2"
    assert cleared.json()["has_token"] is False


async def test_notifications_api_test_notification_and_inbox() -> None:
    async with harness(notifications_test_interval_seconds=60) as h:
        owner = await h.owner()
        await h.add_mower(owner)
        other = await h.owner()
        await h.add_mower(other)
        await h.subscribe_all(owner, language="de")
        sent = await h.client.post(f"{API}/test", headers=owner.headers)
        limited = await h.client.post(f"{API}/test", headers=owner.headers)
        h.clock.advance(60)
        again = await h.client.post(f"{API}/test", headers=owner.headers)
        await h.services.outbox.run_once()
        inbox = (await h.client.get(f"{API}/inbox", headers=owner.headers)).json()
        item_id = inbox["items"][0]["id"]
        foreign_inbox = (await h.client.get(f"{API}/inbox", headers=other.headers)).json()
        foreign_read = await h.client.post(f"{API}/inbox/{item_id}/read", headers=other.headers)
        read = await h.client.post(f"{API}/inbox/{item_id}/read", headers=owner.headers)
        read_again = await h.client.post(f"{API}/inbox/{item_id}/read", headers=owner.headers)
        unread = (await h.client.get(f"{API}/inbox?unread=true", headers=owner.headers)).json()
        missing = await h.client.post(f"{API}/inbox/{uuid4()}/read", headers=owner.headers)
        limit = await h.client.get(f"{API}/inbox?limit=1", headers=owner.headers)
        bad_limit = await h.client.get(f"{API}/inbox?limit=0", headers=owner.headers)
    assert sent.status_code == 202
    assert sent.json()["channels"] == ["log"]
    assert sent.json()["title"] == "MowDeck-Testbenachrichtigung"
    assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
    assert again.status_code == 202
    assert len(h.channel.sent) == 2
    assert inbox["unread"] == 2 and [i["kind"] for i in inbox["items"]] == ["test", "test"]
    assert foreign_inbox == {"items": [], "unread": 0}
    assert foreign_read.status_code == 404
    assert read.status_code == 200 and read.json()["read"] is True
    assert read_again.json()["read_at"] == read.json()["read_at"]
    assert unread["unread"] == 1 and len(unread["items"]) == 1
    assert missing.status_code == 404
    assert len(limit.json()["items"]) == 1 and bad_limit.status_code == 422


async def test_notifications_api_inbox_keeps_the_newest() -> None:
    async with harness(notifications_inbox_size=3, notifications_test_interval_seconds=0) as h:
        owner = await h.owner()
        await h.add_mower(owner)
        for _ in range(5):
            h.clock.advance(1)
            assert (await h.client.post(f"{API}/test", headers=owner.headers)).status_code == 202
        items = await h.inbox(owner)
    assert len(items) == 3
    assert items[0]["created_at"] > items[-1]["created_at"]


async def test_notifications_api_vapid_public_key() -> None:
    from notifications_support import RecordingChannel

    from notifications.adapters.channels.vapid import generate_private_key, load_keys

    private = generate_private_key()
    public = load_keys(private).public_key
    async with harness(
        channels={"webpush": RecordingChannel("webpush")},
        notifications_vapid_private_key=private,
        notifications_vapid_subject="mailto:ops@example.test",
    ) as h:
        configured = await h.client.get(f"{API}/push/vapid-public-key")
    async with harness() as h:
        missing = await h.client.get(f"{API}/push/vapid-public-key")
    assert configured.status_code == 200 and configured.json() == {"public_key": public}
    assert missing.status_code == 404


async def test_notifications_api_starting_service_answers_503() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        services = h.app.state.notifications
        h.app.state.notifications = None
        starting = await h.client.get(f"{API}/inbox", headers=owner.headers)
        h.app.state.notifications = services
    assert starting.status_code == 503
