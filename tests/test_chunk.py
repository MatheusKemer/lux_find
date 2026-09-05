import unittest

import helpers  # noqa: F401  (puts src/ on sys.path)
from lux_find.chunk import (  # noqa: E402
    MAX_CHUNK_CHARS, chunk_code, chunk_for_kind, chunk_markdown, chunk_plain,
)


class TestMarkdownChunking(unittest.TestCase):
    def test_splits_on_headings(self):
        text = (
            "# Title\n" + "intro line\n" * 30 +
            "## Second\n" + "body line\n" * 30 +
            "## Third\n" + "more body\n" * 30
        )
        chunks = chunk_markdown(text)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(any(c[1].startswith("## Second") for c in chunks))

    def test_line_numbers_point_at_the_heading(self):
        text = "# One\n" + "a line of prose\n" * 60 + "# Two\nsecond section body\n"
        chunks = chunk_markdown(text)
        second = [c for c in chunks if c[1].startswith("# Two")]
        self.assertTrue(second)
        line, _ = second[0]
        self.assertEqual(text.splitlines()[line - 1], "# Two")

    def test_tiny_sections_are_not_split(self):
        chunks = chunk_markdown("# A\nshort\n## B\nalso short\n")
        self.assertEqual(len(chunks), 1)

    def test_headings_inside_a_fence_do_not_split(self):
        text = "# Doc\n" + "prose\n" * 40 + "```\n# not a heading\n" + "code\n" * 40 + "```\n"
        chunks = chunk_markdown(text)
        self.assertFalse(any(c[1].lstrip().startswith("# not a heading") for c in chunks))

    def test_long_section_is_wrapped_with_overlap(self):
        text = "# Big\n" + "".join(f"line {i} of a very long section\n" for i in range(400))
        chunks = chunk_markdown(text)
        self.assertGreater(len(chunks), 1)
        for _, body in chunks:
            self.assertLessEqual(len(body), MAX_CHUNK_CHARS + 200)
        # overlap: consecutive chunks share at least one line
        first_tail = set(chunks[0][1].splitlines()[-3:])
        second_head = set(chunks[1][1].splitlines()[:3])
        self.assertTrue(first_tail & second_head)


class TestCodeChunking(unittest.TestCase):
    def test_window_and_overlap(self):
        text = "".join(f"line{i}\n" for i in range(200))
        chunks = chunk_code(text, window=50, overlap=10)
        self.assertEqual(chunks[0][0], 1)
        self.assertEqual(chunks[1][0], 41)

    def test_rejects_bad_window(self):
        with self.assertRaises(ValueError):
            chunk_code("a\nb\n", window=5, overlap=5)

    def test_empty_input(self):
        self.assertEqual(chunk_code(""), [])
        self.assertEqual(chunk_markdown(""), [])
        self.assertEqual(chunk_plain(""), [])

    def test_dispatch(self):
        self.assertEqual(chunk_for_kind("# a\ntext\n", "markdown"), chunk_markdown("# a\ntext\n"))
        self.assertEqual(chunk_for_kind("x = 1\n", "code"), chunk_code("x = 1\n"))
        self.assertEqual(chunk_for_kind("hello\n", "plain"), chunk_plain("hello\n"))


if __name__ == "__main__":
    unittest.main()
