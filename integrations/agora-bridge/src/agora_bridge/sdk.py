# SPDX-License-Identifier: Apache-2.0
"""The seam between the bridge and a vendor SDK.

:class:`Sdk` is what the bridge calls (always from its main thread); :class:`SdkListener` is
what the SDK adapter calls back, from the SDK's own threads. The real adapter is
:mod:`agora_bridge.agora_sdk`; the tests use a fake.
"""

from typing import Protocol

from agora_bridge.protocol import Join


class SdkError(Exception):
    """An SDK call failed (the message never holds a secret)."""


class SdkLoadError(SdkError):
    """The SDK could not be loaded (exit code 3)."""


class SdkListener(Protocol):
    """Callbacks of the SDK adapter. They may run on any thread and must not block."""

    def connected(self) -> None:
        """The connection to the channel is up."""
        ...

    def connection_failed(self, detail: str) -> None:
        """The connection failed for good (before or after it was up)."""
        ...

    def user_joined(self, uid: int) -> None:
        """A remote user (a camera) joined the channel."""
        ...

    def user_left(self, uid: int) -> None:
        """A remote user left the channel."""
        ...

    def video_state(self, uid: int, state: str) -> None:
        """The SDK's view of a remote video stream: starting, decoding, frozen or stopped."""
        ...

    def token_will_expire(self) -> None:
        """The token runs out soon."""
        ...

    def frame(self, uid: int, codec: int, keyframe: bool, capture_ms: int, data: bytes) -> None:
        """One encoded frame (``codec`` is the protocol's codec number, 0 if unsupported)."""
        ...

    def warning(self, code: str, detail: str) -> None:
        """Something worth an ``error`` event that is not fatal."""
        ...


class Sdk(Protocol):
    """A loaded SDK that joins one channel as a receive-only member."""

    version: str
    """SDK name and version for the ``hello`` event."""

    def join(self, join: Join, listener: SdkListener) -> None:
        """Start joining; raises :class:`SdkError` when the call is refused."""
        ...

    def subscribe(self, uid: int) -> int:
        """Subscribe to ``uid``'s encoded video; 0 on success."""
        ...

    def keyframe(self, uid: int) -> int:
        """Ask ``uid`` for a key frame; 0 when the request was sent."""
        ...

    def renew(self, token: str) -> int:
        """Renew the token on the running connection; 0 on success."""
        ...

    def leave(self) -> None:
        """Leave the channel and release the SDK. Idempotent."""
        ...
