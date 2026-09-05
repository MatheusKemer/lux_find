"""Turn a document into retrievable chunks.

A chunk is the unit the ranker scores and the unit a human reads. Whole files
are too coarse (a 900-line file matches everything and tells you nothing) and
single lines are too fine (no context around the hit). The two strategies here
are deliberately boring:

* ``chunk_markdown`` splits on headings, because a heading is an author-written
  statement about where a topic starts. Long sections are then hard-wrapped with
  a few lines of overlap so a paragraph straddling the cut is still findable.
* ``chunk_code`` slides a fixed window with overlap, because code has no
  reliable topical boundary that is cheap to detect across languages.

Both return ``(line_number, text)`` pairs. The line number is what lets a result
say ``notes/design.md:142`` instead of just naming the file.
"""

from __future__ import annotations

import re

__all__ = ["chunk_markdown", "chunk_code", "chunk_plain", "chunk_for_kind"]

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_SETEXT = re.compile(r"^\s{0,3}(=+|-{2,})\s*$")

MAX_CHUNK_CHARS = 1800
# A hard ceiling on any single chunk, independent of line structure. One
# unbroken line - a minified bundle, a generated JSON blob, a log line - is a
# whole "section" to a line-oriented splitter, and handing FTS5 a multi-megabyte
# chunk makes snippet() spend minutes on a query that should take milliseconds.
MAX_ANY_CHUNK_CHARS = 4000
OVERLAP_LINES = 3
MIN_SECTION_CHARS = 200

CODE_WINDOW = 70
CODE_OVERLAP = 12
MAX_CODE_CHARS = 4000


def _flush(buf: list[str], start_line: int, out: list[tuple[int, str]]) -> None:
    """Emit ``buf`` as one or more chunks, wrapping oversized sections.

    Every chunk carries the line its first line actually sits on. The previous
    version rebuilt that number from a running offset and an overlap tail, and
    got it wrong whenever a section wrapped: several chunks of the same section
    all claimed line 1, so a result pointed at the top of the file instead of
    at the paragraph the reader wanted. Walking an explicit index cannot drift.
    """
    text = "\n".join(buf).strip()
    if not text:
        return
    if len(text) <= MAX_CHUNK_CHARS:
        out.append((start_line, text))
        return


    total = len(buf)
    index = 0
    while index < total:
        piece: list[str] = []
        size = 0
        cursor = index
        # At least one line per chunk, even a line longer than the whole budget:
        # otherwise a minified file would loop forever making empty chunks.
        while cursor < total and (not piece or size < MAX_CHUNK_CHARS):
            piece.append(buf[cursor])
            size += len(buf[cursor]) + 1
            cursor += 1
        body = "\n".join(piece).strip()
        for part in _slice(body):
            # An oversized single line is emitted in pieces, every one carrying
            # the line it started on, because that is where a reader has to go.
            out.append((start_line + index, part))
        if cursor >= total:
            break
        index = max(cursor - OVERLAP_LINES, index + 1)


def _slice(body: str) -> list[str]:
    """Cut a body down to something neither a ranker nor a reader chokes on."""
    if not body:
        return []
    if len(body) <= MAX_ANY_CHUNK_CHARS:
        return [body]
    return [
        body[at:at + MAX_ANY_CHUNK_CHARS]
        for at in range(0, len(body), MAX_ANY_CHUNK_CHARS)
    ]


def chunk_markdown(text: str) -> list[tuple[int, str]]:
    """Split on headings, then hard-wrap long sections with overlap."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    lines = text.splitlines()
    in_fence = False
    for number, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        is_break = not in_fence and (
            _HEADING.match(line)
            or (_SETEXT.match(line) and buf and buf[-1].strip())
        )
        if is_break and sum(len(x) for x in buf) > MIN_SECTION_CHARS:
            _flush(buf, start, out)
            buf, start = [line], number
        else:
            buf.append(line)
    _flush(buf, start, out)
    return out


def chunk_code(
    text: str, window: int = CODE_WINDOW, overlap: int = CODE_OVERLAP
) -> list[tuple[int, str]]:
    """Sliding window over source lines."""
    if window <= overlap:
        raise ValueError("window must be larger than overlap")
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        segment = "\n".join(lines[i : i + window]).strip()
        if segment:
            out.append((i + 1, segment[:MAX_CODE_CHARS]))
        i += window - overlap
    return out


def chunk_plain(text: str) -> list[tuple[int, str]]:
    """Plain text: wrap by size, keep line numbers honest."""
    out: list[tuple[int, str]] = []
    _flush(text.splitlines(), 1, out)
    return out


def chunk_for_kind(text: str, chunker: str) -> list[tuple[int, str]]:
    if chunker == "markdown":
        return chunk_markdown(text)
    if chunker == "code":
        return chunk_code(text)
    return chunk_plain(text)
