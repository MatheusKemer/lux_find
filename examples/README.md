# Using lux_find from an agent

The most frequent user of a personal search index is not a person typing — it is
a coding agent that would otherwise re-derive something you already wrote down.
This assumes `lux-find index` runs on a schedule (see the README).

`lux_find_hook.sh` is the whole integration: query the index, print context, get
out of the way. Whatever the agent, the pattern is the same three steps.

1. Run `lux-find find "<the user's question>" --json --limit 5`.
2. Check the exit code. `0` means hits, `1` means none, `3` means the index is
   broken and the agent should say so rather than silently continue. An agent
   that treats "no hits" and "index is corrupt" the same way will confidently
   tell you there is nothing in your notes about the thing you definitely wrote
   down last March.
3. Feed the snippets in as context, with their paths, so the agent can cite
   where an answer came from and open the file if it needs more.

Keep the limit small. Five results is usually plenty and costs a few hundred
tokens; twenty costs thousands and rarely changes the answer.

## Claude Code

Add a `UserPromptSubmit` hook so every prompt is preceded by whatever your own
notes and code already say about it. The hook's stdout is added to the context.
Paste into `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/examples/lux_find_hook.sh"
          }
        ]
      }
    ]
  }
}
```

Make it executable first: `chmod +x examples/lux_find_hook.sh`.

Claude Code hands the prompt to the hook as a JSON event on **stdin**, not as an
environment variable, which is why the command above takes no argument — the
script reads either shape. Check it by hand before wiring it up:

```bash
echo '{"prompt": "why did we choose a tombstone"}' | examples/lux_find_hook.sh
```

A hook that runs on every prompt is a tax you pay on every turn, so keep the
output small — `--limit 5` and nothing else. If it only fires usefully on some
prompts, gate it at the top of the script:

```bash
case "$query" in
  *how*|*why*|*where*|*did\ we*|*remember*) ;;
  *) exit 0 ;;
esac
```

(put it after the block that resolves `$query`, not at the very top).

## Anything else

Cursor, Continue, Aider, a Makefile, a shell alias: if it can run a command and
read stdout, it can use `lux_find_hook.sh` unchanged. If you would rather ask
explicitly than pay on every turn, wire it to a slash command instead of a hook.
