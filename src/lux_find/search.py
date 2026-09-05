"""Query the index: BM25 first, two small structural boosts on top.

The ranking is deliberately shallow and explainable:

1. FTS5 computes BM25 over the chunk text. That is the whole engine — real
   inverse document frequency, so an OR of the query terms is safe: a rare term
   dominates a common one instead of the query degrading into "any word wins".
2. A tiny boost if a query term appears in the document's own title or path. If
   you named a file after the thing, that is the strongest signal a human ever
   leaves behind.
3. A tiny boost for anchor filenames (README, CHANGELOG, index) because they are
   the entry point people are usually looking for.

Nothing else. No learned weights, no personalisation, no network. Pass
``--why`` to print the score breakdown for every hit and check the ranker's
work yourself.

``confidence`` reports the *separation* between the top hit and the runner-up.
Low confidence does not mean "no results"; it means "the index could not pick a
winner, refine the query".
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field

__all__ = ["Hit", "SearchOutcome", "find", "tokens", "confidence"]

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_ANCHOR_NAMES = {"readme", "index", "changelog", "contributing", "overview", "architecture"}

TITLE_BOOST = 0.6
ANCHOR_BOOST = 0.15
POOL = 500


@dataclass
class Hit:
    uri: str
    path: str
    title: str
    kind: str
    line: int
    snippet: str
    score: float
    mtime: float
    bm25: float = 0.0
    boosts: dict = field(default_factory=dict)
    chunk_id: int = 0
    """Which chunk matched. Internal; not part of the JSON contract."""

    def as_dict(self) -> dict:
        return {
            "uri": self.uri,
            "path": self.path,
            "title": self.title,
            "kind": self.kind,
            "line": self.line,
            "snippet": self.snippet,
            "score": round(self.score, 4),
            "bm25": round(self.bm25, 4),
            "boosts": {k: round(v, 4) for k, v in self.boosts.items()},
            "mtime": self.mtime,
        }


@dataclass
class SearchOutcome:
    query: str
    hits: list[Hit]
    ms: float
    confidence: str
    scanned: int
    collapsed: int = 0

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "count": len(self.hits),
            "ms": round(self.ms, 2),
            "confidence": self.confidence,
            "candidates": self.scanned,
            "collapsed": self.collapsed,
            "hits": [h.as_dict() for h in self.hits],
        }


def tokens(query: str, cap: int = 12) -> list[str]:
    """Words worth searching for, capped so one paste cannot become a scan.

    The query is normalised the same way indexed text is: typed on a Mac, a
    Portuguese word can arrive decomposed (an "a" plus a combining cedilla)
    and would never match the composed form stored in the index, with no hint
    that the two strings only looked identical.
    """
    normalised = unicodedata.normalize("NFC", query)
    found = [t.lower() for t in _WORD.findall(normalised) if len(t) > 1]
    return found[:cap]


def _match_expr(toks: list[str]) -> str:
    # OR is correct here *because* BM25 has IDF: a document matching only the
    # common word scores far below one matching the rare word. AND would make
    # every extra word a chance to return nothing.
    return " OR ".join(f'"{t}"' for t in toks)


def confidence(hits: list[Hit]) -> str:
    if not hits:
        return "none"
    if len(hits) == 1:
        return "high"
    top, second = hits[0].score, hits[1].score
    if top <= 0:
        return "low"
    separation = (top - second) / top
    if separation >= 0.25:
        return "high"
    if separation >= 0.08:
        return "medium"
    return "low"


def find(
    con: sqlite3.Connection,
    query: str,
    *,
    limit: int = 8,
    kinds: list[str] | None = None,
    pool: int = POOL,
) -> SearchOutcome:
    started = time.perf_counter()
    toks = tokens(query)
    if not toks:
        return SearchOutcome(query=query, hits=[], ms=0.0, confidence="none", scanned=0)

    # Both filters belong in SQL. Applied afterwards they competed for the
    # same 500 candidate slots as everything else, so a corpus with enough
    # matching notes could push every matching code file out of the pool and
    # answer `--kind code` with a confident zero.
    sql = (
        "SELECT f.rowid AS cid, bm25(chunk_fts, 1.0, 0.35) AS bm, "
        "snippet(chunk_fts, 0, '<<', '>>', ' ... ', 16) AS snip "
        "FROM chunk_fts f JOIN chunks c ON c.id = f.rowid "
        "JOIN docs d ON d.id = c.doc_id "
        "WHERE chunk_fts MATCH ? AND d.dup_of IS NULL"
    )
    params: list = [_match_expr(toks)]
    if kinds:
        sql += " AND d.kind IN (" + ",".join("?" * len(kinds)) + ")"
        params.extend(kinds)
    sql += " ORDER BY bm LIMIT ?"
    params.append(pool)

    rows = con.execute(sql, params).fetchall()

    if not rows:
        ms = (time.perf_counter() - started) * 1000
        return SearchOutcome(query=query, hits=[], ms=ms, confidence="none", scanned=0)

    ids = [r["cid"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    meta = {
        r["cid"]: r
        for r in con.execute(
            "SELECT c.id AS cid, c.doc_id, c.line, d.uri, d.source_path, d.title, "
            "d.kind, d.mtime FROM chunks c JOIN docs d ON d.id = c.doc_id "
            f"WHERE c.id IN ({placeholders})",
            ids,
        )
    }

    best: dict[int, Hit] = {}
    for row in rows:
        info = meta.get(row["cid"])
        if info is None:
            continue

        base = -float(row["bm"])  # bm25() returns a negative score; flip it
        boosts: dict[str, float] = {}

        haystack = f"{info['title']} {os.path.basename(info['source_path'])}".lower()
        matched = sum(1 for t in toks if t in haystack)
        if matched:
            boosts["title"] = TITLE_BOOST * (matched / len(toks))

        stem = os.path.splitext(os.path.basename(info["source_path"]))[0].lower()
        if stem in _ANCHOR_NAMES:
            boosts["anchor"] = ANCHOR_BOOST

        score = base + sum(boosts.values())
        hit = Hit(
            uri=info["uri"],
            path=info["source_path"],
            title=info["title"],
            kind=info["kind"],
            line=info["line"] or 1,
            snippet=(row["snip"] or "").strip(),
            score=score,
            mtime=info["mtime"] or 0.0,
            bm25=base,
            boosts=boosts,
            chunk_id=row["cid"],
        )
        current = best.get(info["doc_id"])
        if current is None or hit.score > current.score:
            best[info["doc_id"]] = hit

    ranked = sorted(best.values(), key=lambda h: (-h.score, -h.mtime, h.path))
    deduped = _dedup(ranked)
    collapsed = len(ranked) - len(deduped)
    ranked = deduped[:limit]
    _refine_lines(con, ranked, toks)

    ms = (time.perf_counter() - started) * 1000
    return SearchOutcome(
        query=query,
        hits=ranked,
        ms=ms,
        confidence=confidence(ranked),
        scanned=len(rows),
        collapsed=collapsed,
    )


def _refine_lines(con: sqlite3.Connection, hits: list[Hit], toks: list[str]) -> None:
    """Move each citation from the top of the chunk to the line that matched.

    A chunk is up to seventy lines of code or a whole markdown section, and the
    stored line number is where the chunk begins. Citing that sends the reader
    to the top of a window and asks them to search again by eye - exactly the
    work this tool exists to remove. The chunk body is fetched only for the
    handful of hits actually being shown.
    """
    if not hits or not toks:
        return
    ids = [h.chunk_id for h in hits if h.chunk_id]
    if not ids:
        return
    bodies = {
        row["id"]: row["body"]
        for row in con.execute(
            "SELECT id, body FROM chunks WHERE id IN (" + ",".join("?" * len(ids)) + ")",
            ids,
        )
    }
    for hit in hits:
        body = bodies.get(hit.chunk_id)
        if not body:
            continue
        for offset, line in enumerate(body.splitlines()):
            lowered = line.lower()
            if any(token in lowered for token in toks):
                hit.line = hit.line + offset
                break


def _dedup(hits: list[Hit]) -> list[Hit]:
    """Drop results a reader would experience as the same line twice.

    Backup folders, synced copies and snapshot directories produce byte-identical
    files under different paths. Document-level hashing already collapses exact
    copies at index time; this catches the rest, where two different documents
    happen to show the identical excerpt.
    """
    seen: set[tuple[str, int, str]] = set()
    out: list[Hit] = []
    for hit in hits:
        signature = (os.path.basename(hit.path), hit.line, hit.snippet)
        if signature in seen:
            continue
        seen.add(signature)
        out.append(hit)
    return out
