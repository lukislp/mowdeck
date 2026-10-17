# SPDX-License-Identifier: Apache-2.0
"""agora-bridge: a camera source bridge for Agora channels (ADR 0045).

Speaks the camera relay's bridge protocol (docs/dev/camera-relay-bridge-protocol.md) on its
standard streams and joins one channel per process with Agora's Linux server SDK as a
receive-only member. The SDK is not part of this package: the operator's image build installs it
(see the README). Standard library only.
"""

__version__ = "0.1.0"
