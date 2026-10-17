"""Domain layer of camera-relay: sessions, leases, limits and tickets (ADR 0045).

Pure Python only: no I/O, no FastAPI, no NATS, no media. The aggregate
:class:`~camera_relay.domain.session.RelaySession` takes the current time as an argument and
answers every input with a list of effects (:mod:`camera_relay.domain.effects`) that the
application layer carries out. Nothing here names a manufacturer: a source is a
``CameraProvider``.
"""
