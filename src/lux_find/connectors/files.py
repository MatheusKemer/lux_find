"""``files`` connector — plain files on disk (notes and code).

Walks the source root, applies include/exclude globs, and skips the obvious
non-text: binaries, very large files, and files whose *name* says they exist to
hold credentials (``.env``, ``id_rsa``, ``*.pem``). Those are never opened at
all — there is no point reading a file that exists only to hold a key.
"""

from __future__ import annotations

import fnmatch
import os
import unicodedata
from pathlib import Path
from typing import Iterator

from ..config import CODE_EXTS, NOTE_EXTS
from .base import Connector, DocRef, ScanReport

__all__ = ["FilesConnector", "looks_like_credential_file"]

MAX_FILE_BYTES = 4 * 1024 * 1024

CREDENTIAL_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "htpasswd",
    ".npmrc", ".pypirc", ".netrc", ".htpasswd",
    # Whole-file credential stores that carry no telltale extension: a git
    # credential store is plaintext user:password, and a kubeconfig embeds
    # cluster tokens and client keys.
    ".git-credentials", "git-credentials", "kubeconfig",
}
CREDENTIAL_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".asc",
    # .p8 is an Apple service key, .ppk a PuTTY private key - both are the
    # private half, both are routinely left in a project folder.
    ".p8", ".ppk",
)

# A file *named* after secrets is a store; a note *about* secrets is a note.
# "secrets.yaml" is the first, "secrets-rotation.md" is the second, and the
# difference has to survive: skipping the note would drop the user's own
# writing from their index without ever saying so.
SECRET_STEMS = (
    "secret", "secrets", "credential", "credentials",
    # A cloud service-account file is a private key in JSON clothing; the name
    # is the only warning it ever gives.
    "service-account", "service_account", "serviceaccount",
)
SECRET_STORE_EXTS = {
    "", ".txt", ".yml", ".yaml", ".toml", ".json", ".jsonl", ".ini", ".conf", ".cfg",
}


def looks_like_credential_file(path: Path) -> bool:
    """True when a file's *name* says it exists to hold a credential.

    Matching is on the name alone - the file is never opened, which is the
    whole point. A glued-on extension does not make a key file safe: an export
    folder is exactly where ``id_rsa.jsonl`` or ``server.pem.bak`` shows up, so
    the leading name is checked as well as the full one.
    """
    name = path.name.lower()
    if name.startswith(".env") or name.endswith(".env"):
        return True

    parts = name.split(".")
    candidates = {name, parts[0]}
    if name.startswith(".") and len(parts) > 1:
        candidates.add("." + parts[1])
    if candidates & CREDENTIAL_NAMES:
        return True

    if name.endswith(CREDENTIAL_SUFFIXES):
        return True
    if any(suffix.lower() in CREDENTIAL_SUFFIXES for suffix in path.suffixes):
        return True

    if path.suffix.lower() in SECRET_STORE_EXTS:
        stem = parts[0]
        if stem in SECRET_STEMS:
            return True
        if any(stem.endswith(sep + word) for sep in ("-", "_", ".") for word in SECRET_STEMS):
            return True
    return False


def _matches(rel: str, patterns: list[str]) -> bool:
    # Patterns in a config are always written with forward slashes. os.walk
    # hands back the platform separator, so "docs/**" would never match on
    # Windows unless the candidate is normalised first.
    rel = rel.replace(os.sep, "/")
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        # "dir/**" should also match "dir/a/b.md" and "dir" itself.
        if pattern.endswith("/**") and (rel == pattern[:-3] or rel.startswith(pattern[:-2])):
            return True
    return False


_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
)


def _encoding_from_bom(head: bytes) -> str | None:
    """Return the encoding a byte-order mark declares, if there is one."""
    for mark, encoding in _BOMS:
        if head.startswith(mark):
            return encoding
    return None


def _is_probably_binary(head: bytes) -> bool:
    if b"\x00" in head:
        return True
    # Heuristic: a lot of bytes outside printable/UTF-8 continuation ranges.
    if not head:
        return False
    weird = sum(1 for b in head if b < 9 or (13 < b < 32))
    return weird / len(head) > 0.05


class FilesConnector(Connector):
    name = "files"

    def scan(self, source, report: ScanReport | None = None) -> Iterator[DocRef]:
        root = source.root
        report = report if report is not None else ScanReport()
        if not root.exists():
            raise FileNotFoundError(f"source path does not exist: {root}")

        include = source.effective_include()
        exclude = source.effective_exclude()

        if root.is_file():
            # A single file is a legal source (`lux-find init notes.md`).
            # os.walk() over a file yields nothing at all, which made the whole
            # source index to zero documents and still report success.
            ref = self._ref_for(root, root.name, source, report)
            if ref is not None:
                yield ref
            return

        def walk_error(exc: OSError) -> None:
            # os.walk swallows these by default. Swallowing here would let one
            # unreadable subdirectory prune every document under it from the
            # index, with a clean exit code and no message: the exact silent
            # zero this tool exists to avoid.
            report.fail(f"{getattr(exc, 'filename', root)}: cannot read directory: {exc}")

        # followlinks stays off: a symlink loop inside a notes folder would walk
        # forever, and a link out of the tree indexes files the source never named.
        for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [
                d
                for d in dirnames
                if not _matches(os.path.join(rel_dir, d) if rel_dir else d, exclude)
                and not d.startswith(".git")
            ]
            for filename in filenames:
                rel = os.path.join(rel_dir, filename) if rel_dir else filename
                if _matches(rel, exclude) or not _matches(rel, include):
                    continue
                path = Path(dirpath) / filename
                ref = self._ref_for(path, rel, source, report)
                if ref is not None:
                    yield ref

    def _ref_for(self, path: Path, rel: str, source, report: ScanReport) -> "DocRef | None":
        """Apply the skip policy to one file and describe it, or explain why not."""
        # os.walk(followlinks=False) stops directory recursion but still hands
        # back symlinked *files*, so a single link in a notes folder could pull
        # documents from anywhere on the disk into an index whose config never
        # named them. A source indexes what it points at, nothing else.
        if path.is_symlink():
            try:
                inside = path.resolve().is_relative_to(source.root.resolve())
            except OSError as exc:
                report.fail(f"{path}: cannot resolve symlink: {exc}")
                return None
            if not inside:
                report.skip("symlink-outside-source")
                return None

        if looks_like_credential_file(path):
            report.skip("credential-named")
            return None
        try:
            stat = path.stat()
        except OSError as exc:
            report.fail(f"{path}: cannot stat: {exc}")
            return None
        if stat.st_size == 0:
            report.skip("empty")
            return None
        if stat.st_size > MAX_FILE_BYTES:
            report.skip("over-size-limit")
            return None

        ext = path.suffix.lower()
        kind = source.kind
        if kind in ("notes", "code"):
            # A README inside a code tree is still notes, and a script inside a
            # notes folder is still code.
            if ext in NOTE_EXTS:
                kind = "notes"
            elif ext in CODE_EXTS:
                kind = "code"
        chunker = "markdown" if ext in NOTE_EXTS else ("code" if ext in CODE_EXTS else "plain")
        return DocRef(
            uri=str(path),
            source_path=str(path),
            title=rel,
            kind=kind,
            signature=f"{stat.st_mtime_ns}:{stat.st_size}",
            mtime=stat.st_mtime,
            size=stat.st_size,
            chunker=chunker,
        )

    def load(self, ref: DocRef) -> list[tuple[int, str]]:
        from ..chunk import chunk_for_kind

        path = Path(ref.source_path)
        with path.open("rb") as fh:
            head = fh.read(4096)
            encoding = _encoding_from_bom(head)
            # UTF-16 text is half null bytes, so the binary sniffer used to
            # throw away perfectly ordinary documents. A byte-order mark is a
            # positive statement that this is text; trust it over the heuristic.
            if encoding is None and _is_probably_binary(head):
                return []
            rest = fh.read()
        raw = head + rest
        text = raw.decode(encoding or "utf-8", "replace")
        # One normal form in the index and the same one in the query: on macOS a
        # file name or a note typed elsewhere arrives decomposed, so "acao" with
        # a combining cedilla would never match the composed word typed at the
        # prompt.
        text = unicodedata.normalize("NFC", text).lstrip("\ufeff")
        return chunk_for_kind(text, ref.chunker)
