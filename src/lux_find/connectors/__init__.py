"""Connector registry.

Add a connector by importing it here and mapping the source kinds it serves.
"""

from __future__ import annotations

from .base import Connector, DocRef
from .chat_jsonl import ChatConnector
from .files import FilesConnector

__all__ = ["Connector", "DocRef", "REGISTRY", "for_kind"]

REGISTRY: dict[str, Connector] = {
    "notes": FilesConnector(),
    "code": FilesConnector(),
    "chat": ChatConnector(),
}


def for_kind(kind: str) -> Connector:
    try:
        return REGISTRY[kind]
    except KeyError as exc:
        raise KeyError(
            f"no connector for kind {kind!r}; known kinds: {sorted(REGISTRY)}"
        ) from exc
