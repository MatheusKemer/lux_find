import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import make_corpus  # noqa: E402
from lux_find.cli import (  # noqa: E402
    EXIT_INDEX, EXIT_NO_HITS, EXIT_OK, EXIT_USAGE, main,
)
from lux_find.config import guess_kind, load_config  # noqa: E402


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class TestCliEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        make_corpus(self.base)
        self.cfg = self.base / "lux_find.toml"
        self.db = self.base / "index.sqlite"

    def tearDown(self):
        self._tmp.cleanup()

    def _init(self):
        with captured():
            return main([
                "init",
                str(self.base / "notes"),
                str(self.base / "project"),
                str(self.base / "chats"),
                "--config", str(self.cfg),
                "--db", str(self.db),
            ])

    def _index(self):
        with captured() as (out, _):
            code = main(["index", "--config", str(self.cfg), "--quiet"])
        return code, out.getvalue()

    def test_quickstart(self):
        self.assertEqual(self._init(), EXIT_OK)
        self.assertTrue(self.cfg.exists())

        config = load_config(self.cfg)
        self.assertEqual(len(config.sources), 3)
        kinds = {s.kind for s in config.sources}
        self.assertEqual(kinds, {"notes", "code", "chat"})
        for source in config.sources:
            self.assertTrue(
                os.path.isabs(source.path),
                f"init wrote a relative source path: {source.path}",
            )

        code, summary = self._index()
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(summary.startswith("SUMMARY "))
        payload = json.loads(summary[len("SUMMARY "):])
        self.assertTrue(payload["complete"])
        self.assertGreater(payload["indexed"], 5)

        with captured() as (out, _):
            code = main(["find", "tombstone expiry", "--config", str(self.cfg)])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("sync-protocol", out.getvalue())

        with captured() as (out, _):
            code = main(["find", "backpressure", "--config", str(self.cfg), "--json"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out.getvalue())
        self.assertGreater(payload["count"], 0)
        self.assertIn("hits", payload)
        self.assertIn("stale", payload)
        self.assertFalse(payload["stale"])
        self.assertIn("line", payload["hits"][0])

        with captured() as (out, _):
            code = main(["status", "--config", str(self.cfg), "--json"])
        self.assertEqual(code, EXIT_OK)
        info = json.loads(out.getvalue())
        self.assertGreater(info["docs"], 5)
        self.assertGreater(info["chunks"], 0)
        self.assertFalse(info["stale"])

    def test_no_hits_exit_code(self):
        self._init()
        self._index()
        with captured():
            code = main(["find", "zzzqqq-not-present", "--config", str(self.cfg)])
        self.assertEqual(code, EXIT_NO_HITS)

    def test_missing_index_exit_code_prints_path(self):
        self._init()
        with captured() as (_, err):
            code = main(["find", "anything", "--config", str(self.cfg)])
        self.assertEqual(code, EXIT_INDEX)
        self.assertIn(str(self.db), err.getvalue())

    def test_corrupt_index_exit_code_prints_path(self):
        self._init()
        self._index()
        with open(self.db, "r+b") as fh:
            fh.seek(0)
            fh.write(b"corrupted header, definitely not sqlite")
        with captured() as (_, err):
            code = main(["find", "anything", "--config", str(self.cfg)])
        self.assertEqual(code, EXIT_INDEX)
        self.assertIn(str(self.db), err.getvalue())
        self.assertIn("unreadable", err.getvalue())

    def test_missing_config_is_a_usage_error(self):
        with captured() as (_, err):
            code = main(["index", "--config", str(self.base / "absent.toml")])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("init", err.getvalue())

    def test_init_refuses_to_clobber(self):
        self._init()
        with captured() as (_, err):
            code = main([
                "init", str(self.base / "notes"), "--config", str(self.cfg),
            ])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("already exists", err.getvalue())

    def test_stale_index_warns(self):
        self._init()
        self._index()
        # An index built a second ago is stale under a zero-hour threshold.
        self.cfg.write_text(
            self.cfg.read_text(encoding="utf-8").replace("stale_hours = 24", "stale_hours = 0"),
            encoding="utf-8",
        )
        with captured() as (_, err):
            main(["find", "tombstone", "--config", str(self.cfg)])
        self.assertIn("index is", err.getvalue())
        self.assertIn("lux-find index", err.getvalue())

    def test_why_shows_the_score_breakdown(self):
        self._init()
        self._index()
        with captured() as (out, _):
            main(["find", "storage layout", "--config", str(self.cfg), "--why"])
        self.assertIn("bm25=", out.getvalue())

    def test_no_command_prints_help(self):
        with captured() as (out, _):
            code = main([])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("lux-find", out.getvalue())


class TestDbPathResolution(unittest.TestCase):
    def test_relative_db_in_config_resolves_next_to_the_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "sub").mkdir()
            cfg = base / "sub" / "lux_find.toml"
            cfg.write_text(
                '[index]\ndb = "index.sqlite"\n\n[[source]]\npath = "."\nkind = "notes"\n',
                encoding="utf-8",
            )
            config = load_config(cfg)
            self.assertEqual(config.db_path, (base / "sub" / "index.sqlite"))
            self.assertTrue(config.db_path.is_absolute())

    def test_home_shortcut_is_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "lux_find.toml"
            cfg.write_text(
                '[index]\ndb = "~/.lux_find/index.sqlite"\n\n'
                '[[source]]\npath = "."\nkind = "notes"\n',
                encoding="utf-8",
            )
            config = load_config(cfg)
            self.assertEqual(
                str(config.db_path),
                os.path.expanduser("~/.lux_find/index.sqlite"),
            )


class TestKindGuess(unittest.TestCase):
    def test_guesses_from_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_corpus(base)
            self.assertEqual(guess_kind(base / "notes"), "notes")
            self.assertEqual(guess_kind(base / "project"), "code")
            self.assertEqual(guess_kind(base / "chats"), "chat")


class TestConfigRoundTrip(unittest.TestCase):
    """What ``init`` writes must be exactly what ``load_config`` can read back."""

    def test_round_trip(self):
        from lux_find.config import Source, render_config

        text = render_config(
            [Source(path="~/notes", kind="notes", include=["*.md"], exclude=["old/**"])]
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "lux_find.toml"
            cfg.write_text(text, encoding="utf-8")
            config = load_config(cfg)

        self.assertEqual(config.db, "~/.lux_find/index.sqlite")
        self.assertEqual(config.stale_hours, 24)
        self.assertEqual(len(config.sources), 1)
        self.assertEqual(config.sources[0].path, "~/notes")
        self.assertEqual(config.sources[0].include, ["*.md"])
        self.assertEqual(config.sources[0].exclude, ["old/**"])

    def test_invalid_toml_is_a_config_error(self):
        from lux_find.config import ConfigError

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "lux_find.toml"
            cfg.write_text("[index\ndb = broken", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(cfg)


if __name__ == "__main__":
    unittest.main()
