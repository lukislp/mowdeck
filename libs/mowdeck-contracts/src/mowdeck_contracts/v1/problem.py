"""RFC 9457 problem details, the error format of every MowDeck HTTP API."""

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"
"""Media type of problem detail responses."""

PROBLEM_TYPE_PREFIX = "urn:mowdeck:problem:"
"""Prefix of MowDeck problem type URIs, e.g. ``urn:mowdeck:problem:not-found``."""


class ProblemFieldError(ContractModel):
    """One validation error inside :attr:`ProblemDetails.errors`."""

    detail: str = Field(description="Human readable message.")
    pointer: str | None = Field(
        default=None, description="JSON Pointer (RFC 6901) into the request body, e.g. `#/name`."
    )
    parameter: str | None = Field(
        default=None, description="Name of the offending query, path, header or cookie parameter."
    )
    code: str | None = Field(default=None, description="Machine readable error code.")


class ProblemDetails(ContractModel):
    """An RFC 9457 problem document with MowDeck extensions ``correlation_id`` and ``errors``."""

    type: str = Field(default="about:blank", description="URI reference identifying the type.")
    title: str = Field(description="Short, human readable summary of the problem type.")
    status: int = Field(ge=100, le=599, description="HTTP status code.")
    detail: str | None = Field(default=None, description="Explanation of this occurrence.")
    instance: str | None = Field(default=None, description="URI reference of this occurrence.")
    correlation_id: str | None = Field(
        default=None, description="Correlation id of the request, for support and log search."
    )
    errors: list[ProblemFieldError] | None = Field(
        default=None, description="Individual validation errors, if any."
    )
