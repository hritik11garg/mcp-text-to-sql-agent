"""FastAPI layer. Thin by design -- no business logic lives here.

Every component this exposes is testable without HTTP, and is tested that way.
What lives here is the part that only exists because there is a network: the
error envelope, request correlation, the probes, and the closed-by-default
configuration that decides who can reach any of it.

Contract: docs/architecture/API.md.
"""

from api.app import create_app
from api.errors import ApiError, ErrorCode

__all__ = ["ApiError", "ErrorCode", "create_app"]
