import tempfile
import unittest
from pathlib import Path

from helpers import TempCorpus  # noqa: E402
from lux_find.config import Config, Source  # noqa: E402
from lux_find.search import confidence, find, tokens  # noqa: E402
from lux_find.store import build, open_db  # noqa: E402


class TestTokens(unittest.TestCase):
    def test_drops_punctuation_and_single_letters(self):
        self.assertEqual(tokens("How do I: a sync-protocol?"), ["how", "do", "sync", "protocol"])

    def test_caps_length(self):
        self.assertEqual(len(tokens(" ".join(f"word{i}" for i in range(50)))), 12)

    def test_empty(self):
        self.assertEqual(tokens("   ?  "), [])


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = TempCorpus().__enter__()
        build(cls.corpus.config)
        cls.con = open_db(cls.corpus.config.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.corpus.__exit__(None, None, None)

    def test_finds_the_document_that_owns_the_topic(self):
        outcome = find(self.con, "tombstone expiry")
        self.assertTrue(outcome.hits)
        self.assertTrue(
            any("sync-protocol" in h.path or "tombstone" in h.path for h in outcome.hits),
            [h.path for h in outcome.hits],
        )

    def test_result_carries_a_line_number(self):
        outcome = find(self.con, "backpressure")
        self.assertTrue(outcome.hits)
        self.assertGreaterEqual(outcome.hits[0].line, 1)

    def test_snippet_marks_the_matched_terms(self):
        outcome = find(self.con, "compaction")
        self.assertTrue(outcome.hits)
        self.assertIn("<<", outcome.hits[0].snippet)

    def test_kind_filter(self):
        outcome = find(self.con, "backpressure", kinds=["code"])
        self.assertTrue(outcome.hits)
        self.assertTrue(all(h.kind == "code" for h in outcome.hits))

        outcome = find(self.con, "backpressure", kinds=["chat"])
        self.assertTrue(all(h.kind == "chat" for h in outcome.hits))

    def test_chat_export_is_searchable(self):
        outcome = find(self.con, "append-only log queue", kinds=["chat"])
        self.assertTrue(outcome.hits, "the ChatGPT-style export was not indexed")

    def test_transcript_is_searchable(self):
        outcome = find(self.con, "conflict during sync", kinds=["chat"])
        self.assertTrue(outcome.hits)

    def test_no_match_is_a_clean_empty_answer(self):
        outcome = find(self.con, "zzzqqqxxx wwwvvvuuu")
        self.assertEqual(outcome.hits, [])
        self.assertEqual(outcome.confidence, "none")

    def test_limit_is_respected(self):
        outcome = find(self.con, "the", limit=2)
        self.assertLessEqual(len(outcome.hits), 2)

    def test_one_document_appears_once(self):
        outcome = find(self.con, "tombstone", limit=20)
        paths = [h.path for h in outcome.hits]
        self.assertEqual(len(paths), len(set(paths)))

    def test_title_boost_is_reported(self):
        outcome = find(self.con, "storage layout")
        top = outcome.hits[0]
        self.assertIn("storage", top.path.lower())
        self.assertIn("title", top.boosts)

    def test_search_is_fast(self):
        outcome = find(self.con, "tombstone expiry backpressure")
        self.assertLess(outcome.ms, 250, "search should be milliseconds, not seconds")


class TestNearDuplicateCollapse(unittest.TestCase):
    """Two different files that happen to carry the identical matched line
    (a synced copy under a different parent directory, same basename) should
    be collapsed to one result line, and the collapse must be reported, not
    silent. See README "Copies are collapsed, not counted twice."""

    def test_identical_lines_from_different_files_are_collapsed_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a").mkdir()
            (base / "b").mkdir()
            # Different content OUTSIDE the matched section (so the two
            # documents are NOT byte-identical - that path is deduplicated at
            # index time by content hash already), but the same line count,
            # so the "## Shared Design" section below lands on the same line
            # number in both files and becomes its own heading-chunk with
            # byte-identical text - exactly the "synced copy" shape the
            # query-time collapse in search.py targets.
            filler = "Alpha " * 40  # > MIN_SECTION_CHARS, forces its own chunk
            shared = (
                "## Shared Design\n\n"
                "The vector clock reconciles concurrent edits on merge.\n"
            )
            (base / "a" / "notes.md").write_text(
                f"# Team A\n\n{filler}\n\n{shared}", encoding="utf-8"
            )
            (base / "b" / "notes.md").write_text(
                f"# Team B\n\n{filler}\n\n{shared}", encoding="utf-8"
            )
            config = Config(
                sources=[
                    Source(path=str(base / "a"), kind="notes"),
                    Source(path=str(base / "b"), kind="notes"),
                ],
                db=str(base / "index.sqlite"),
            )
            build(config)
            con = open_db(config.db_path)
            try:
                outcome = find(con, "vector clock reconciles concurrent")
            finally:
                con.close()

            self.assertGreaterEqual(outcome.collapsed, 1)
            self.assertEqual(outcome.as_dict()["collapsed"], outcome.collapsed)
            paths = [h.path for h in outcome.hits]
            self.assertEqual(len(paths), len(set(paths)))


class TestConfidence(unittest.TestCase):
    def test_levels(self):
        class H:
            def __init__(self, score):
                self.score = score

        self.assertEqual(confidence([]), "none")
        self.assertEqual(confidence([H(1.0)]), "high")
        self.assertEqual(confidence([H(1.0), H(0.5)]), "high")
        self.assertEqual(confidence([H(1.0), H(0.9)]), "medium")
        self.assertEqual(confidence([H(1.0), H(0.99)]), "low")


if __name__ == "__main__":
    unittest.main()
