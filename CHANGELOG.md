# Changelog

## 0.2.0 — 2026-09-07

First release on PyPI: `pip install lux-find`.

### Queries do what the punctuation says

- A **quoted phrase** searches for the phrase, and a trailing **`*`** searches
  for the prefix. Both were previously stripped as punctuation, so `"distributed
  cache"` returned exactly what the loose words returned and `tombston*` looked
  for a word nobody had written.
- **`-a` / `--all`** requires every term instead of ranking anything that matches.

### A failure never looks like an empty result

This is one class of defect, and it is the one that matters most in a search
tool: an empty result set is a claim about your corpus, and the tool may only
make that claim after actually looking.

- **A query starting with a dash is a query.** `lux-find find -hello` used to be
  swallowed by argparse's short-option bundling: it printed help, exited `0`, and
  ignored `--json`, so a script read a success with no results and concluded the
  corpus had nothing. It now searches for `-hello`.
- **`--limit` below `1` is refused** instead of returning nothing. `-n -1` was
  sliced from the end by Python and produced an empty list for a query with real
  hits; `-n 0` did the same.
- **`--json` stays JSON when the command fails**, on exit `2` and `3`. stdout was
  previously empty, so a caller doing `json.loads` raised far away from the
  cause. The failure object carries `error`, `exit_code` and usually `hint` — and
  deliberately carries no `count` and no `hits`, so a caller reading `count`
  raises rather than trusting a zero nobody measured.
- **`index --force-prune` on a fresh config builds the index** instead of exiting
  `3`. Pruning what does not exist yet is a no-op, not an error.

### Credential files

- The name denylist now also covers git credential stores, `kubeconfig`, cloud
  service-account JSON, `*.p8` and `*.ppk`. A note *about* secrets
  (`secrets-rotation.md`) is still indexed — widening the net must not drop your
  own writing.

### Packaging

- Published from a tag via PyPI Trusted Publishing (OIDC): no API token exists
  anywhere. The tag must match the packaged version, and the built wheel is
  installed into a clean environment and made to answer a real search before
  anything is uploaded.

150 tests, still zero runtime dependencies.

## 0.1.0 — 2026-09-05

First public release. `init` → `index` → `find` → `status`, SQLite FTS5 + BM25,
notes/code/chat connectors, `--json` output, no network calls.
