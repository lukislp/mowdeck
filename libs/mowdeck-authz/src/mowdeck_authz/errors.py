"""Errors of mowdeck-authz.

Checks never raise these: a failing check is answered with ``False`` (fail closed). They are
raised by setup code (``connect()``, ``bootstrap_model()``) and by the tuple writer, whose
callers must know that a write did not happen.
"""


class AuthzError(Exception):
    """Base class of all mowdeck-authz errors."""


class AuthzUnavailableError(AuthzError):
    """OpenFGA could not be reached or the store or model could not be resolved."""


class StoreNotFoundError(AuthzUnavailableError):
    """No store with the configured name exists (run the ``authz-bootstrap`` job)."""


class ModelNotFoundError(AuthzUnavailableError):
    """The store has no authorization model (run the ``authz-bootstrap`` job)."""


class TupleOwnershipError(AuthzError, ValueError):
    """A writer tried to write a tuple that belongs to another service."""
