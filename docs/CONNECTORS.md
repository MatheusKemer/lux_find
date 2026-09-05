# Writing a connector

A connector teaches `lux_find` to read one more kind of thing. The contract
is two methods, and a working one fits in about thirty lines.

## The contract

```python
class Connector:
    name = "my-connector"

    def scan(self, source, report: ScanReport | None = None) -> Iterator[DocRef]:
        """What documents exist, and has each one changed?"""

    def load(self, ref: DocRef) -> list[tuple[int, str]]:
        """Give me the (line_number, text) chunks of this one document."""
```

### ScanReport

`scan` is handed a `ScanReport` and is expected to use it. It has two methods,
and the difference between them is the whole point:

- `report.fail("<path>: cannot read: ...")` — something you *should* have been
  able to read and could not. The build reports it, exits `2`, and prunes
  nothing, because a document you failed to read must never be deleted from the
  index as though it were gone.
- `report.skip("over-size-limit")` — something you left out on purpose. It is
  counted per reason and printed in the summary, so the file does not simply
  vanish without explanation.

Never swallow an error to keep a scan tidy. A connector that silently returns
fewer documents turns "I could not read your notes" into "there is nothing in
your notes", which is the one answer this tool must never give.

The split is what makes incremental indexing possible. `scan` runs over
everything on every build and must be cheap; `load` runs only for documents
whose `signature` differs from what the index already holds.

### DocRef

| field | meaning |
|---|---|
| `uri` | stable identifier. Absolute path for files; `<path>#<inner-id>` for a container format. Re-indexing keys on this. |
| `source_path` | the file on disk backing this document. Used for display and for pruning. |
| `title` | what a human sees. Usually the path relative to the source root. |
| `kind` | `notes`, `code` or `chat`. Drives `--kind` filtering. |
| `signature` | any opaque string. Same string means "skip, already indexed". `mtime:size` is usually enough — use `st_mtime_ns`, not `int(st_mtime)`, or an edit made in the same second at the same size is never re-read. A content hash is fine if you can afford it. |
| `mtime` | float timestamp, shown as "3mo ago" in results. |
| `chunker` | `markdown`, `code` or `plain` — only relevant if you hand text to `chunk_for_kind`. |
| `extra` | free dict; `load` gets it back. Use it to remember which record inside a container this ref points at. |

`load` returns `(line_number, text)` pairs. The number is a coordinate a human
can act on: a file line for files, a message number for conversations, a page
number if you ever add PDFs. Getting it right is most of what makes a result
useful rather than merely correct.

## A complete example

```python
# src/lux_find/connectors/bookmarks.py
import json
from pathlib import Path
from .base import Connector, DocRef  # ScanReport too, if you type-annotate


class BookmarksConnector(Connector):
    """Index a browser bookmark export: one bookmark per document."""

    name = "bookmarks"

    def scan(self, source, report=None):
        for path in source.root.rglob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError as exc:
                if report is not None:
                    report.fail(f"{path}: cannot stat: {exc}")
                continue
            yield DocRef(
                uri=str(path),
                source_path=str(path),
                title=path.name,
                kind="notes",
                signature=f"{stat.st_mtime_ns}:{stat.st_size}",
                mtime=stat.st_mtime,
            )

    def load(self, ref):
        chunks = []
        for number, line in enumerate(Path(ref.source_path).read_text().splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            chunks.append((number, f"{item['title']}\n{item['url']}\n{item.get('note', '')}"))
        return chunks
```

Register it:

```python
# src/lux_find/connectors/__init__.py
from .bookmarks import BookmarksConnector

REGISTRY = {
    ...,
    "bookmarks": BookmarksConnector(),
}
```

Then add `bookmarks` to `DEFAULT_INCLUDE` in `config.py` so `kind = "bookmarks"`
validates, and use it:

```toml
[[source]]
path = "~/exports/bookmarks"
kind = "bookmarks"
```

## Things the framework already does for you

- **Deduplication.** Documents with identical content are collapsed by hash.
- **Pruning.** Anything you stop yielding from `scan` is removed from the index —
  unless your whole source went dark, in which case the prune guard stops the
  build instead of deleting everything.
- **Chunk size.** Use `chunk_for_kind(text, ref.chunker)` from
  `lux_find.chunk` if you have raw text and want the standard splitting.

## Things you must do yourself

- **Fail loudly.** If your source path does not exist, raise `FileNotFoundError`.
  Do not return an empty iterator — "I could not read it" and "there is nothing
  there" must never look the same.
- **Survive malformed input.** One broken line in an export should be skipped,
  not fatal. One broken *file* should raise, so the build reports it.
- **Skip binaries and anything absurdly large.** `scan` is the place for that.
- **Ship tests**: a well-formed input, a malformed one, an empty one.
