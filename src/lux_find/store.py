"""SQLite FTS5 index: build, prune, status.

Why SQLite and not a vector database: the whole point is that a stranger can
clone this, run one command, and search their own files five minutes later. FTS5
ships inside Python's standard library, the index is a single file you can copy
or delete, BM25 is debuggable, and the whole thing answers in milliseconds on a
laptop. Embeddings are a good *second* opinion; they are a bad first dependency.

Three behaviours here are load-bearing:

* **Incremental by signature.** A document is re-read only when its
  ``mtime:size`` changes. Re-chunking an unchanged corpus is the difference
  between a 15-second refresh and a two-minute one.
* **Prune guard.** If a source suddenly scans to zero documents while the index
  holds many for that same root, that is far more likely to be a permissions
  problem or a mistyped path than a deletion of every file. The build refuses to
  prune and raises; ``--force-prune`` is the explicit override.
* **Typed errors.** A corrupt or missing index raises a typed error that the CLI
  turns into a distinct exit code with the database path printed, instead of
  returning an empty result list.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import connectors
from .config import Config
from .connectors.base import ScanReport

__all__ = [
    "LuxFindError", "IndexMissing", "IndexCorrupt", "IndexBusy", "PruneGuardTripped",
    "open_db", "build", "status", "force_prune", "BuildStats", "SCHEMA_VERSION",
]

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS docs(
  id           INTEGER PRIMARY KEY,
  uri          TEXT UNIQUE NOT NULL,
  source_root  TEXT NOT NULL,
  source_path  TEXT NOT NULL,
  title        TEXT,
  kind         TEXT,
  connector    TEXT,
  signature    TEXT,
  content_hash TEXT,
  dup_of       TEXT,
  mtime        REAL,
  size         INTEGER,
  indexed_at   REAL
);
CREATE INDEX IF NOT EXISTS docs_kind ON docs(kind);
CREATE INDEX IF NOT EXISTS docs_root ON docs(source_root);
CREATE INDEX IF NOT EXISTS docs_hash ON docs(content_hash);

CREATE TABLE IF NOT EXISTS chunks(
  id     INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL,
  line   INTEGER,
  body   TEXT,
  title  TEXT
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);

-- Two columns, deliberately. A query matches the text *or* the name of the
-- file it lives in, so a document named after its topic is retrievable by that
-- name - but snippets and line numbers are computed over ``body`` alone, so the
-- name never leaks into what the reader sees or shifts the line they are sent to.
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  body,
  title,
  content='chunks',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
"""


class LuxFindError(Exception):
    """Base class for index problems the CLI reports with a dedicated exit code."""


class IndexMissing(LuxFindError):
    pass


class IndexCorrupt(LuxFindError):
    pass


class PruneGuardTripped(LuxFindError):
    pass


class IndexBusy(LuxFindError):
    """Another process holds the write lock. The index is fine; try again."""


# ------------------------------------------------------------------ connection

def _harden(path: Path) -> None:
    """Keep the index readable only by its owner.

    The database holds the plain text of every file you pointed a source at.
    Created with the default umask it lands 0644, which hands the whole corpus
    to any other account on the machine. Tightening is best effort: filesystems
    without POSIX permissions (FAT, some network mounts, Windows) simply cannot
    express this, and that is not a reason to refuse to build an index.
    """
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            if candidate.exists():
                os.chmod(candidate, 0o600)
        except OSError as exc:  # noqa: PERF203 - reported, never swallowed
            print(f"lux-find: could not restrict permissions on {candidate}: {exc}",
                  file=__import__("sys").stderr)


def open_db(path: Path, *, create: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if not create and not path.exists():
        raise IndexMissing(f"no index at {path}")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Create it private from the very first byte rather than
            # widening a window where the file exists world-readable.
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    try:
        con = sqlite3.connect(str(path))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        if create:
            existing = _schema_version_of(con)
            con.executescript(SCHEMA)
            if existing == 0:
                # Brand new index. Stamping an index that already existed would
                # overwrite the one fact that says it needs rebuilding.
                con.execute(
                    "INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
            con.commit()
            _harden(path)
        else:
            # Touch the FTS table: a truncated or non-SQLite file fails here,
            # not silently three queries later.
            con.execute("SELECT count(*) FROM chunk_fts").fetchone()
            _check_schema(con, path)
    except sqlite3.OperationalError as exc:
        # "database is locked" is a healthy index with a writer in front of it.
        # Reporting that as corruption sends the user to delete the file.
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise IndexBusy(
                f"index at {path} is locked by another lux-find run; try again in a moment"
            ) from exc
        raise IndexCorrupt(f"index at {path} is unreadable: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        raise IndexCorrupt(f"index at {path} is unreadable: {exc}") from exc
    return con


def _schema_version_of(con: sqlite3.Connection) -> int:
    """The schema version recorded in the index, or 0 when it predates the field."""
    try:
        row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return 0
    try:
        return int(row["v"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _schema_is_current(con: sqlite3.Connection) -> bool:
    """Is this index the shape this build knows how to write?

    Both halves matter. The recorded version catches a deliberate change; the
    table shape catches an index whose version was stamped by mistake, and it is
    the half that cannot be wrong.
    """
    if _schema_version_of(con) != SCHEMA_VERSION:
        return False
    try:
        columns = {row["name"] for row in con.execute("PRAGMA table_info(chunks)")}
    except sqlite3.DatabaseError:
        return False
    return {"id", "doc_id", "line", "body", "title"} <= columns


def _reset_schema(con: sqlite3.Connection) -> None:
    """Remove the derived tables and lay the current schema down again."""
    for table in ("chunk_fts", "chunks", "docs"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    con.executescript(SCHEMA)
    con.execute(
        "INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )
    con.commit()


def _check_schema(con: sqlite3.Connection, path: Path) -> None:
    """Refuse an index written by a different schema, loudly.

    The version was written into ``meta`` from the first release and never read
    back, so an index from another version failed later with a raw sqlite error
    and the exit code for "no hits" - a broken index answering like an empty one.
    """
    row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    found = row["v"] if row else "0"
    if str(found) != str(SCHEMA_VERSION):
        raise IndexCorrupt(
            f"index at {path} was built by schema version {found}, this build needs "
            f"{SCHEMA_VERSION}. Rebuild it with `lux-find index --full`."
        )


# ----------------------------------------------------------------------- build

@dataclass
class BuildStats:
    sources: int = 0
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    duplicates: int = 0
    pruned: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    """Deliberate policy exclusions by reason (credential-named, over-size-limit,
    binary, empty). Not failures, but never invisible either: a document that
    silently disappears from an index is indistinguishable from one that was
    never there."""
    seconds: float = 0.0

    @property
    def complete(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "sources": self.sources,
            "scanned": self.scanned,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "duplicates": self.duplicates,
            "pruned": self.pruned,
            "chunks": self.chunks,
            "errors": self.errors,
            "excluded": self.excluded,
            "seconds": round(self.seconds, 3),
            "complete": self.complete,
        }


def _drop_doc_chunks(con: sqlite3.Connection, doc_id: int) -> None:
    """Remove a document's chunks from both the table and the FTS shadow.

    With an external-content FTS5 table the delete has to be announced with the
    original body, otherwise the term index keeps pointing at rows that no
    longer exist and stale text stays searchable forever.
    """
    rows = con.execute(
        "SELECT id, body, title FROM chunks WHERE doc_id=?", (doc_id,)
    ).fetchall()
    for row in rows:
        con.execute(
            "INSERT INTO chunk_fts(chunk_fts, rowid, body, title) "
            "VALUES('delete', ?, ?, ?)",
            (row["id"], row["body"], row["title"]),
        )
    con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))


def _title_words(title: str) -> str:
    """The words a human put in the file's own name, as searchable text.

    A title boost can only re-rank chunks that already matched, so a file named
    after the thing was unfindable by that name unless the name also appeared
    inside the text. Folding the name into the first chunk makes it retrievable
    without giving it a tiny high-scoring document of its own.
    """
    stem = title.rsplit(".", 1)[0] if "." in os.path.basename(title) else title
    words = re.split(r"[^\w]+", stem, flags=re.UNICODE)
    return " ".join(w for w in words if len(w) > 1)


def _insert_chunks(
    con: sqlite3.Connection,
    doc_id: int,
    chunks: list[tuple[int, str]],
    title: str = "",
) -> int:
    written = 0
    name = _title_words(title)
    for line, body in chunks:
        if not body.strip():
            continue
        cursor = con.execute(
            "INSERT INTO chunks(doc_id, line, body, title) VALUES(?,?,?,?)",
            (doc_id, line, body, name),
        )
        con.execute(
            "INSERT INTO chunk_fts(rowid, body, title) VALUES(?,?,?)",
            (cursor.lastrowid, body, name),
        )
        written += 1
    return written


def build(
    config: Config,
    *,
    full: bool = False,
    progress: Callable[[str], None] | None = None,
) -> BuildStats:
    """Bring the index up to date with the configured sources."""
    stats = BuildStats(sources=len(config.sources))
    started = time.time()
    con = open_db(config.db_path, create=True)
    # An index written by an older build has an older shape, and CREATE TABLE
    # IF NOT EXISTS will not reshape it. `find` tells the user to rebuild with
    # --full; the rebuild command itself must not be the thing that crashes, so
    # it simply does the rebuild. The index is a derived cache - every document
    # in it is still on disk, so remaking it costs time and nothing else.
    if not _schema_is_current(con):
        if progress:
            progress("index was built by an older version - rebuilding from scratch")
        _reset_schema(con)
        full = True
    try:
        _build_into(con, config, stats, full=full, progress=progress)
    except BaseException:
        # Whatever went wrong - a tripped prune guard, Ctrl-C, a disk error -
        # the uncommitted tail is discarded and the connection is released.
        # Work already checkpointed during the run stays.
        try:
            con.rollback()
        finally:
            con.close()
        raise
    else:
        con.close()
    _harden(config.db_path)
    stats.seconds = time.time() - started
    return stats


def _build_into(con, config: Config, stats: BuildStats, *, full: bool, progress) -> None:

    if full:
        # executescript() commits before it runs, which published an empty
        # index to every concurrent reader for the whole rebuild. Kept inside
        # the transaction, a --full rebuild is atomic: readers see the old
        # index until the new one is complete.
        con.execute("DELETE FROM chunk_fts")
        con.execute("DELETE FROM chunks")
        con.execute("DELETE FROM docs")

    existing = {
        row["uri"]: (row["id"], row["signature"])
        for row in con.execute("SELECT id, uri, signature FROM docs")
    }
    hash_owner = {
        row["content_hash"]: row["uri"]
        for row in con.execute(
            "SELECT content_hash, uri FROM docs WHERE dup_of IS NULL AND content_hash IS NOT NULL"
        )
    }

    seen: set[str] = set()
    # uri -> (ref, connector) for everything this run saw, so a duplicate whose
    # owner just disappeared can be promoted without waiting for another run.
    live_refs: dict = {}
    since_commit = 0

    for source in config.sources:
        root = str(source.root)
        connector = connectors.for_kind(source.kind)
        scanned_here = 0
        report = ScanReport()
        try:
            refs = list(connector.scan(source, report))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            stats.errors.append(f"{root}: scan failed: {exc}")
            if progress:
                progress(f"ERROR scanning {root}: {exc}")
            continue
        finally:
            # An unreadable subdirectory is an error even when the scan itself
            # completed: without this the documents under it are pruned as
            # "gone" and the run still exits 0.
            stats.errors.extend(report.errors)
            for reason, count in report.skipped.items():
                stats.excluded[reason] = stats.excluded.get(reason, 0) + count
            for message in report.errors:
                if progress:
                    progress(f"ERROR {message}")

        # Deterministic order, shortest path first. When two files hold identical
        # content the first one seen wins the chunks, so "notes/design.md" should
        # beat "notes/design (copy).md" - and the same run must produce the same
        # index twice.
        refs.sort(key=lambda r: (len(r.source_path), r.source_path))

        if progress:
            progress(f"source {root} [{source.kind}] -> {len(refs)} documents")

        for ref in refs:
            scanned_here += 1
            stats.scanned += 1
            seen.add(ref.uri)
            live_refs[ref.uri] = (ref, connector)

            known = existing.get(ref.uri)
            if known and not full and known[1] == ref.signature:
                stats.skipped += 1
                continue

            try:
                chunks = connector.load(ref)
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{ref.uri}: load failed: {exc}")
                if progress:
                    progress(f"ERROR loading {ref.uri}: {exc}")
                continue

            if not chunks:
                # Nothing indexable came back (a binary that sniffed as text, a
                # transcript with no messages). Record it, and if the document
                # was in the index before, take the stale chunks out instead of
                # leaving last week's text searchable forever.
                stats.excluded["no-indexable-text"] = (
                    stats.excluded.get("no-indexable-text", 0) + 1
                )
                if known:
                    _drop_doc_chunks(con, known[0])
                    con.execute(
                        "UPDATE docs SET signature=?, content_hash=NULL, dup_of=NULL,"
                        " mtime=?, size=?, indexed_at=? WHERE id=?",
                        (ref.signature, ref.mtime, ref.size, time.time(), known[0]),
                    )
                    existing[ref.uri] = (known[0], ref.signature)
                continue

            digest = hashlib.sha256(
                "\n".join(b for _, b in chunks).encode("utf-8", "replace")
            ).hexdigest()

            owner = hash_owner.get(digest)
            is_dup = owner is not None and owner != ref.uri

            if known:
                doc_id = known[0]
                _drop_doc_chunks(con, doc_id)
                con.execute(
                    "UPDATE docs SET source_root=?, source_path=?, title=?, kind=?, "
                    "connector=?, signature=?, content_hash=?, dup_of=?, "
                    "mtime=?, size=?, indexed_at=? WHERE id=?",
                    (root, ref.source_path, ref.title, ref.kind, connector.name,
                     ref.signature, digest, owner if is_dup else None,
                     ref.mtime, ref.size, time.time(), doc_id),
                )
            else:
                cursor = con.execute(
                    "INSERT INTO docs(uri, source_root, source_path, title, kind, connector,"
                    " signature, content_hash, dup_of, mtime, size, indexed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ref.uri, root, ref.source_path, ref.title, ref.kind, connector.name,
                     ref.signature, digest, owner if is_dup else None,
                     ref.mtime, ref.size, time.time()),
                )
                doc_id = cursor.lastrowid
                existing[ref.uri] = (doc_id, ref.signature)

            if is_dup:
                stats.duplicates += 1
            else:
                hash_owner.setdefault(digest, ref.uri)
                stats.chunks += _insert_chunks(con, doc_id, chunks, ref.title)
                stats.indexed += 1

            since_commit += 1
            if since_commit >= 500 and not full:
                # Checkpoint. An interrupted build should cost the last few
                # hundred documents, not the whole run.
                con.commit()
                since_commit = 0

            if progress and stats.indexed and stats.indexed % 200 == 0:
                progress(
                    f"indexed {stats.indexed} documents "
                    f"({stats.skipped} unchanged, {stats.chunks} chunks)"
                )

        # Prune guard, per source root.
        held = con.execute(
            "SELECT count(*) AS n FROM docs WHERE source_root=?", (root,)
        ).fetchone()["n"]
        if scanned_here == 0 and held > 0:
            raise PruneGuardTripped(
                f"source {root} scanned 0 documents but the index holds {held}. "
                "Refusing to prune: this is usually an unreadable path or a bad "
                "include pattern, not a deletion. Fix the source, or pass "
                "--force-prune if the files really are gone."
            )

    # Prune documents that no longer exist anywhere.
    if not stats.errors:
        stale = [
            row["id"]
            for row in con.execute("SELECT id, uri FROM docs")
            if row["uri"] not in seen
        ]
        for doc_id in stale:
            _drop_doc_chunks(con, doc_id)
            con.execute("DELETE FROM docs WHERE id=?", (doc_id,))
        stats.pruned = len(stale)

        # A document marked as a duplicate carries no chunks of its own: the
        # copy it pointed at held them. If that owner was just pruned, the
        # survivor would be a file on disk with unique content and zero hits -
        # the worst shape this index can take. Promote it now.
        orphans = [
            row["uri"]
            for row in con.execute(
                "SELECT uri FROM docs WHERE dup_of IS NOT NULL AND dup_of NOT IN"
                " (SELECT uri FROM docs)"
            )
        ]
        for uri in orphans:
            entry = live_refs.get(uri)
            row = con.execute("SELECT id FROM docs WHERE uri=?", (uri,)).fetchone()
            if row is None:
                continue
            con.execute("UPDATE docs SET dup_of=NULL WHERE id=?", (row["id"],))
            if entry is None:
                # Not seen this run (its source was not scanned). Clear the
                # signature so the next run is forced to re-read it.
                con.execute("UPDATE docs SET signature='' WHERE id=?", (row["id"],))
                continue
            ref, connector = entry
            try:
                chunks = connector.load(ref)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                stats.errors.append(f"{uri}: reload after de-duplication failed: {exc}")
                continue
            _drop_doc_chunks(con, row["id"])
            stats.chunks += _insert_chunks(con, row["id"], chunks, ref.title)
            stats.duplicates = max(0, stats.duplicates - 1)
            stats.indexed += 1

    con.execute(
        "INSERT OR REPLACE INTO meta(k,v) VALUES('built_at',?)", (str(int(time.time())),)
    )
    con.execute(
        "INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version',?)", (str(SCHEMA_VERSION),)
    )
    con.commit()
    if full:
        con.execute("VACUUM")


def force_prune(config: Config) -> int:
    """Delete every document whose backing file is gone. Explicit, never implicit."""
    con = open_db(config.db_path)
    removed = 0
    for row in con.execute("SELECT id, source_path FROM docs").fetchall():
        if not Path(row["source_path"]).exists():
            _drop_doc_chunks(con, row["id"])
            con.execute("DELETE FROM docs WHERE id=?", (row["id"],))
            removed += 1
    con.commit()
    con.close()
    return removed


# ---------------------------------------------------------------------- status

def status(db_path: Path) -> dict:
    con = open_db(db_path)
    try:
        row = con.execute("SELECT v FROM meta WHERE k='built_at'").fetchone()
        built_at = int(row["v"]) if row else 0
        docs = con.execute("SELECT count(*) AS n FROM docs").fetchone()["n"]
        dups = con.execute("SELECT count(*) AS n FROM docs WHERE dup_of IS NOT NULL").fetchone()["n"]
        chunks = con.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]
        by_kind = {
            r["kind"]: r["n"]
            for r in con.execute(
                "SELECT kind, count(*) AS n FROM docs GROUP BY kind ORDER BY n DESC"
            )
        }
        roots = [r["source_root"] for r in con.execute(
            "SELECT DISTINCT source_root FROM docs ORDER BY source_root"
        )]
    finally:
        con.close()

    size = sum(
        p.stat().st_size
        for p in [Path(db_path), Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]
        if p.exists()
    )
    age = time.time() - built_at if built_at else None
    return {
        "db": str(db_path),
        "db_bytes": size,
        "db_mb": round(size / (1024 * 1024), 2),
        "docs": docs,
        "duplicates": dups,
        "chunks": chunks,
        "by_kind": by_kind,
        "sources": roots,
        "built_at": built_at,
        # Not rounded: staleness is a comparison, and rounding an age of a few
        # seconds down to 0.0 would make a zero-hour threshold unreachable.
        "age_hours": (age / 3600) if age is not None else None,
    }
