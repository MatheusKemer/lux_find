"""One test per defect found in the pre-release audit.

These are the reporters' own reproductions, kept as tests so a fix cannot be
undone quietly. Most of them are variations of a single failure mode - the
index answering "nothing here" about something it does hold - which is the one
outcome this tool must never produce, because an empty result reads as a
verdict.

They drive the real CLI in a subprocess: exit codes and the JSON contract are
part of what is being tested, and neither is visible from inside the library.
"""

import json
import os
import stat
import subprocess
import sqlite3
import time
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

import lux_find

SRC = str(Path(lux_find.__file__).resolve().parent.parent)
ENVIRONMENT = {
    **os.environ,
    "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
    "NO_COLOR": "1",
}


class CliCase(unittest.TestCase):
    """A temporary corpus plus the handful of helpers every case below needs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.db = self.base / "index.sqlite"

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *args, expect=None):
        proc = subprocess.run(
            [sys.executable, "-m", "lux_find", *args],
            cwd=str(self.base), env=ENVIRONMENT, capture_output=True, text=True,
        )
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                f"`lux-find {' '.join(args)}` exited {proc.returncode}, expected {expect}\n"
                f"stdout: {proc.stdout[:400]}\nstderr: {proc.stderr[:400]}",
            )
        return proc

    def summary(self, proc):
        for line in proc.stdout.splitlines():
            if line.startswith("SUMMARY "):
                return json.loads(line[len("SUMMARY "):])
        self.fail(f"no SUMMARY line in output: {proc.stdout[:300]}")

    def hits(self, query, *extra):
        proc = self.cli("find", query, "--json", *extra)
        self.assertIn(proc.returncode, (0, 1), proc.stderr[:300])
        return json.loads(proc.stdout)

    def seed(self, files, kind="notes"):
        root = self.base / "corpus"
        root.mkdir(parents=True, exist_ok=True)
        for rel, body in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        self.cli("init", str(root), "--kind", kind, "--db", str(self.db), expect=0)
        return root


class TestNothingDisappearsSilently(CliCase):
    def test_an_unreadable_subdirectory_is_an_error_and_prunes_nothing(self):
        root = self.seed({"a.md": "# A\n\nalpha content\n",
                          "deep/b.md": "# B\n\nbravo content\n"})
        self.cli("index", "-q", expect=0)
        os.chmod(root / "deep", 0o000)
        try:
            report = self.summary(self.cli("index", "-q", expect=2))
            self.assertTrue(report["errors"], "an unreadable directory reported nothing")
            self.assertEqual(report["pruned"], 0, "pruned documents it could not read")
        finally:
            os.chmod(root / "deep", 0o755)
        self.assertEqual(self.hits("bravo")["count"], 1)

    def test_a_file_over_the_size_limit_is_counted(self):
        root = self.seed({"small.md": "# S\n\nsmall content\n"})
        (root / "big.md").write_text("# Big\n\n" + ("padding " * 700_000), encoding="utf-8")
        report = self.summary(self.cli("index", "-q", expect=0))
        self.assertEqual(report["excluded"].get("over-size-limit"), 1)

    def test_a_single_file_source_is_indexed(self):
        lone = self.base / "solo.md"
        lone.write_text("# Solo\n\nnarwhal facts live here\n", encoding="utf-8")
        self.cli("init", str(lone), "--db", str(self.db), expect=0)
        self.assertEqual(self.summary(self.cli("index", "-q", expect=0))["indexed"], 1)
        self.assertEqual(self.hits("narwhal")["count"], 1)

    def test_force_prune_appears_in_the_json_summary(self):
        root = self.seed({"a.md": "# A\n\nalpha\n", "b.md": "# B\n\nbravo\n"})
        self.cli("index", "-q", expect=0)
        (root / "b.md").unlink()
        report = self.summary(self.cli("index", "-q", "--force-prune", expect=0))
        self.assertGreaterEqual(report["pruned"], 1)


class TestIncrementalTruth(CliCase):
    def test_a_renamed_file_stays_findable(self):
        root = self.seed({"one.md": "# One\n\nunique zebra content\n"})
        self.cli("index", "-q", expect=0)
        (root / "one.md").rename(root / "two.md")
        self.cli("index", "-q", expect=0)
        found = self.hits("zebra")
        self.assertEqual(found["count"], 1, "a rename made the document unsearchable")
        self.assertTrue(found["hits"][0]["path"].endswith("two.md"))

    def test_a_duplicate_survives_losing_the_copy_it_pointed_at(self):
        root = self.seed({"a.md": "# Same\n\nidentical quokka body\n",
                          "b-longer-name.md": "# Same\n\nidentical quokka body\n"})
        self.cli("index", "-q", expect=0)
        (root / "a.md").unlink()
        self.cli("index", "-q", expect=0)
        self.assertEqual(self.hits("quokka")["count"], 1)

    def test_an_edit_in_the_same_second_at_the_same_size_is_reread(self):
        root = self.seed({"c.md": "# C\n\nthe word is dog here\n"})
        self.cli("index", "-q", expect=0)
        target = root / "c.md"
        before = target.stat()
        target.write_text("# C\n\nthe word is cat here\n", encoding="utf-8")
        os.utime(target, (before.st_atime, before.st_mtime))
        self.cli("index", "-q", expect=0)
        self.assertEqual(self.hits("cat")["count"], 1, "stale text was served as current")

    def test_a_full_rebuild_is_never_visible_as_an_empty_index(self):
        self.seed({f"n{n}.md": f"# N{n}\n\nflamingo content {n}\n" for n in range(200)})
        self.cli("index", "-q", expect=0)
        writer = subprocess.Popen(
            [sys.executable, "-m", "lux_find", "index", "--full", "-q"],
            cwd=str(self.base), env=ENVIRONMENT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        empties = 0
        try:
            while writer.poll() is None:
                found = self.hits("flamingo")
                if found["count"] == 0:
                    empties += 1
        finally:
            writer.wait()
        self.assertEqual(empties, 0, "a reader saw an empty index during --full")


class TestWhatTheResultSays(CliCase):
    def test_a_kind_filter_survives_a_crowded_candidate_pool(self):
        notes = self.base / "corpus" / "notes"
        code = self.base / "corpus" / "code"
        notes.mkdir(parents=True)
        code.mkdir(parents=True)
        for n in range(900):
            (notes / f"n{n}.md").write_text(
                f"# widget note {n}\n\nwidget widget widget discussion {n}\n", encoding="utf-8")
        (code / "widget.py").write_text("def widget():\n    return 'widget'\n", encoding="utf-8")
        self.cli("init", str(notes), "--kind", "notes", "--db", str(self.db), expect=0)
        config = self.base / "lux_find.toml"
        config.write_text(
            config.read_text() + f'\n[[source]]\npath = "{code}"\nkind = "code"\n',
            encoding="utf-8",
        )
        self.cli("index", "-q", expect=0)
        found = self.hits("widget", "--kind", "code")
        self.assertGreaterEqual(found["count"], 1, "the only code match lost its pool slot")
        self.assertTrue(all(h["kind"] == "code" for h in found["hits"]))

    def test_the_cited_line_holds_the_match(self):
        body = "\n".join(f"# filler {n}" for n in range(60)) + "\nmarmoset = 1\n"
        root = self.seed({"code.py": body}, kind="code")
        self.cli("index", "-q", expect=0)
        found = self.hits("marmoset")
        self.assertEqual(found["count"], 1)
        lines = (root / "code.py").read_text().splitlines()
        cited = found["hits"][0]["line"]
        self.assertIn("marmoset", lines[cited - 1],
                      f"cited line {cited} holds {lines[cited - 1]!r}")

    def test_a_document_is_findable_by_the_name_of_its_file(self):
        self.seed({"kubernetes-upgrade.md": "# Upgrade\n\nthe body never repeats the name\n"})
        self.cli("index", "-q", expect=0)
        self.assertEqual(self.hits("kubernetes upgrade")["count"], 1)

    def test_the_file_name_never_leaks_into_the_snippet(self):
        self.seed({"kubernetes-upgrade.md": "# Upgrade\n\nplain body text about pods\n"})
        self.cli("index", "-q", expect=0)
        snippet = self.hits("pods")["hits"][0]["snippet"]
        self.assertNotIn("kubernetes", snippet.lower())

    def test_a_decomposed_query_matches_composed_text(self):
        self.seed({"pt.md": "# Notas\n\nA ação de replicação foi adiada.\n"})
        self.cli("index", "-q", expect=0)
        decomposed = unicodedata.normalize("NFD", "ação")
        self.assertEqual(self.hits(decomposed)["count"], 1)

    def test_utf16_text_is_indexed_not_mistaken_for_binary(self):
        root = self.base / "corpus"
        root.mkdir(parents=True)
        (root / "wide.md").write_bytes("# Wide\n\nplatypus in utf sixteen\n".encode("utf-16"))
        self.cli("init", str(root), "--db", str(self.db), expect=0)
        self.cli("index", "-q", expect=0)
        self.assertEqual(self.hits("platypus")["count"], 1)

    def test_wrapped_sections_do_not_share_a_line_number(self):
        long_section = "# Section\n\n" + "\n".join(
            f"line {n} of prose about pelicans" for n in range(400))
        self.seed({"long.md": long_section})
        self.cli("index", "-q", expect=0)
        con = sqlite3.connect(self.db)
        try:
            lines = [row[0] for row in con.execute("SELECT line FROM chunks ORDER BY id")]
        finally:
            con.close()
        self.assertGreater(len(lines), 1)
        self.assertEqual(len(set(lines)), len(lines), f"chunks share a line: {lines[:8]}")


class TestPathologicalShapes(CliCase):
    """Corpora that are ordinary to own and hostile to a line-oriented splitter."""

    def test_one_enormous_line_does_not_freeze_a_query(self):
        # A minified bundle, a generated JSON blob, one very long log line: all
        # are a single "section", and handing FTS5 a multi-megabyte chunk made
        # snippet() spend minutes on a query that should take milliseconds.
        self.seed({"log.txt": "needle_y " * 200_000 + "\n",
                   "ok.md": "# Ok\n\nneedle_y appears here too\n"})
        self.cli("index", "-q", expect=0)

        started = time.perf_counter()
        found = self.hits("needle_y")
        elapsed = time.perf_counter() - started

        self.assertGreaterEqual(found["count"], 1, "the long-line document vanished")
        self.assertLess(elapsed, 10, f"one query over a long-line corpus took {elapsed:.1f}s")

        con = sqlite3.connect(self.db)
        try:
            widest = con.execute("SELECT max(length(body)) FROM chunks").fetchone()[0]
        finally:
            con.close()
        self.assertLessEqual(widest, 4000, f"a chunk of {widest} chars escaped the ceiling")


class TestAnOlderIndexIsRebuilt(CliCase):
    def test_index_rebuilds_an_older_schema_instead_of_crashing(self):
        # `find` tells the user to rebuild; the rebuild command itself must not
        # be the thing that dies. The index is a cache - remaking it is free of
        # consequences, every document is still on disk.
        root = self.seed({"a.md": "# A\n\nalpha content\n"})
        self.cli("index", "-q", expect=0)

        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE chunks DROP COLUMN title")   # the older shape
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version','1')")
        con.commit()
        con.close()

        run = self.cli("index", expect=0)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(self.hits("alpha")["count"], 1, "the rebuilt index does not answer")
        del root


class TestQueriesMeanWhatPeopleType(CliCase):
    """Quoting and a trailing star are the two oldest search conventions.

    Both used to be stripped as punctuation, so a phrase search silently
    returned the loose-word results and a prefix search returned nothing. A
    search tool that ignores quotes reads as broken, whatever the docs say.
    """

    def setUp(self):
        super().setUp()
        self.seed({
            "a.md": "# Cache\n\nThe distributed cache uses a ring buffer.\n",
            "b.md": "# Notes\n\nWe distributed the work, and the cache came later.\n",
            "c.md": "# Tomb\n\nA tombstone expires after ninety days.\n",
        })
        self.cli("index", "-q", expect=0)

    def test_loose_words_still_match_any_document(self):
        self.assertEqual(self.hits("distributed cache")["count"], 2)

    def test_a_quoted_phrase_requires_the_words_together(self):
        found = self.hits('"distributed cache"')
        self.assertEqual(found["count"], 1, "a phrase matched documents that only share words")
        self.assertTrue(found["hits"][0]["path"].endswith("a.md"))

    def test_a_trailing_star_matches_by_prefix(self):
        self.assertEqual(self.hits("tombston*")["count"], 1)
        self.assertEqual(self.hits("zzzqq*")["count"], 0)

    def test_all_requires_every_term(self):
        self.assertEqual(self.hits("distributed tombstone")["count"], 3)
        self.assertEqual(self.hits("distributed tombstone", "--all")["count"], 0)
        self.assertEqual(self.hits("distributed cache", "--all")["count"], 2)

    def test_the_echoed_query_is_not_double_quoted(self):
        out = self.cli("find", '"distributed cache"', expect=0).stdout
        self.assertNotIn('""', out)

    def test_punctuation_alone_still_finds_nothing_and_does_not_crash(self):
        for query in ('"', '""', "*", '"" *', "!!! ???", "a"):
            with self.subTest(query=query):
                proc = self.cli("find", query)
                self.assertEqual(proc.returncode, 1, f"{query!r} -> {proc.stderr[:200]}")
                self.assertNotIn("Traceback", proc.stderr)


class TestFailureIsLoud(CliCase):
    def test_an_index_from_another_schema_exits_three(self):
        self.seed({"a.md": "# A\n\nalpha\n"})
        self.cli("index", "-q", expect=0)
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version','99')")
        con.commit()
        con.close()
        proc = self.cli("find", "alpha", expect=3)
        self.assertNotIn("Traceback", proc.stderr)

    def test_the_index_is_readable_only_by_its_owner(self):
        self.seed({"a.md": "# A\n\nalpha\n"})
        self.cli("index", "-q", expect=0)
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db) + suffix)
            if path.exists():
                mode = stat.S_IMODE(path.stat().st_mode)
                self.assertEqual(mode & 0o077, 0, f"{path.name} is {oct(mode)}")

    def test_escape_sequences_in_content_never_reach_stdout(self):
        esc = chr(27)
        self.seed({"evil.md": f"# Evil\n\nthe okapi {esc}]0;PWNED{chr(7)} hides here\n"})
        self.cli("index", "-q", expect=0)
        out = self.cli("find", "okapi", expect=0).stdout
        self.assertNotIn(esc, out)
        self.assertNotIn(chr(7), out)


if __name__ == "__main__":
    unittest.main()
