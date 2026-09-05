"""The connector contract.

A connector answers two questions about one configured source:

1. ``scan()`` — *what documents exist, and has each one changed?* This must be
   cheap: it is called on every index run, over every file, and it decides what
   gets skipped. It returns :class:`DocRef` objects carrying a ``signature``
   (any string; change the string, and the document is re-read).
2. ``load()`` — *give me the chunks of this one document.* Called only for refs
   whose signature differs from what the index already has.

Splitting the two is what makes incremental indexing possible without hashing
gigabytes on every run.

Writing your own connector is roughly thirty lines: subclass :class:`Connector`,
yield ``DocRef``s from ``scan``, return ``(line, text)`` pairs from ``load``, and
register it in ``connectors/__init__.py``. See ``docs/CONNECTORS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

__all__ = ["DocRef", "Connector", "ScanReport"]


@dataclass
class ScanReport:
    """What a scan could not read, and what it deliberately left out.

    A scan that quietly returns fewer documents than the disk holds is the one
    failure this tool refuses to have: an empty result then reads as a verdict.
    ``errors`` are real failures (an unreadable directory, a file that vanished)
    and make the build incomplete; ``skipped`` counts the deliberate policy
    exclusions - a credential-named file, a binary, something over the size cap -
    which are reported in the summary rather than treated as failures.
    """

    errors: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.errors.append(message)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


@dataclass
class DocRef:
    """One addressable document inside a source."""

    uri: str
    """Stable identifier. For files this is the absolute path; for a container
    format it is ``<path>#<inner-id>``. Re-indexing keys on this."""

    source_path: str
    """The file on disk that backs this document (used for prune + display)."""

    title: str
    kind: str
    """``notes`` | ``code`` | ``chat`` — what the ranker and ``--kind`` filter see."""

    signature: str
    """Opaque change token. Same signature means "skip, already indexed"."""

    mtime: float = 0.0
    size: int = 0
    chunker: str = "plain"
    """``markdown`` | ``code`` | ``plain`` — how ``load`` output should be split
    when the connector hands back raw text instead of chunks."""

    extra: dict = field(default_factory=dict)


class Connector:
    """Base class. Subclasses implement :meth:`scan` and :meth:`load`."""

    name = "base"

    def scan(self, source, report: ScanReport | None = None) -> Iterator[DocRef]:
        """Yield the documents in ``source``.

        ``report`` collects what could not be read and what was deliberately
        skipped. It is optional so a connector can be driven directly in a
        test, but the indexer always passes one.
        """
        raise NotImplementedError

    def load(self, ref: DocRef) -> list[tuple[int, str]]:
        """Return ``(line_number, text)`` chunks for one document."""
        raise NotImplementedError
