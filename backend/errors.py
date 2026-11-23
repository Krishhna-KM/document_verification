"""Every failure the API reports on purpose. Anything else is an unexpected 500.

Each error carries an HTTP status, a stable machine-readable `code`, a message that is safe to show to
API callers, and optional `details` (upstream response bodies etc.) that are only logged / returned in DEBUG.
"""
from typing import Optional


class ApiError(Exception):
    status = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None,
                 details: Optional[str] = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.details = details


# ---- the caller did something wrong (4xx) ---------------------------------------------------------
class RequestError(ApiError):
    status, code = 400, "BAD_REQUEST"


class DocumentError(ApiError):
    """The uploaded file is not a usable image / PDF."""
    status, code = 422, "INVALID_DOCUMENT"


# ---- the model service failed (5xx) ---------------------------------------------------------------
class ModelError(ApiError):
    status, code = 502, "MODEL_ERROR"


class ModelUnavailable(ModelError):
    code = "MODEL_UNAVAILABLE"


class ModelTimeout(ModelError):
    status, code = 504, "MODEL_TIMEOUT"


class ModelAuthError(ModelError):
    code = "MODEL_AUTH_FAILED"


class ModelRejected(ModelError):
    code = "MODEL_REJECTED_REQUEST"


class ModelBadResponse(ModelError):
    code = "MODEL_BAD_RESPONSE"


class ExtractionError(ApiError):
    """The model answered, but not with usable data."""
    status, code = 502, "EXTRACTION_FAILED"


# ---- our own setup is wrong -----------------------------------------------------------------------
class ServerConfigError(ApiError):
    status, code = 500, "SERVER_MISCONFIGURED"
