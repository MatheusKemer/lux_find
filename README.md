# lux_find

[![tests](https://github.com/MatheusKemer/lux_find/actions/workflows/tests.yml/badge.svg)](https://github.com/MatheusKemer/lux_find/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](pyproject.toml)

**Full-text search over the notes, code and chats you already have.
One SQLite file. No server, no cloud, no dependencies.**

📖 [Architecture walkthrough](https://matheuskemer.github.io/lux_find/)

You have thousands of documents you wrote yourself: markdown notes, a few
repositories, an exported conversation history with a coding agent. The answer
to "how did we decide that?" is almost always already on your disk, and almost
always unfindable — because `grep` needs the exact word, and the search box in
your editor stops at the current project.

`lux_find` indexes all of it into one SQLite FTS5 database and answers in
milliseconds. It is about 2,400 lines of standard-library Python — comments and
docstrings included — with 150 tests and zero runtime dependencies. You can read
all of it in an afternoon, which is the point: this is infrastructure you should
be able to audit before pointing it at your private files.

```console
$ lux-find find "why tombstone instead of hard delete"
lux_find "why tombstone instead of hard delete" - 8 hits - 1.5 ms - confidence medium

1. ~/exports/agent-session.jsonl:1  [chat] - today
   user: <<why>> did we pick a <<tombstone>> <<instead>> of a <<hard>> <<delete>>?

2. ~/notes/design/sync-protocol.md:17  [note] - today
   ## Rejected alternative  We chose a <<tombstone>> <<instead>> of a <<hard>>
   <<delete>> because a <<hard>> <<delete>> cannot ...
```

> Demoed at AI Tinkerers Curitiba (26 August 2026) as *"Buscador Local: Salve
> Tudo"* — local search over everything you save — and picked up by the
> AI Tinkerers global Community Spotlights newsletter. This repository is the
> generic engine from that demo, rebuilt as a standalone tool called `lux_find`.

---

## 60-second quickstart

Requires Python 3.11 or newer. Nothing else.

(`lux-find` with a hyphen installs the same package - PyPI treats `_`
and `-` as the same name. The installed command is `lux-find`.)

```bash
pip install lux_find                 # or: pipx install lux_find

lux-find init ~/notes ~/src/my-project ~/exports
lux-find index
lux-find find "the thing you half remember"
```

`init` writes a `lux_find.toml` next to you and guesses a *kind* for each path by
looking at what is actually inside it. `index` builds `~/.lux_find/index.sqlite`.
`find` searches it. `status` tells you how fresh it is. That is the whole tool —
four commands.

Running from a clone, with or without installing, works too:

```bash
git clone https://github.com/MatheusKemer/lux_find
cd lux_find
pip install .                        # or skip it and use PYTHONPATH:
PYTHONPATH=src python3 -m lux_find init ~/notes
PYTHONPATH=src python3 -m lux_find index
PYTHONPATH=src python3 -m lux_find find "hello"
```

### The config

```toml
[index]
db = "~/.lux_find/index.sqlite"
stale_hours = 24

[[source]]
path = "~/notes"
kind = "notes"
include = ["*.md", "*.txt"]
exclude = ["archive/**"]

[[source]]
path = "~/src/my-project"
kind = "code"

[[source]]
path = "~/exports"
kind = "chat"
```

Add as many `[[source]]` blocks as you like; a `path` may be a directory or a
single file. `include` and `exclude` are glob patterns, always written with
forward slashes, matched against the path relative to the source root. See
`lux_find.example.toml`.

`find`, `index` and `status` look for the config in this order: `--config/-c` if
you passed one, then `$LUX_FIND_CONFIG`, then `./lux_find.toml`, then
`~/.lux_find/lux_find.toml`. `--db` overrides the database path from wherever you
are standing.

---

## The whole command line

```
lux-find init <path> [<path> ...] [--kind notes|code|chat] [--force]
lux-find index [--full] [--force-prune] [-q|--quiet]
lux-find find "<query>" [-n N] [-k notes|code|chat] [-a] [--json] [--why]
lux-find status [--json]
```

Every command also takes `-c/--config <file>` and `--db <file>`, and
`lux-find --version` prints the version.

| flag | what it does |
|---|---|
| `init --kind` | force a kind instead of guessing from the directory contents |
| `init --force` | overwrite an existing `lux_find.toml` |
| `index --full` | rebuild from scratch. Atomic: readers keep seeing the old index until the new one is complete |
| `index --force-prune` | drop documents whose backing file is gone (see below) |
| `index -q` | print only the machine-readable `SUMMARY` line |
| `find -n` | maximum results, default 8 |
| `find -k` | restrict to a kind; repeat the flag to allow several |
| `find -a` | require every term, instead of ranking anything that matches |
| `find --why` | print the score breakdown for every hit |

`$NO_COLOR` disables colour, as does piping the output anywhere. A query that
starts with a dash is searched as written — `lux-find find -hello` looks for
`-hello` — and `--` before it still works if you prefer to be explicit.

---

## How it reads your data

Each source is handled by a **connector**. There are two, and they cover the
three kinds:

| kind | connector | what it reads | how it chunks |
|---|---|---|---|
| `notes` | `files` | `.md`, `.mdx`, `.markdown`, `.rst`, `.txt`, `.org`, `.adoc` | by heading, then wrapped with overlap |
| `code` | `files` | 36 source extensions, plus the repository's own markdown | sliding window of 70 lines, 12 overlapping |
| `chat` | `chat-jsonl` | agent transcripts (`*.jsonl`), ChatGPT data exports (`conversations.json`), any JSONL with a `text`/`content` field | one chunk per message |

A chunk is the unit that gets ranked and shown, which is why results carry a
line number: `notes/design/sync-protocol.md:142`, not just a filename. The number
points at the line that actually matched, not at the top of the chunk. For chat
documents it is the **message number** in the conversation.

Writing a connector for something else — an email archive, a bookmark dump, an
issue tracker export — is about thirty lines. See
[docs/CONNECTORS.md](docs/CONNECTORS.md).

### What gets left out, and how you find out

Some files are deliberately not indexed:

- files whose **name** says they exist to hold a credential (`.env` and
  friends, `id_rsa`, `*.pem`, `*.key`, `*.p8`, `*.ppk`, `.netrc`, `kubeconfig`,
  a git credential store, a cloud service-account JSON, `secrets.yaml` —
  including with an extension glued on, like `id_rsa.bak`). A note *about*
  secrets is not one: `secrets-rotation.md` stays in your index;
- files larger than **4 MB**, empty files, and files that sniff as binary;
- anything matched by an `exclude` pattern (`.git/**`, `node_modules/**`,
  `dist/**` and similar are excluded by default).

None of that happens silently. `index` prints a `left out:` line with a count
per reason, and the same counts are in the `SUMMARY` JSON under `excluded`:

```console
$ lux-find index
indexed 1,204 documents / 5,880 chunks (312 unchanged, 4 duplicates, 0 pruned) in 1.31s
  left out: 3 credential-named, 1 over-size-limit, 12 empty
```

A file that could not be *read* — an unreadable directory, a permission error —
is not a skip. It is an error: it appears in `errors`, the run exits `2`, and
**nothing is pruned**, because deleting documents you failed to read is how an
index quietly loses content.

---

## Privacy

**Everything stays on disk. There are no network calls.** There is no HTTP
client, no socket, no telemetry, no update check anywhere in the package, and
CI fails the build if a network import ever appears in `src/`:

```bash
grep -rnE '^\s*(import|from)\s+(socket|ssl|urllib|http|requests|httpx|aiohttp)\b' src/
```

The credential skip above is a **filename** rule, and it is worth being precise
about what that does and does not buy you: those files are never opened, but a
password pasted into an ordinary note is ordinary text, and it is indexed like
everything else. The index contains whatever your files contain, so exclude
folders you would not `grep`.

The database is created `0600` — owner only — because it holds the plain text of
every source you pointed it at. See [SECURITY.md](SECURITY.md).

---

## Using it from an agent

The main consumer of a personal search index is usually not a person typing —
it is a coding agent that would otherwise re-derive what you already wrote down.
`--json` exists for that, and the exit codes are part of the contract:

| code | meaning |
|---|---|
| `0` | at least one hit |
| `1` | the query ran and matched nothing |
| `2` | usage error: bad flags, missing config, unreadable source — or the index is locked by another run, which is transient and harmless |
| `3` | index missing, corrupt, or written by a different schema version — the database path is printed |

The separate code for "index is broken" matters more than it looks. An empty
result list reads as a valid verdict; if a broken index also returned zero,
every caller would silently conclude *there is nothing about that* and move on.
Same reason a stale index prints a warning to stderr instead of quietly serving
last month's answers.

```console
$ lux-find find "rate limit ceiling" --json | jq '.hits[0]'
{
  "uri": "/home/you/notes/design/rate-limits.md",
  "path": "/home/you/notes/design/rate-limits.md",
  "title": "design/rate-limits.md",
  "kind": "notes",
  "line": 24,
  "snippet": "The per-tenant <<ceiling>> is enforced at the edge ...",
  "score": 4.12,
  "bm25": 3.97,
  "boosts": { "title": 0.15 },
  "mtime": 1756900000.0
}
```

The object around `hits` carries `query`, `count`, `ms`, `confidence`,
`candidates`, `collapsed`, `db`, `built_at`, `age_hours` and `stale`. `--json`
writes nothing but JSON to stdout — warnings go to stderr — so it is safe to
pipe, including when the query matched nothing.

**`--json` stays JSON when the command fails**, including on exit `2` and `3`.
The failure object carries `error`, `exit_code` and usually `hint`:

```console
$ lux-find find alpha --db /gone.sqlite --json; echo "exit=$?"
{"error": "no index at /gone.sqlite", "exit_code": 3, "hint": "run `lux-find index` to (re)build it; delete the file to start over."}
exit=3
```

It deliberately has **no `count` and no `hits`**. A caller doing
`result["count"]` raises instead of reading a zero nobody measured — a failure
that looks like an empty result set is worse than one that crashes the caller,
because nobody ever investigates it.

**A query that starts with a dash is a query.** `lux-find find -hello` searches
for `-hello`; it is not parsed as flags. `--` still works if you prefer to be
explicit. And `--limit` refuses anything below `1` rather than returning an
empty list for a query that has hits.

A ready-made hook is in [examples/](examples/): a short shell wrapper that
consults the index before the agent answers, plus the Claude Code settings
snippet that wires it up. It reads the question from an argument or from a JSON
event on stdin, so anything that can run a shell command can use it.

---

## Performance

Measured on an Apple-silicon laptop (macOS 26.5, Python 3.12, SQLite 3.49) over
a generated corpus of dense synthetic prose — a deliberately unfriendly shape,
where every query term matches a large fraction of the corpus. Your own notes
are more selective and will usually be faster.

| corpus | index size | cold build | warm re-index | query median | p90 |
|---|---|---|---|---|---|
| 5,000 docs / 17,500 chunks | 27 MB | 0.8 s | 0.14 s | **23 ms** | 27 ms |
| 20,000 docs / 70,000 chunks | 109 MB | 3.0 s | 0.42 s | **54 ms** | 61 ms |

Query time grows with the size of the corpus rather than staying flat, and the
first query after a fresh build pays a one-off disk-cache cost. Both are visible
in the header `find` prints, which is the point of printing it.

You do not need a benchmark harness to check this on your own corpus: `index`
prints its own elapsed time, `find` prints the query time in its header, and
`status` prints the index size.

The warm re-index is the number that matters day to day: nothing whose
`mtime:size` is unchanged gets re-read, so keeping a large index fresh costs
well under a second. Keep it fresh with whatever your machine already runs:

```bash
# cron
*/15 * * * * /usr/local/bin/lux-find index --quiet >> /tmp/lux_find.log 2>&1

# systemd timer, launchd agent, Windows Task Scheduler
lux-find index --quiet
```

`index` is idempotent — running it twice changes nothing.

---

## How queries are matched

This is the part most likely to surprise you, so it is written out in full.

**Your words are OR-ed, not AND-ed.** `sync backpressure queue` returns
documents containing *any* of those words. That is deliberate: FTS5 computes
real inverse document frequency, so a document matching only the rare word
outranks one matching only the common word, and adding a word can never make
the search return nothing.

**Quote a phrase, star a prefix.** `"tombstone expiry"` matches those two words
next to each other, in that order. `tombston*` matches any word starting with
that. `--all` (`-a`) switches the whole query from "rank anything that matches"
to "only documents containing every term".

**Other punctuation is not syntax.** FTS5's own operators do not apply: `AND`,
`OR`, `NOT` and `NEAR(...)` are searched as literal words. Words of one
character are dropped, and at most 12 terms are used.

**Accents fold, word forms do not.** `acao` finds `ação` and vice versa, and
text is normalised so a decomposed accent typed on one machine matches a
composed one written on another. But there is no stemming: `tombstone` does not
find `tombstones`, and `expiry` does not find `expires`.

**The name of the file counts as text.** A document is findable by the words in
its own filename even when the body never repeats them, so
`kubernetes-upgrade.md` answers a search for `kubernetes upgrade`. The name is
indexed in its own field, weighted below the body, and never appears in the
snippet.

---

## How the ranking works

FTS5 computes BM25 over the chunk text. That is the engine. On top of it there
are exactly two boosts, both structural:

- a query term appearing in the file's own name or title (if you named a file
  after the thing, that is the strongest signal a human ever leaves behind);
- a small nudge for anchor filenames (`README`, `CHANGELOG`, `index`,
  `CONTRIBUTING`, `OVERVIEW`, `ARCHITECTURE`).

That is all. No learned weights, no personalisation, no re-ranking model. Pass
`--why` and the score breakdown is printed for every hit, so you can check the
ranker's work:

```console
$ lux-find find "storage layout" --why
2. ~/notes/reference/storage-layout-31.md:1  [note] - today
   # <<Storage>> <<Layout>>  Pages, extents, and why the free list is a bitmap ...
   score 5.937 = bm25=5.337 title=+0.600
```

`confidence` in the header reports the *separation* between the top hit and the
runner-up. Low confidence does not mean "nothing found" — it means the index
could not pick a winner and you should add a rarer word. Thresholds: a single
hit (or no runner-up) is `high`; otherwise `high` at ≥25% separation, `medium`
at ≥8%, `low` below that.

---

## Behaviour notes

**Ranking is BM25 over FTS5, nothing else.** It is debuggable (`--why` prints
every point of every score), offline and free. If you want a semantic layer,
put it in front of `--json`; the tool does not prescribe one.

**Errors exit with a code, not an empty list.** A missing or corrupt database
exits `3` with the path printed; a source that could not be read is an error in
the `index` summary and exits `2`. No code path catches an exception and
returns `[]`.

**Pruning an entire source needs `--force-prune`.** A plain `lux-find index`
already prunes documents whose file is gone, but only when the run had no read
errors. If a source scans to zero documents while the index still holds many for
that root, the build stops and says so — that shape is usually a permissions
problem or a typo in an include pattern. `--force-prune` is the explicit door
when the files really are gone.

**Copies are collapsed, not counted twice.** Synced folders and backup
directories leave byte-identical files everywhere. Documents are deduplicated by
content hash at index time (shortest path wins, so `design.md` beats
`design (copy).md`), and near-identical result lines are collapsed again at
query time. The header names the count — `... confidence high (+3 similar
collapsed)` — and `--json` carries the same number as `"collapsed"`. If the copy
that held the chunks is deleted, the survivor is promoted in the same run rather
than being left as a document with no text.

**Interrupting a build costs the last few hundred documents, not the run.**
`index` checkpoints as it goes. `--full` is the exception and is one
transaction, so that a rebuild is never visible to a reader as an empty index.

**One TOML parser, not two.** `tomllib` has been in the standard library since
Python 3.11, so that is the floor. Vendoring a second parser to reach 3.10 would
mean two implementations of the same file format, and two parsers eventually
disagree about your config.

---

## Limitations

Things this does not do, stated plainly so you can decide before installing:

- **No semantic search, and no stemming.** Synonyms do not match ("car" will not
  find "automobile") and neither do word forms. FTS5 indexes the words you
  actually wrote. In practice you type a word from the document, not a word
  about it.
- **No boolean operators and no fuzzy matching.** Phrases and prefixes work;
  `AND`/`OR`/`NEAR` do not, and neither does a typo. See *How queries are
  matched* above.
- **No PDFs, images, or office documents.** Text files only. A connector could
  add them; none ships.
- **Files over 4 MB are not indexed.** They are counted and reported, not hidden.
- **Single machine, single user.** There is no server, no sync, no multi-user
  story. The index is a file; copy it if you want it elsewhere.
- **Chat support covers the shapes described above.** Other export formats need
  a connector — usually a small one.
- **No incremental awareness of file *moves*.** A renamed file is indexed as new
  and the old entry is pruned — correct, just not clever. The content stays
  searchable throughout.
- **Tested on macOS and Linux.** The code avoids POSIX-only assumptions and CI
  runs both, but Windows is not covered by CI and is therefore unverified.

Nothing is announced here before it exists.

---

## Contributing

Issues and pull requests welcome. Tests run with no dependencies:

```bash
python3 -m pytest    # or: PYTHONPATH=src:tests python3 -m unittest discover -s tests -t tests
```

Three requirements, and CI enforces the first two:

1. **No runtime dependencies.** The standard library is the whole toolbox.
2. **No network calls.** Not for updates, not for telemetry, not optionally
   behind a flag.
3. **No bare `except: pass`.** Errors surface in the summary or the exit code.

A bug fix comes with a test for the *class* of the bug, not only the input that
exposed it — `tests/test_regressions.py` is that file, and every case in it is a
defect this tool once had.

Test fixtures are generated at runtime, never committed, and fake credentials
are assembled from parts — this repository contains nothing shaped like a real
key.

## License

MIT. See [LICENSE](LICENSE).
