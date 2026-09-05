import sqlite3
import unittest
from pathlib import Path

from helpers import TempCorpus  # noqa: E402
from lux_find.store import (  # noqa: E402
    IndexCorrupt, IndexMissing, PruneGuardTripped, build, force_prune, open_db,
    status,
)


class TestBuild(unittest.TestCase):
    def test_full_build_then_incremental_skips(self):
        with TempCorpus() as corpus:
            first = build(corpus.config)
            self.assertGreater(first.indexed, 5)
            self.assertGreater(first.chunks, first.indexed)
            self.assertTrue(first.complete)

            second = build(corpus.config)
            self.assertEqual(second.indexed, 0)
            self.assertEqual(second.skipped, first.scanned)

    def test_edited_file_is_reindexed_and_old_text_disappears(self):
        with TempCorpus() as corpus:
            build(corpus.config)
            target = corpus.base / "notes" / "design" / "storage.md"
            target.write_text(
                "# Storage layout\n\nRewritten: we now use a segmented ring buffer.\n",
                encoding="utf-8",
            )
            # bump mtime beyond the one-second signature resolution
            import os
            import time
            os.utime(target, (time.time() + 10, time.time() + 10))

            build(corpus.config)
            con = open_db(corpus.config.db_path)
            rows = con.execute(
                "SELECT count(*) AS n FROM chunk_fts WHERE chunk_fts MATCH 'compaction'"
            ).fetchone()["n"]
            fresh = con.execute(
                "SELECT count(*) AS n FROM chunk_fts WHERE chunk_fts MATCH 'segmented'"
            ).fetchone()["n"]
            con.close()
            self.assertEqual(rows, 0, "stale text is still searchable after a rewrite")
            self.assertGreater(fresh, 0)

    def test_deleted_file_is_pruned(self):
        with TempCorpus() as corpus:
            build(corpus.config)
            (corpus.base / "notes" / "recipes" / "bread.md").unlink()
            stats = build(corpus.config)
            self.assertEqual(stats.pruned, 1)
            con = open_db(corpus.config.db_path)
            left = con.execute(
                "SELECT count(*) AS n FROM docs WHERE title LIKE '%bread%'"
            ).fetchone()["n"]
            con.close()
            self.assertEqual(left, 0)

    def test_prune_guard_refuses_a_source_that_went_dark(self):
        with TempCorpus() as corpus:
            build(corpus.config)
            # Simulate "cannot read the source" without deleting anything: an
            # include pattern that matches nothing.
            corpus.config.sources[0].include = ["*.nothing-matches-this"]
            with self.assertRaises(PruneGuardTripped) as ctx:
                build(corpus.config)
            self.assertIn("Refusing to prune", str(ctx.exception))

            con = open_db(corpus.config.db_path)
            kept = con.execute("SELECT count(*) AS n FROM docs").fetchone()["n"]
            con.close()
            self.assertGreater(kept, 0, "the guard must leave the index intact")

    def test_force_prune_is_explicit(self):
        with TempCorpus() as corpus:
            build(corpus.config)
            for path in (corpus.base / "notes").rglob("*.md"):
                path.unlink()
            removed = force_prune(corpus.config)
            self.assertGreater(removed, 0)

    def test_duplicate_files_are_collapsed(self):
        with TempCorpus() as corpus:
            source = corpus.base / "notes" / "design" / "sync-protocol.md"
            copy = corpus.base / "notes" / "design" / "sync-protocol (backup copy).md"
            copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            stats = build(corpus.config)
            self.assertGreaterEqual(stats.duplicates, 1)

            con = open_db(corpus.config.db_path)
            dup = con.execute(
                "SELECT count(*) AS n FROM docs WHERE dup_of IS NOT NULL"
            ).fetchone()["n"]
            con.close()
            self.assertGreaterEqual(dup, 1)

    def test_full_rebuild_matches_incremental(self):
        with TempCorpus() as corpus:
            build(corpus.config)
            before = status(corpus.config.db_path)
            build(corpus.config, full=True)
            after = status(corpus.config.db_path)
            self.assertEqual(before["docs"], after["docs"])
            self.assertEqual(before["chunks"], after["chunks"])

    def test_missing_source_is_reported_not_swallowed(self):
        with TempCorpus() as corpus:
            corpus.config.sources[0].path = str(corpus.base / "does-not-exist")
            stats = build(corpus.config)
            self.assertFalse(stats.complete)
            self.assertTrue(any("does-not-exist" in e for e in stats.errors))


class TestOpen(unittest.TestCase):
    def test_missing_index_raises(self):
        with self.assertRaises(IndexMissing):
            open_db(Path("/nonexistent/path/index.sqlite"))

    def test_corrupt_index_raises(self):
        with TempCorpus() as corpus:
            build(corpus.config)
            path = corpus.config.db_path
            with open(path, "r+b") as fh:
                fh.seek(0)
                fh.write(b"this is not a database at all, not even close")
            with self.assertRaises(IndexCorrupt) as ctx:
                open_db(path)
            self.assertIn(str(path), str(ctx.exception))


class TestStatus(unittest.TestCase):
    def test_status_reports_counts_and_age(self):
        with TempCorpus() as corpus:
            build(corpus.config)
            info = status(corpus.config.db_path)
            self.assertGreater(info["docs"], 5)
            self.assertGreater(info["chunks"], info["docs"])
            self.assertGreater(info["built_at"], 0)
            self.assertLess(info["age_hours"], 1)
            self.assertIn("notes", info["by_kind"])
            self.assertIn("code", info["by_kind"])
            self.assertIn("chat", info["by_kind"])


if __name__ == "__main__":
    unittest.main()
