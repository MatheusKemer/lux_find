# Security policy

## Reporting

Report a vulnerability privately through GitHub's
[security advisory form](https://github.com/MatheusKemer/lux_find/security/advisories/new)
for this repository. Please do not open a public issue for anything that would
expose someone's data before a fix exists.

Include: what you ran, what you expected, what happened.

Expect a first reply within a week. This is a small project maintained by one
person; there is no bounty programme.

## Threat model

`lux_find` runs entirely on your machine. It has no network code: no HTTP
client, no socket, no telemetry, no update check. Verify with:

```bash
grep -rnE '^[[:space:]]*(import|from)[[:space:]]+(socket|ssl|urllib|http|requests|httpx|aiohttp)\b' src/
```

CI runs the same check and fails the build if it ever matches.

What it *does* have is an index file containing text from your private
documents. That file is exactly as sensitive as the documents themselves.

- The index lives at `~/.lux_find/index.sqlite` by default and is created
  `0600`, readable only by you — it holds the plain text of everything you
  pointed it at, so a world-readable index would hand your whole corpus to any
  other account on the machine. It is **not encrypted**: if your disk is not
  encrypted, neither is your index.
- Files that look like they exist only to hold a credential — `.env` and
  `.env.*`, `id_rsa` and friends, `*.pem`, `*.key`, `.netrc`, `credentials.*`,
  `secrets.*`, including with an extension appended (`id_rsa.bak`) — are
  skipped **by name** and never opened, by every connector.
- That rule matches names, not contents. **A credential pasted into an ordinary
  note is ordinary text and will be indexed.** Nothing scans your documents for
  secrets, and nothing redacts them. Anything you point a source at is read and
  indexed as-is: review the `lux_find.toml` before running `index`, and do not
  point it at a folder you would not `grep`.
- **Indexed content is untrusted input.** A note, a scraped page or a chat
  export was not necessarily written by you. Terminal control sequences are
  stripped from human output so a file cannot repaint your screen, but if you
  feed `--json` results to an agent, treat the text as data — it can contain
  instructions aimed at that agent, and this tool has no way to tell them apart
  from your own notes.
- A source indexes what it points at and nothing else: a symlink whose target
  resolves outside the source root is skipped and counted, and directory
  symlinks are never followed.
- Delete the index by deleting the file. There is no other state.
