"""Isolated LightRAG 1.5.4 integration primitives.

The public application should import :mod:`services.lightrag_service` rather
than importing this package directly.  Keeping the third-party import behind
the adapter boundary lets the legacy engine and unit tests run without the
optional LightRAG distribution installed.
"""

from .errors import (
    LightRAGConfigurationError,
    LightRAGDisabledError,
    LightRAGError,
    LightRAGProtocolError,
    LightRAGUnavailableError,
    LightRAGValidationError,
)
from .types import Engine, LightRAGMode, TARGET_LIGHTRAG_VERSION

__all__ = [
    "Engine",
    "LightRAGMode",
    "TARGET_LIGHTRAG_VERSION",
    "LightRAGError",
    "LightRAGDisabledError",
    "LightRAGConfigurationError",
    "LightRAGUnavailableError",
    "LightRAGProtocolError",
    "LightRAGValidationError",
]
