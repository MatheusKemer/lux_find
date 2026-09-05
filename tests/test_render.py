import unittest

from helpers import TempCorpus  # noqa: E402
from lux_find.render import render_human, render_status, shorten_path  # noqa: E402
from lux_find.search import Hit, SearchOutcome, find  # noqa: E402
from lux_find.store import build, open_db, status  # noqa: E402


class TestRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = TempCorpus().__enter__()
        build(cls.corpus.config)
        cls.con = open_db(cls.corpus.config.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.corpus.__exit__(None, None, None)

    def test_markers_survive_without_colour(self):
        outcome = find(self.con, "tombstone")
        text = render_human(outcome, colour=False)
        self.assertIn("<<", text)
        self.assertIn(">>", text)
        self.assertNotIn("\033[", text)

    def test_colour_replaces_the_markers(self):
        outcome = find(self.con, "tombstone")
        text = render_human(outcome, colour=True)
        self.assertNotIn("<<", text)
        self.assertIn("\033[", text)

    def test_empty_result_explains_itself(self):
        outcome = find(self.con, "zzzqqqxxx wwwvvvuuu")
        text = render_human(outcome, colour=False)
        self.assertIn("no matches", text)
        self.assertIn("lux-find index", text)

    def test_why_lines(self):
        outcome = find(self.con, "storage layout")
        text = render_human(outcome, colour=False, why=True)
        self.assertIn("bm25=", text)
        self.assertIn("title=+", text)

    def test_status_marks_a_stale_index(self):
        info = status(self.corpus.config.db_path)
        fresh = render_status(info, stale_hours=24, colour=False)
        self.assertIn("fresh", fresh)
        stale = render_status(info, stale_hours=0, colour=False)
        self.assertIn("STALE", stale)

    def test_shorten_path(self):
        import os
        self.assertTrue(shorten_path(os.path.expanduser("~/x/y")).startswith("~/"))
        self.assertEqual(shorten_path("/opt/thing"), "/opt/thing")


class TestCollapsedSignal(unittest.TestCase):
    def _hit(self, path="notes/a.md"):
        return Hit(
            uri=path, path=path, title="a.md", kind="notes", line=1,
            snippet="hi", score=1.0, mtime=0.0,
        )

    def test_header_names_the_collapsed_count(self):
        outcome = SearchOutcome(
            query="hi", hits=[self._hit()], ms=1.0, confidence="high",
            scanned=3, collapsed=2,
        )
        text = render_human(outcome, colour=False)
        self.assertIn("(+2 similar collapsed)", text)

    def test_no_note_when_nothing_was_collapsed(self):
        outcome = SearchOutcome(
            query="hi", hits=[self._hit()], ms=1.0, confidence="high",
            scanned=3, collapsed=0,
        )
        text = render_human(outcome, colour=False)
        self.assertNotIn("collapsed", text)

    def test_json_carries_the_same_count(self):
        outcome = SearchOutcome(
            query="hi", hits=[], ms=1.0, confidence="none", scanned=0, collapsed=5,
        )
        self.assertEqual(outcome.as_dict()["collapsed"], 5)


class TestDeterministicDedup(unittest.TestCase):
    def test_canonical_path_wins_over_a_copy(self):
        with TempCorpus() as corpus:
            source = corpus.base / "notes" / "design" / "sync-protocol.md"
            copy = corpus.base / "notes" / "design" / "sync-protocol (copy).md"
            copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            build(corpus.config)

            con = open_db(corpus.config.db_path)
            row = con.execute(
                "SELECT uri FROM docs WHERE dup_of IS NOT NULL"
            ).fetchone()
            con.close()
            self.assertIsNotNone(row)
            self.assertIn("(copy)", row["uri"], "the copy should be the duplicate, not the original")


class TestSnippetWrapping(unittest.TestCase):
    """The terminal demo showed snippet lines 110-145 cols wide getting torn
    mid-word by a 108-column terminal. render_human() now wraps at word
    boundaries when writing to a real TTY, forced here via the `width`
    parameter since a test run has no TTY of its own.
    """

    def _hit(self, snippet):
        return Hit(
            uri="notes/a.md", path="notes/a.md", title="a.md", kind="notes", line=1,
            snippet=snippet, score=1.0, mtime=0.0,
        )

    def _make_outcome(self, snippet):
        return SearchOutcome(
            query="q", hits=[self._hit(snippet)], ms=1.0, confidence="high", scanned=1,
        )

    def test_forced_width_wraps_long_snippet_on_word_boundaries(self):
        snippet = " ".join(f"word{i}" for i in range(40))
        text = render_human(self._make_outcome(snippet), colour=False, width=60)
        body_lines = [ln for ln in text.split("\n") if ln.startswith("   ")]
        self.assertGreater(len(body_lines), 1, "a long snippet should wrap onto more than one line")
        for ln in body_lines:
            self.assertLessEqual(len(ln), 60)
        # Word boundaries only: every wordN token survives whole, in order.
        joined_tokens = " ".join(ln.strip() for ln in body_lines).split()
        self.assertEqual(joined_tokens, [f"word{i}" for i in range(40)])

    def test_forced_width_keeps_highlight_markers_balanced(self):
        words = [f"word{i}" for i in range(40)]
        words[20] = "<<match>>"
        snippet = " ".join(words)
        text = render_human(self._make_outcome(snippet), colour=True, width=60)
        body_lines = [ln for ln in text.split("\n") if ln.startswith("   ")]
        self.assertGreater(len(body_lines), 1)

        open_code = "\033[33m\033[1m"
        reset_code = "\033[0m"
        for ln in body_lines:
            if open_code in ln:
                self.assertIn(
                    reset_code, ln,
                    "an opened highlight must be reset on the same line, or its "
                    "colour bleeds into whatever the terminal prints next",
                )
        self.assertEqual(
            sum(ln.count(open_code) for ln in body_lines),
            sum(ln.count(reset_code) for ln in body_lines),
        )

    def test_default_width_on_a_non_tty_is_unchanged_single_line(self):
        # Test runs are never attached to a TTY, so width=None (the default)
        # must reproduce exactly what render_human() did before this feature:
        # one line per hit, markers untouched.
        snippet = " ".join(f"word{i}" for i in range(40)) + " <<match>>"
        text = render_human(self._make_outcome(snippet), colour=False)
        body_lines = [ln for ln in text.split("\n") if ln.startswith("   ")]
        self.assertEqual(len(body_lines), 1)
        self.assertEqual(body_lines[0], f"   {snippet}")


if __name__ == "__main__":
    unittest.main()
