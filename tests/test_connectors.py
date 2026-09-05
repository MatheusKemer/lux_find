import tempfile
import unittest
from pathlib import Path

from helpers import make_corpus, write_chat_exports  # noqa: E402
from lux_find import connectors  # noqa: E402
from lux_find.config import Source  # noqa: E402
from lux_find.connectors.base import Connector, DocRef  # noqa: E402
from lux_find.connectors.files import looks_like_credential_file  # noqa: E402


class TestFilesConnector(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        make_corpus(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_respects_include_and_exclude(self):
        source = Source(path=str(self.base / "notes"), kind="notes", exclude=["recipes/**"])
        refs = list(connectors.for_kind("notes").scan(source))
        titles = {r.title for r in refs}
        self.assertIn("index.md", titles)
        self.assertNotIn("recipes/bread.md", titles)

    def test_readme_in_a_code_tree_is_classified_as_notes(self):
        source = Source(path=str(self.base / "project"), kind="code")
        refs = {r.title: r for r in connectors.for_kind("code").scan(source)}
        self.assertEqual(refs["README.md"].kind, "notes")
        self.assertEqual(refs["app/queue.py"].kind, "code")

    def test_signature_changes_with_content(self):
        source = Source(path=str(self.base / "notes"), kind="notes")
        connector = connectors.for_kind("notes")
        before = {r.uri: r.signature for r in connector.scan(source)}
        target = self.base / "notes" / "index.md"
        target.write_text("# Home\n\nrewritten with more text than before\n", encoding="utf-8")
        import os
        import time
        os.utime(target, (time.time() + 5, time.time() + 5))
        after = {r.uri: r.signature for r in connector.scan(source)}
        key = str(target.resolve())
        self.assertNotEqual(before[key], after[key])

    def test_credential_files_are_never_opened(self):
        for name in (".env", ".env.local", "id_rsa", "server.pem", "deploy.key"):
            self.assertTrue(looks_like_credential_file(Path("/x") / name), name)
        for name in ("notes.md", "keyboard.md", "environment.md"):
            self.assertFalse(looks_like_credential_file(Path("/x") / name), name)

    def test_credential_file_is_skipped_by_the_scan(self):
        (self.base / "notes" / ".env").write_text("SECRET=abc123456789\n", encoding="utf-8")
        source = Source(path=str(self.base / "notes"), kind="notes", include=["*"])
        titles = {r.title for r in connectors.for_kind("notes").scan(source)}
        self.assertNotIn(".env", titles)

    def test_binary_file_yields_no_chunks(self):
        blob = self.base / "notes" / "picture.md"
        blob.write_bytes(b"\x00\x01\x02" * 500)
        source = Source(path=str(self.base / "notes"), kind="notes")
        connector = connectors.for_kind("notes")
        ref = next(r for r in connector.scan(source) if r.title == "picture.md")
        self.assertEqual(connector.load(ref), [])

    def test_missing_source_raises(self):
        source = Source(path=str(self.base / "nope"), kind="notes")
        with self.assertRaises(FileNotFoundError):
            list(connectors.for_kind("notes").scan(source))


class TestChatConnector(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        write_chat_exports(self.base / "chats")
        self.source = Source(path=str(self.base / "chats"), kind="chat")
        self.connector = connectors.for_kind("chat")

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_finds_both_shapes(self):
        refs = list(self.connector.scan(self.source))
        titles = {r.title for r in refs}
        self.assertIn("session.jsonl", titles)
        self.assertIn("Choosing a queue", titles)

    def test_jsonl_messages_become_chunks_numbered_by_turn(self):
        ref = next(r for r in self.connector.scan(self.source) if r.title == "session.jsonl")
        chunks = self.connector.load(ref)
        self.assertEqual([c[0] for c in chunks], [1, 2, 3])
        self.assertTrue(chunks[0][1].startswith("user:"))
        self.assertIn("tombstone", chunks[1][1])

    def test_tool_blocks_are_summarised_not_dumped(self):
        ref = next(r for r in self.connector.scan(self.source) if r.title == "session.jsonl")
        chunks = self.connector.load(ref)
        self.assertIn("[tool Read]", chunks[2][1])

    def test_json_export_conversation_is_ordered(self):
        ref = next(r for r in self.connector.scan(self.source) if r.title == "Choosing a queue")
        chunks = self.connector.load(ref)
        self.assertEqual(len(chunks), 2)
        self.assertIn("log or a queue", chunks[0][1])
        self.assertIn("append-only log", chunks[1][1])

    def test_generic_jsonl_with_a_text_field(self):
        path = self.base / "chats" / "generic.jsonl"
        path.write_text(
            '{"role": "note", "text": "a plain jsonl line with a text field"}\n',
            encoding="utf-8",
        )
        ref = next(r for r in self.connector.scan(self.source) if r.title == "generic.jsonl")
        chunks = self.connector.load(ref)
        self.assertEqual(len(chunks), 1)
        self.assertIn("plain jsonl line", chunks[0][1])

    def test_malformed_lines_are_skipped_not_fatal(self):
        path = self.base / "chats" / "broken.jsonl"
        path.write_text(
            'not json at all\n{"text": "this one is fine"}\n{"broken": \n',
            encoding="utf-8",
        )
        ref = next(r for r in self.connector.scan(self.source) if r.title == "broken.jsonl")
        chunks = self.connector.load(ref)
        self.assertEqual(len(chunks), 1)


class TestRegistry(unittest.TestCase):
    def test_known_kinds(self):
        self.assertEqual(set(connectors.REGISTRY), {"notes", "code", "chat"})
        self.assertEqual(connectors.for_kind("notes").name, "files")
        self.assertEqual(connectors.for_kind("chat").name, "chat-jsonl")

    def test_unknown_kind_raises(self):
        with self.assertRaises(KeyError):
            connectors.for_kind("telepathy")

    def test_a_custom_connector_is_about_thirty_lines(self):
        """The extension point works with a plain subclass - no plugin system."""

        class MemoryConnector(Connector):
            name = "memory"

            def scan(self, source):
                yield DocRef(
                    uri="memory://one",
                    source_path="memory://one",
                    title="in-memory document",
                    kind="notes",
                    signature="v1",
                    chunker="plain",
                )

            def load(self, ref):
                return [(1, "a document that never touched the disk")]

        connector = MemoryConnector()
        ref = next(iter(connector.scan(None)))
        self.assertEqual(connector.load(ref)[0][1], "a document that never touched the disk")


if __name__ == "__main__":
    unittest.main()
