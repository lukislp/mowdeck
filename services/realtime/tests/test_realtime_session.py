"""Backpressure without a network: the outbox and a session on a fake socket that reads slowly.

* Coalescing: three states published while the client is busy arrive as the first one (already
  in flight) and the newest one; the middle one is replaced.
* Slow consumer: a send that does not finish in time closes the socket with 4430.
* The queue goes before states, revoked mowers lose their unsent states.
"""

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

from mowdeck_auth.testing import TestKeys
from mowdeck_authz import RelationshipTuple, account, mower, user
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import CommandResult, CommandStatus, Principal, PrincipalKind
from mowdeck_contracts.v1.realtime import RealtimeCloseCode
from realtime_support import make_state

from realtime.adapters.auth import JwtTokenVerifier, RejectAllTokens
from realtime.adapters.authz import AuthzAccessChecker
from realtime.application.config import RealtimeConfig
from realtime.application.hub import Hub
from realtime.application.outbox import Frame, Outbox
from realtime.application.ports import NoMetrics
from realtime.domain.models import CloseRequest, close_reason


class SlowSocket:
    """Records frames; ``gate`` blocks every send until it is set."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.closed: tuple[int, str] | None = None
        self.sending = asyncio.Event()

    async def send_text(self, text: str) -> None:
        self.sending.set()
        await self.gate.wait()
        self.sent.append(json.loads(text))

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


class CountingMetrics(NoMetrics):
    def __init__(self) -> None:
        self.drops: list[str] = []
        self.codes: list[int | None] = []

    def dropped(self, reason: str) -> None:
        self.drops.append(reason)

    def closed(self, code: int | None) -> None:
        self.codes.append(code)


async def wait_for(condition: Any, timeout: float = 3.0) -> None:  # noqa: ASYNC109
    """Poll a plain condition (no event exists for it in the fakes)."""
    async with asyncio.timeout(timeout):
        while not condition():  # noqa: ASYNC110 - polling a plain condition
            await asyncio.sleep(0.01)


class Setup:
    def __init__(self, **config: Any) -> None:
        self.keys = TestKeys()
        self.principal = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
        assert self.principal.account_id is not None
        self.mower_id = uuid4()
        self.authz = InMemoryAuthorizer(
            [
                RelationshipTuple(
                    user(self.principal.subject), "owner", account(self.principal.account_id)
                ),
                RelationshipTuple(
                    account(self.principal.account_id).as_subject(), "account", mower(self.mower_id)
                ),
            ]
        )
        self.metrics = CountingMetrics()
        self.hub = Hub(
            RealtimeConfig(**config),
            verifier=JwtTokenVerifier(self.keys.verifier()),
            access=AuthzAccessChecker(self.authz),
            metrics=self.metrics,
        )
        self.socket = SlowSocket()
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.session = self.hub.open(self.socket)
        self.task = asyncio.create_task(self.session.serve(self.inbox.get))

    async def start(self) -> None:
        await self.inbox.put(json.dumps({"type": "auth", "token": self.keys.mint(self.principal)}))
        await self.inbox.put(json.dumps({"type": "subscribe", "mower_ids": [str(self.mower_id)]}))
        await wait_for(lambda: any(m["type"] == "subscribed" for m in self.socket.sent))

    def state(self, battery: int) -> None:
        assert self.principal.account_id is not None
        self.hub.publish_state(
            make_state(self.mower_id, self.principal.account_id, battery=battery)
        )

    async def stop(self) -> None:
        await self.inbox.put(None)
        await asyncio.wait_for(self.task, 3)


async def test_realtime_slow_reader_gets_the_newest_state() -> None:
    setup = Setup()
    await setup.start()
    setup.socket.gate.clear()
    setup.socket.sending.clear()
    setup.state(10)
    await setup.socket.sending.wait()  # the first state is in flight, the client is busy
    setup.state(20)
    setup.state(30)  # replaces 20, which was never sent
    setup.socket.gate.set()
    await wait_for(lambda: sum(m["type"] == "state" for m in setup.socket.sent) == 2)
    batteries = [m["state"]["battery_percent"] for m in setup.socket.sent if m["type"] == "state"]
    assert batteries == [10, 30]
    assert setup.metrics.drops == ["coalesced"]
    await setup.stop()
    assert setup.metrics.codes == [None]  # the client went away, no close frame
    assert setup.hub.sessions == set()


async def test_realtime_send_timeout_closes_4430() -> None:
    setup = Setup(send_timeout_seconds=0.1)
    await setup.start()
    setup.socket.gate.clear()
    setup.state(10)
    await wait_for(lambda: setup.socket.closed is not None)
    assert setup.socket.closed == (RealtimeCloseCode.SLOW_CONSUMER, "slow consumer")
    await asyncio.wait_for(setup.task, 3)
    assert "slow_consumer" in setup.metrics.drops
    assert setup.hub.subscription_count == 0


async def test_realtime_full_queue_closes_4430_without_flushing() -> None:
    setup = Setup(queue_size=2, send_timeout_seconds=0.2)
    await setup.start()
    setup.socket.gate.clear()
    for _ in range(5):
        setup.hub.publish_result(
            CommandResult(
                command_id=uuid4(), mower_id=setup.mower_id, status=CommandStatus.ACCEPTED
            )
        )
    await wait_for(lambda: setup.socket.closed is not None)
    assert setup.socket.closed is not None
    assert setup.socket.closed[0] == RealtimeCloseCode.SLOW_CONSUMER
    await asyncio.wait_for(setup.task, 3)


async def test_realtime_without_token_configuration_every_socket_is_refused() -> None:
    hub = Hub(
        RealtimeConfig(),
        verifier=RejectAllTokens(),
        access=AuthzAccessChecker(InMemoryAuthorizer()),
    )
    socket = SlowSocket()
    session = hub.open(socket)
    inbox: asyncio.Queue[str | None] = asyncio.Queue()
    await inbox.put(json.dumps({"type": "auth", "token": "x"}))
    await asyncio.wait_for(session.serve(inbox.get), 3)
    assert socket.closed is not None
    assert socket.closed[0] == RealtimeCloseCode.UNAUTHORIZED


async def test_realtime_closing_hub_refuses_new_sessions() -> None:
    hub = Hub(
        RealtimeConfig(),
        verifier=RejectAllTokens(),
        access=AuthzAccessChecker(InMemoryAuthorizer()),
    )
    await hub.close_all(timeout=0.1)
    socket = SlowSocket()
    session = hub.open(socket)
    inbox: asyncio.Queue[str | None] = asyncio.Queue()
    await asyncio.wait_for(session.serve(inbox.get), 3)
    assert socket.closed == (RealtimeCloseCode.GOING_AWAY, "going away")


def frame(kind: str, n: int = 0) -> Frame:
    return Frame(kind, json.dumps({"type": kind, "n": n}))


async def test_realtime_outbox_order_and_discard() -> None:
    outbox = Outbox(queue_size=10)
    a, b = UUID(int=1), UUID(int=2)
    outbox.offer_state(a, frame("state", 1))
    outbox.offer_state(b, frame("state", 2))
    outbox.offer(frame("subscribed"))
    assert outbox.offer_state(a, frame("state", 3)) is True  # replaced, moved to the end
    outbox.discard_states([b])
    await outbox.wait()  # returns at once: something is pending
    assert outbox.pending() == 2
    assert [outbox.next_frame(), outbox.next_frame(), outbox.next_frame()] == [
        frame("subscribed"),
        frame("state", 3),
        None,
    ]
    outbox.close(CloseRequest(4401, "x"))
    outbox.close(CloseRequest(1001, "later"))  # the first request wins
    assert outbox.close_request == CloseRequest(4401, "x")
    assert outbox.offer(frame("pong")) is False
    assert outbox.offer_state(a, frame("state")) is False


def test_realtime_close_reason_fits_a_control_frame() -> None:
    assert close_reason("ä" * 100).encode("utf-8").__len__() <= 123
    assert close_reason("short") == "short"
