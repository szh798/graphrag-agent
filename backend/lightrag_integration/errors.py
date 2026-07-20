"""Typed, privacy-safe LightRAG integration failures."""

from __future__ import annotations


class LightRAGError(RuntimeError):
    """Base class that callers can translate into a stable API error."""

    code = "lightrag_error"


class LightRAGDisabledError(LightRAGError):
    code = "lightrag_disabled"


class LightRAGConfigurationError(LightRAGError):
    code = "lightrag_misconfigured"


class LightRAGUnavailableError(LightRAGError):
    code = "lightrag_unavailable"


class LightRAGProtocolError(LightRAGError):
    code = "lightrag_protocol_error"


class LightRAGValidationError(LightRAGError, ValueError):
    code = "lightrag_invalid_request"


class LightRAGAuthenticationError(LightRAGError):
    code = "lightrag_unauthorized"
