"""MowDeck authorization: one question, "may this subject do this on that object?".

Services ask through an :class:`Authorizer` (``OpenFgaAuthorizer`` in production,
``mowdeck_authz.testing.InMemoryAuthorizer`` in tests). Identity and Fleet write tuples through a
:class:`TupleWriter`. FastAPI helpers live in ``mowdeck_authz.fastapi`` (extra ``fastapi``).
"""

from mowdeck_authz._client import FgaClient, create_client
from mowdeck_authz.authorizer import (
    Authorizer,
    CheckItem,
    DecisionCache,
    SupportsPing,
    readiness_check_authz,
)
from mowdeck_authz.bootstrap import BootstrapResult, bootstrap_model, find_store_id
from mowdeck_authz.errors import (
    AuthzError,
    AuthzUnavailableError,
    ModelNotFoundError,
    StoreNotFoundError,
    TupleOwnershipError,
)
from mowdeck_authz.model import load_model, models_equal, normalize_model
from mowdeck_authz.openfga import FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.permissions import (
    GRANT_RELATIONS,
    PERMISSION_RELATIONS,
    PermissionRelations,
    Relation,
    grant_relation,
)
from mowdeck_authz.refs import (
    PLATFORM_OBJECT,
    ObjectRef,
    Subject,
    account,
    api_token,
    group,
    mower,
    principal_subject,
    site,
    user,
)
from mowdeck_authz.settings import AuthzSettings
from mowdeck_authz.tuples import RelationshipTuple, TupleCondition, not_expired, rfc3339
from mowdeck_authz.writer import (
    OpenFgaTupleBackend,
    TupleBackend,
    TupleChange,
    TupleWriter,
    tuple_owner,
)

__version__ = "0.1.0"

__all__ = [
    "GRANT_RELATIONS",
    "PERMISSION_RELATIONS",
    "PLATFORM_OBJECT",
    "Authorizer",
    "AuthzError",
    "AuthzSettings",
    "AuthzUnavailableError",
    "BootstrapResult",
    "CheckItem",
    "DecisionCache",
    "FgaClient",
    "FgaConnection",
    "ModelNotFoundError",
    "ObjectRef",
    "OpenFgaAuthorizer",
    "OpenFgaTupleBackend",
    "PermissionRelations",
    "Relation",
    "RelationshipTuple",
    "StoreNotFoundError",
    "Subject",
    "SupportsPing",
    "TupleBackend",
    "TupleChange",
    "TupleCondition",
    "TupleOwnershipError",
    "TupleWriter",
    "account",
    "api_token",
    "bootstrap_model",
    "create_client",
    "find_store_id",
    "grant_relation",
    "group",
    "load_model",
    "models_equal",
    "mower",
    "normalize_model",
    "not_expired",
    "principal_subject",
    "readiness_check_authz",
    "rfc3339",
    "site",
    "tuple_owner",
    "user",
]
