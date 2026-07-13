"""Domain errors surfaced to the API layer as OpenAI-style error responses."""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for recoverable provider failures."""

    status_code: int = 502
    error_type: str = "provider_error"


class ProviderTimeout(ProviderError):
    status_code = 504
    error_type = "timeout"


class AuthenticationRequired(ProviderError):
    """The underlying chat UI is not logged in / session expired."""

    status_code = 502
    error_type = "upstream_authentication_error"


class ProviderUnavailable(ProviderError):
    status_code = 503
    error_type = "provider_unavailable"
