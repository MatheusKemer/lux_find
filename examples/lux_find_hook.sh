#!/usr/bin/env bash
# Query the local index before the agent answers.
#
#   ./lux_find_hook.sh "the question"     # explicit
#   echo '{"prompt": "..."}' | ./lux_find_hook.sh   # agent hook on stdin
#
# Prints a short context block on stdout; prints nothing when there is no hit,
# and never exits non-zero, because a search miss must not block the agent.
set -uo pipefail

# Agents differ in how they hand over the prompt: some pass it as an argument,
# some write a JSON event to stdin (Claude Code's UserPromptSubmit sends
# {"prompt": "..."} that way). Accept both rather than guessing.
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
  query="$1"
elif [ ! -t 0 ]; then
  query=$(python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    event = json.loads(raw)
except json.JSONDecodeError:
    print(raw.strip())
else:
    if isinstance(event, dict):
        for key in ("prompt", "user_prompt", "query", "message", "text"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                print(value)
                break
')
else
  echo "usage: lux_find_hook.sh \"question\"   (or pipe the agent event as JSON)" >&2
  exit 0
fi

[ -n "${query:-}" ] || exit 0

out=$(lux-find find "$query" --json --limit 5 2>/dev/null)
case $? in
  0) ;;
  1) exit 0 ;;                                    # no hits: stay quiet
  3) echo "lux-find: index missing or corrupt - run 'lux-find index'" >&2
     exit 0 ;;                                    # never block the agent
  *) exit 0 ;;
esac

echo "--- from your own notes and code (lux_find) ---"
echo "$out" | python3 -c '
import json, sys
for h in json.load(sys.stdin)["hits"]:
    print("{}:{} [{}]".format(h["path"], h["line"], h["kind"]))
    print("  {}\n".format(h["snippet"]))
'
