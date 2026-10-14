"""Domain layer of device-gateway: sharding, command admission, telemetry and backoff rules.

Pure Python only: no I/O, no FastAPI, no NATS. Everything here is testable without mocks.
"""
