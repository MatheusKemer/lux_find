"""Behaviour over time: edit, add, delete, no-op reindex, status.

A search index that only works on the first ``index`` run is not useful -
notes get edited, files get added and removed, and re-running ``index`` with
nothing changed should be fast and quiet. This exercises the real CLI against
a real SQLite/FTS5 database on disk at every step; nothing is mocked or
stubbed.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from lux_find.cli import EXIT_NO_HITS, EXIT_OK, main  # noqa: E402
from lux_find.store import open_db  # noqa: E402


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


ALPHA_ORIGINAL = (
    "# Alpha\n\n"
    "The original alpha note talks about widget throughput and nothing "
    "else.\n"
)
ALPHA_REVISED = (
    "# Alpha\n\n"
    "The alpha note was rewritten to talk about gadget latency instead. The "
    "previous subject has been dropped entirely and replaced with several "
    "new sentences so the file size actually changes on disk.\n"
)
BETA = (
    "# Beta\n\n"
    "The beta note is about beta-flavoured onboarding steps and nothing "
    "else in this corpus mentions them.\n"
)
GAMMA = (
    "# Gamma\n\n"
    "The gamma note documents the gamma rollout checklist, a topic unique "
    "to this file.\n"
)
DELTA = (
    "# Delta\n\n"
    "The delta note was added after the first index run and covers delta "
    "cluster failover, a topic that did not exist before.\n"
)


class TestIncremental(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.notes = self.base / "notes"
        self.notes.mkdir()
        (self.notes / "alpha.md").write_text(ALPHA_ORIGINAL, encoding="utf-8")
        (self.notes / "beta.md").write_text(BETA, encoding="utf-8")
        (self.notes / "gamma.md").write_text(GAMMA, encoding="utf-8")

        self.cfg = self.base / "lux_find.toml"
        self.db = self.base / "index.sqlite"
        with captured():
            code = main([
                "init", str(self.notes), "--config", str(self.cfg), "--db", str(self.db),
            ])
        assert code == EXIT_OK

    def tearDown(self):
        self._tmp.cleanup()

    # ----------------------------------------------------------- helpers

    def _index(self, *extra: str) -> dict:
        with captured() as (out, _):
            code = main(["index", "--config", str(self.cfg), "--quiet", *extra])
        self.assertEqual(code, EXIT_OK)
        return json.loads(out.getvalue()[len("SUMMARY "):])

    def _find(self, query: str) -> tuple[int, dict]:
        with captured() as (out, _):
            code = main(["find", query, "--config", str(self.cfg), "--json"])
        return code, json.loads(out.getvalue())

    def _status(self) -> dict:
        with captured() as (out, _):
            code = main(["status", "--config", str(self.cfg), "--json"])
        self.assertEqual(code, EXIT_OK)
        return json.loads(out.getvalue())

    def _doc_paths_in_store(self) -> set[str]:
        """Read the docs table directly - the ground truth, not a report about it."""
        con = open_db(self.db)
        try:
            rows = con.execute("SELECT source_path FROM docs").fetchall()
        finally:
            con.close()
        return {row["source_path"] for row in rows}

    # ----------------------------------------------------------------- tests

    def test_initial_index_finds_the_original_content(self):
        summary = self._index()
        self.assertEqual(summary["indexed"], 3)

        code, payload = self._find("widget throughput")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["hits"][0]["title"], "alpha.md")

    def test_editing_a_note_surfaces_new_content_and_drops_the_old(self):
        self._index()

        (self.notes / "alpha.md").write_text(ALPHA_REVISED, encoding="utf-8")
        summary = self._index()
        # The edited file is re-read (not skipped as unchanged), nothing else is.
        self.assertEqual(summary["indexed"], 1)
        self.assertEqual(summary["skipped"], 2)

        code, payload = self._find("gadget latency")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["hits"][0]["title"], "alpha.md")

        code, _payload = self._find("widget throughput")
        self.assertEqual(code, EXIT_NO_HITS)

    def test_adding_a_file_is_found_only_after_reindex(self):
        self._index()

        code, _payload = self._find("delta cluster failover")
        self.assertEqual(code, EXIT_NO_HITS)

        (self.notes / "delta.md").write_text(DELTA, encoding="utf-8")

        # Still not there before the reindex - the CLI never scans on `find`.
        code, _payload = self._find("delta cluster failover")
        self.assertEqual(code, EXIT_NO_HITS)

        summary = self._index()
        self.assertEqual(summary["indexed"], 1)

        code, payload = self._find("delta cluster failover")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["hits"][0]["title"], "delta.md")

        status = self._status()
        self.assertEqual(status["docs"], 4)

    def test_deleting_a_file_removes_it_after_reindex(self):
        self._index()
        code, _payload = self._find("beta-flavoured onboarding")
        self.assertEqual(code, EXIT_OK)

        (self.notes / "beta.md").unlink()

        # A single missing file among several does not trip the prune guard -
        # the source still scans the other two documents just fine.
        summary = self._index()
        self.assertEqual(summary["pruned"], 1)

        code, _payload = self._find("beta-flavoured onboarding")
        self.assertEqual(code, EXIT_NO_HITS)

        store_paths = self._doc_paths_in_store()
        self.assertFalse(
            any(p.endswith("beta.md") for p in store_paths),
            f"beta.md still present in the store: {store_paths}",
        )
        self.assertEqual(len(store_paths), 2)

        status = self._status()
        self.assertEqual(status["docs"], 2)

    def test_reindex_with_no_changes_is_a_no_op(self):
        self._index()
        before = self._status()

        summary = self._index()
        self.assertEqual(summary["indexed"], 0)
        self.assertEqual(summary["skipped"], 3)
        self.assertEqual(summary["pruned"], 0)
        self.assertEqual(summary["duplicates"], 0)

        after = self._status()
        self.assertEqual(after["docs"], before["docs"])
        self.assertEqual(after["chunks"], before["chunks"])

    def test_status_reflects_edits_additions_and_deletions_together(self):
        self._index()

        (self.notes / "alpha.md").write_text(ALPHA_REVISED, encoding="utf-8")
        (self.notes / "delta.md").write_text(DELTA, encoding="utf-8")
        (self.notes / "beta.md").unlink()
        self._index()

        status = self._status()
        self.assertEqual(status["docs"], 3)  # alpha (edited), gamma, delta
        self.assertFalse(status["stale"])
        self.assertGreater(status["chunks"], 0)

        store_paths = self._doc_paths_in_store()
        self.assertEqual(len(store_paths), 3)
        self.assertFalse(any(p.endswith("beta.md") for p in store_paths))
        self.assertTrue(any(p.endswith("delta.md") for p in store_paths))


if __name__ == "__main__":
    unittest.main()
