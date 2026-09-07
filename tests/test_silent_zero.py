"""Every way the CLI could answer "nothing" without ever having searched.

These are one class of defect, not a list of unrelated bugs. Each case used to
end in something a caller reads as a clean verdict: exit 0 with help text on
stdout, ``count: 0`` over an index that holds the document, or an empty stdout
that turns into a ``JSONDecodeError`` far away from its cause. An empty result
set is a claim about the corpus, and this tool may only make that claim after
actually looking.

They drive the real CLI in a subprocess, because the exit code and the JSON
contract are the thing under test and neither is visible from inside the
library.
"""

import json
import unittest
from pathlib import Path

from test_regressions import CliCase


class TestAFailureNeverLooksLikeAnEmptyResult(CliCase):
    def test_a_query_starting_with_a_dash_is_searched_not_swallowed(self):
        self.seed({"a.md": "# Flags\n\nthe -hello flag is documented here\n"})
        self.cli("index", "-q", expect=0)
        proc = self.cli("find", "-hello", "--json", expect=0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["query"], "-hello")
        self.assertEqual(payload["count"], 1)
        self.assertNotIn("usage:", proc.stdout)

    def test_a_dash_query_still_honours_the_flags_around_it(self):
        # The query travels behind a `--` at the end of the line, so every flag
        # written after it has to keep working.
        self.seed({"a.md": "# Flags\n\nthe -hello flag is documented here\n"})
        self.cli("index", "-q", expect=0)
        proc = self.cli(
            "find", "-hello", "-n", "3", "-k", "notes",
            "--db", str(self.db), "--json", expect=0,
        )
        self.assertEqual(json.loads(proc.stdout)["count"], 1)

    def test_asking_for_help_still_gives_help(self):
        for flag in ("-h", "--help"):
            proc = self.cli("find", flag, expect=0)
            self.assertIn("usage:", proc.stdout)

    def test_a_limit_below_one_is_refused_instead_of_returning_nothing(self):
        self.seed({"a.md": "# A\n\nquokkamarker lives here\n"})
        self.cli("index", "-q", expect=0)
        self.assertEqual(self.hits("quokkamarker")["count"], 1)
        for bad in ("0", "-1", "-4"):
            proc = self.cli("find", "quokkamarker", "-n", bad, "--json", expect=2)
            payload = json.loads(proc.stdout)
            self.assertIn("error", payload)
            self.assertNotIn("count", payload, "a refused limit still reported a count")

    def test_json_stays_json_when_the_index_is_missing(self):
        self.seed({"a.md": "# A\n\nalpha\n"})
        proc = self.cli(
            "find", "alpha", "--db", str(self.base / "nope.sqlite"), "--json", expect=3,
        )
        payload = json.loads(proc.stdout)  # used to raise: stdout was empty
        self.assertEqual(payload["exit_code"], 3)
        self.assertIn("hint", payload)

    def test_json_stays_json_on_a_usage_error(self):
        proc = self.cli("find", "alpha", "--nonsense", "--json", expect=2)
        self.assertEqual(json.loads(proc.stdout)["exit_code"], 2)

    def test_an_error_payload_never_carries_a_count_or_hits(self):
        # The whole point: a caller doing payload["count"] must raise rather
        # than read a zero that was never measured.
        proc = self.cli(
            "find", "alpha", "--db", str(self.base / "nope.sqlite"), "--json", expect=3,
        )
        payload = json.loads(proc.stdout)
        self.assertNotIn("count", payload)
        self.assertNotIn("hits", payload)

    def test_a_failure_is_still_reported_on_stderr_for_a_person(self):
        proc = self.cli(
            "find", "alpha", "--db", str(self.base / "nope.sqlite"), "--json", expect=3,
        )
        self.assertIn("no index", proc.stderr)

    def test_force_prune_on_a_fresh_config_builds_instead_of_refusing(self):
        self.seed({"a.md": "# A\n\nalpha\n"})
        proc = self.cli("index", "-q", "--force-prune", expect=0)
        self.assertEqual(self.summary(proc)["indexed"], 1)


class TestCredentialFilesStayOutOfTheIndex(CliCase):
    """Widening the name denylist, without eating the user's own writing."""

    def test_credential_stores_without_a_telltale_extension_are_skipped(self):
        secret_body = "zebracanary\n"
        files = {
            ".git-" + "credentials": "https://user:pw@example.invalid\n",
            "service-account.json": '{"type": "service_account", "k": "zebracanary"}\n',
            "kubeconfig": "apiVersion: v1\nusers:\n- user:\n    token: zebracanary\n",
            "deploy.p8": "-----BEGIN PRIVATE KEY-----\n" + secret_body,
            "putty.ppk": "PuTTY-User-Key-File-3\n" + secret_body,
            "notes.md": "# Notes\n\nzebracanary is mentioned in my own note\n",
        }
        self.seed(files)
        self.cli("index", "-q", expect=0)
        payload = self.hits("zebracanary")
        found = {Path(hit["path"]).name for hit in payload["hits"]}
        self.assertEqual(found, {"notes.md"}, f"a credential file was indexed: {found}")

    def test_a_note_about_secrets_is_still_indexed(self):
        # The mirror image, and the reason the denylist is name-shaped rather
        # than word-shaped: widening it must not drop the user's own writing.
        self.seed({
            "secrets-rotation.md": "# Rotation\n\nzebracanary rotation runbook\n",
            "service-account-setup.md": "# Setup\n\nzebracanary onboarding notes\n",
        })
        self.cli("index", "-q", expect=0)
        self.assertEqual(
            self.hits("zebracanary")["count"], 2, "a note about secrets was dropped",
        )


if __name__ == "__main__":
    unittest.main()
