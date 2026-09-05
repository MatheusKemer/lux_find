"""Regression tests for the hardening pass before the first public release.

Each test covers the *class* of the bug, not the single input that exposed it:
a credential-named file must be skipped by every connector, not just the one
that was fixed; the index must be private however it was created; and no
control character must reach a terminal, whether it arrived through a file's
content or through its name.

Credential file names are assembled from parts here for the same reason the
shared fixtures assemble fake keys from parts: this source file should contain
nothing that looks like the real thing to a scanner.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import make_corpus  # noqa: E402
from lux_find import connectors  # noqa: E402
from lux_find.config import Source  # noqa: E402
from lux_find.render import render_human, sanitize  # noqa: E402
from lux_find.search import Hit, SearchOutcome  # noqa: E402
from lux_find.store import build, open_db  # noqa: E402

DOT = "."
ENV = DOT + "env"
RSA = "id" + "_rsa"

# Names that exist only to hold a credential, in the shapes each connector sees.
CREDENTIAL_FILES = {
    "notes": [ENV, ENV + ".local", RSA, "server.pem", "secrets.txt"],
    "code": [ENV, "deploy.key", DOT + "netrc"],
    "chat": [ENV + ".json", RSA + ".jsonl", ENV + ".jsonl"],
}


class TestCredentialFilesAreNeverOpened(unittest.TestCase):
    """SECURITY.md promises these are never opened. Every connector must obey."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_connector_skips_credential_named_files(self):
        for kind, names in CREDENTIAL_FILES.items():
            with self.subTest(kind=kind):
                root = self.base / kind
                root.mkdir(parents=True, exist_ok=True)
                for name in names:
                    json_shaped = name.endswith(".json") or name.endswith(".jsonl")
                    body = '{"text": "leaked"}\n' if json_shaped else "leaked\n"
                    (root / name).write_text(body, encoding="utf-8")

                # One legitimate document, so an empty scan cannot pass by accident.
                keeper = "keep.jsonl" if kind == "chat" else "keep.md"
                keeper_body = (
                    '{"text": "ordinary content"}\n'
                    if kind == "chat"
                    else "# Keep\n\nordinary content\n"
                )
                (root / keeper).write_text(keeper_body, encoding="utf-8")

                refs = list(connectors.for_kind(kind).scan(Source(path=str(root), kind=kind)))
                scanned = {Path(r.source_path).name for r in refs}

                self.assertIn(keeper, scanned, f"{kind}: the ordinary file must still be indexed")
                for name in names:
                    self.assertNotIn(name, scanned, f"{kind}: {name} must never be opened")


class TestIndexIsPrivate(unittest.TestCase):
    """The index holds the plaintext of the whole corpus: owner-only, always."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.config = make_corpus(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def _assert_owner_only(self, path: Path):
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(
            mode & 0o077,
            0,
            f"{path.name} is {oct(mode)} - readable beyond its owner",
        )

    def test_a_fresh_index_is_not_readable_by_others(self):
        open_db(self.config.db_path, create=True).close()
        self._assert_owner_only(self.config.db_path)

    def test_a_built_index_and_its_wal_sidecars_are_not_readable_by_others(self):
        build(self.config)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.config.db_path) + suffix)
            if candidate.exists():
                self._assert_owner_only(candidate)

    def test_permissions_are_tightened_on_an_index_that_was_left_open(self):
        open_db(self.config.db_path, create=True).close()
        os.chmod(self.config.db_path, 0o644)
        build(self.config)
        self._assert_owner_only(self.config.db_path)


class TestControlCharactersNeverReachTheTerminal(unittest.TestCase):
    """Indexed content is written by other people. It must not drive the screen."""

    # Built from parts so this source file holds no literal escape sequence.
    ESC = chr(27)
    PAYLOADS = {
        "osc-window-title": ESC + "]0;PWNED" + chr(7),
        "csi-clear-screen": ESC + "[2J",
        "csi-cursor-home": ESC + "[H",
        "c1-eighty-bit": chr(0x9B) + "31m",
        "delete-char": chr(127),
        "null-byte": chr(0),
    }

    def _make_outcome(self, *, snippet: str, path: str, query: str = "anything") -> SearchOutcome:
        hit = Hit(
            uri=path,
            path=path,
            title="t",
            kind="notes",
            line=1,
            snippet=snippet,
            score=1.0,
            mtime=0.0,
            bm25=1.0,
            boosts={},
        )
        return SearchOutcome(query=query, hits=[hit], ms=1.0, confidence="high", scanned=1)

    def test_sanitize_strips_control_characters(self):
        for name, payload in self.PAYLOADS.items():
            with self.subTest(payload=name):
                cleaned = sanitize(f"before{payload}after")
                self.assertNotIn(self.ESC, cleaned)
                self.assertNotIn(chr(0), cleaned)
                self.assertNotIn(chr(7), cleaned)
                self.assertNotIn(chr(127), cleaned)
                self.assertNotIn(chr(0x9B), cleaned)
                self.assertTrue(cleaned.startswith("before"))
                self.assertTrue(cleaned.endswith("after"))

    def test_sanitize_keeps_tabs_newlines_and_non_ascii_text(self):
        self.assertEqual(sanitize("a\tb\nc"), "a\tb\nc")
        self.assertEqual(sanitize("ação — 日本語 🙂"), "ação — 日本語 🙂")

    def test_no_control_character_survives_rendering(self):
        for name, payload in self.PAYLOADS.items():
            for surface in ("snippet", "filename", "query"):
                with self.subTest(payload=name, surface=surface):
                    outcome = self._make_outcome(
                        snippet=f"body {payload} text" if surface == "snippet" else "body text",
                        path=f"/tmp/note{payload}.md" if surface == "filename" else "/tmp/note.md",
                        query=f"q{payload}" if surface == "query" else "q",
                    )
                    for colour in (False, True):
                        rendered = render_human(outcome, colour=colour, width=80)
                        # The escape itself must be gone. Leftover plain text
                        # like "]0;PWNED" is inert once ESC and BEL are stripped.
                        self.assertNotIn(self.ESC + "]", rendered)
                        self.assertNotIn(chr(0), rendered)
                        self.assertNotIn(chr(7), rendered)
                        self.assertNotIn(chr(127), rendered)
                        self.assertNotIn(chr(0x9B), rendered)
                        if not colour:
                            # With colour off nothing at all may emit an escape.
                            self.assertNotIn(self.ESC, rendered)


if __name__ == "__main__":
    unittest.main()
