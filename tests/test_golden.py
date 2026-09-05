"""Golden / "known answer" set: hits@3 over a synthetic, deterministic corpus.

The corpus is a small company wiki (notes across many folders), a toy code
project (three files, three named functions) and one chat export. Every note
covers exactly one topic and uses a handful of words that do not show up
anywhere else in the corpus, so we can say with certainty which single
document a given query *should* surface - this is what makes the table below
a "known answer" set rather than a vibe check.

Nothing here is mocked: every query goes through ``lux_find.cli.main``
exactly the way a user would invoke it, against a real SQLite/FTS5 index
built from real files on disk.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from lux_find.cli import EXIT_NO_HITS, EXIT_OK, main  # noqa: E402


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


# --------------------------------------------------------------- the corpus

# 30 notes across 10 folders. Each one leans on a couple of words that are
# deliberately not reused in any other note, so a query built from those
# words has exactly one right answer.
NOTES: dict[str, str] = {
    "architecture/event-bus.md": (
        "# Event Bus\n\n"
        "Our event bus uses Kafka with hash-based partitioning. Consumers are "
        "organized into named consumer groups so each partition is processed "
        "by exactly one worker, and a paused group can resume by replaying "
        "its committed offset.\n"
    ),
    "architecture/database-sharding.md": (
        "# Database Sharding\n\n"
        "Each row is assigned a shard using a hash of the customer id. "
        "Rebalancing moves shard ranges between nodes during a maintenance "
        "window, and a bad shard key choice creates hot spots that no amount "
        "of caching fixes.\n"
    ),
    "architecture/caching-layer.md": (
        "# Caching Layer\n\n"
        "Read-through caching sits in front of the primary database using "
        "Redis. Every write invalidates the matching cache key immediately, "
        "and unused keys still expire on a fixed ttl so a missed invalidation "
        "cannot linger forever.\n"
    ),
    "architecture/api-gateway.md": (
        "# API Gateway\n\n"
        "The gateway enforces a rate limit per API key using a sliding token "
        "bucket. Callers that exceed their quota receive a throttling "
        "response with a retry-after header instead of a hard failure.\n"
    ),
    "ops/incident-response.md": (
        "# Incident Response\n\n"
        "A triggered alert pages the on-call engineer according to its "
        "severity level. Every incident above severity two ends with a "
        "written postmortem within five business days.\n"
    ),
    "ops/backup-policy.md": (
        "# Backup Policy\n\n"
        "The primary database is backed up nightly and copies are retained "
        "for thirty days. A quarterly restore drill proves the backups are "
        "actually usable, not just present.\n"
    ),
    "ops/deployment-pipeline.md": (
        "# Deployment Pipeline\n\n"
        "Every release goes out blue-green, with a canary slice taking live "
        "traffic first. A single command triggers an automatic rollback if "
        "the canary's error rate climbs.\n"
    ),
    "ops/monitoring-alerts.md": (
        "# Monitoring And Alerts\n\n"
        "Dashboards track latency and error budgets per service. Alert "
        "thresholds are tuned deliberately because alert fatigue from noisy "
        "pages is worse than missing one real problem.\n"
    ),
    "security/access-review.md": (
        "# Access Review\n\n"
        "Every quarter, managers review who has access to production "
        "systems and revoke anything unused, following the principle of "
        "least privilege.\n"
    ),
    "security/secrets-rotation.md": (
        "# Secrets Rotation\n\n"
        "API keys and database passwords are rotated every ninety days and "
        "stored only in the vault, never in a config file checked into git.\n"
    ),
    "security/vulnerability-scanning.md": (
        "# Vulnerability Scanning\n\n"
        "A nightly job scans every dependency for known CVEs and opens a "
        "ticket for anything above medium severity so it gets triaged "
        "instead of ignored.\n"
    ),
    "product/roadmap-q3.md": (
        "# Roadmap Q3\n\n"
        "This quarter ships the mobile app, a dark mode theme, and offline "
        "sync so notes keep working without a network connection.\n"
    ),
    "product/pricing-model.md": (
        "# Pricing Model\n\n"
        "Pricing has three tiers: a free tier for individuals, a pro tier "
        "with more storage, and an enterprise tier billed per seat with a "
        "dedicated account manager.\n"
    ),
    "product/onboarding-flow.md": (
        "# Onboarding Flow\n\n"
        "A new user gets a welcome email, then a short checklist that walks "
        "them through creating their first project with a guided wizard.\n"
    ),
    "product/feature-flags.md": (
        "# Feature Flags\n\n"
        "New behaviour ships behind a flag with a gradual rollout "
        "percentage, and every flag has a kill switch that disables it "
        "instantly without a deploy.\n"
    ),
    "hr/vacation-policy.md": (
        "# Vacation Policy\n\n"
        "Employees accrue annual leave every month. Manager approval is "
        "required before requesting time away from the office, and unused "
        "leave carries over into the next year.\n"
    ),
    "hr/hiring-process.md": (
        "# Hiring Process\n\n"
        "A candidate moves through four interview stages ending with a "
        "hiring committee decision. The take-home exercise is optional for "
        "senior candidates who have three or more work samples.\n"
    ),
    "hr/performance-review.md": (
        "# Performance Review\n\n"
        "Reviews happen every six months and include a calibration meeting "
        "where managers compare ratings across the team before anything is "
        "finalized.\n"
    ),
    "finance/expense-policy.md": (
        "# Expense Policy\n\n"
        "Submit a receipt for any expense over twenty five dollars. "
        "Reimbursement requires approval from your manager before it is "
        "paid out in the next payroll cycle.\n"
    ),
    "finance/budget-planning.md": (
        "# Budget Planning\n\n"
        "Each department proposes a yearly spending allocation in the fall, "
        "and finance consolidates every proposal into one company-wide "
        "budget by December.\n"
    ),
    "travel/packing-checklist.md": (
        "# Packing Checklist\n\n"
        "A backpacking trip needs layered clothing, a rain shell, and a "
        "compressible sleeping bag. Pack the heaviest items closest to your "
        "back.\n"
    ),
    "travel/visa-requirements.md": (
        "# Visa Requirements\n\n"
        "A tourist visa for Japan requires a passport valid for at least "
        "six months beyond the travel dates and proof of a return ticket.\n"
    ),
    "cooking/sourdough-starter.md": (
        "# Sourdough Starter\n\n"
        "Feed the starter equal parts flour and water once a day. A hundred "
        "percent hydration ratio keeps it easy to predict, and discard half "
        "before every feeding.\n"
    ),
    "cooking/knife-skills.md": (
        "# Knife Skills\n\n"
        "Hone the blade on a steel before each use and sharpen it on a "
        "whetstone every few weeks. A consistent fifteen degree angle keeps "
        "the edge even.\n"
    ),
    "gardening/tomato-care.md": (
        "# Tomato Care\n\n"
        "Water tomatoes deeply and consistently, because irregular watering "
        "causes blossom end rot. Stake the plant early so the stem does not "
        "snap once fruit sets.\n"
    ),
    "gardening/composting.md": (
        "# Composting\n\n"
        "A healthy compost pile needs roughly thirty parts carbon to one "
        "part nitrogen. Turn the pile weekly so oxygen reaches the center "
        "and it does not start to smell.\n"
    ),
    "meetings/2031-01-05-standup.md": (
        "# Standup Jan 5\n\n"
        "Sprint planning assigned story points for every ticket in the "
        "backlog. The team agreed to carry over two unfinished tickets into "
        "the next sprint.\n"
    ),
    "meetings/2031-02-10-retro.md": (
        "# Retro Feb 10\n\n"
        "Action items from the retro: rotate the on-call schedule fairly "
        "and write down what went well so the team keeps doing it.\n"
    ),
    "misc/glossary.md": (
        "# Glossary\n\n"
        "A short list of terms used across this wiki: MTTR, RPO, RTO, and "
        "SLA are defined here so nobody has to ask twice.\n"
    ),
    "misc/faq.md": (
        "# FAQ\n\n"
        "Frequently asked questions about this wiki itself: how to add a "
        "page, who can edit it, and how search finds your notes.\n"
    ),
}

# One small, real code project: three files, three distinctly-named functions.
CODE: dict[str, str] = {
    "shipping.py": (
        '"""Shipping cost calculations."""\n\n\n'
        "def calculate_shipping_cost(weight_kg, distance_km):\n"
        "    base_fee = 4.5\n"
        "    return base_fee + weight_kg * 0.8 + distance_km * 0.05\n"
    ),
    "csv_utils.py": (
        "def parse_csv_row(line, delimiter=\",\"):\n"
        "    return [field.strip() for field in line.split(delimiter)]\n"
    ),
    "validators.py": (
        "def validate_email_format(address):\n"
        "    return \"@\" in address and \".\" in address.split(\"@\")[-1]\n"
    ),
    "README.md": (
        "# Toy Shipping Service\n\n"
        "A small demo service: shipping cost, csv row parsing, and email "
        "validation.\n"
    ),
}

# One chat export holding a phrase that appears nowhere else in the corpus.
CHAT_MESSAGES = [
    {"type": "user", "message": {"role": "user", "content":
        "Why did the payment gateway keep working during yesterday's outage?"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text":
         "The payments team calls it a silent failover: when the primary "
         "provider times out, the client swaps to the backup processor "
         "without alerting the caller at all."}]}},
    {"type": "user", "message": {"role": "user", "content":
        "Should we log when that happens?"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text":
         "Yes, we added a metric so silent failover events show up on the "
         "payments dashboard."}]}},
]


def build_golden_corpus(base: Path) -> tuple[Path, Path, Path]:
    """Write the notes / code / chat corpus under ``base``. Deterministic."""
    notes_dir = base / "wiki"
    for rel, body in NOTES.items():
        path = notes_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    code_dir = base / "project"
    for rel, body in CODE.items():
        path = code_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    chats_dir = base / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    (chats_dir / "session.jsonl").write_text(
        "\n".join(json.dumps(m) for m in CHAT_MESSAGES) + "\n", encoding="utf-8"
    )

    return notes_dir, code_dir, chats_dir


# (query, root, relative_path) - "root" is one of "notes" / "code" / "chats",
# resolved against whichever directory build_golden_corpus() put it in.
GOLDEN_PAIRS: list[tuple[str, str, str]] = [
    ("kafka consumer group offset replay", "notes", "architecture/event-bus.md"),
    ("hash shard key hot spot rebalancing", "notes", "architecture/database-sharding.md"),
    ("redis cache invalidation ttl expire", "notes", "architecture/caching-layer.md"),
    ("api key rate limit token bucket throttling", "notes", "architecture/api-gateway.md"),
    ("on-call paging severity postmortem", "notes", "ops/incident-response.md"),
    ("nightly backup retention restore drill", "notes", "ops/backup-policy.md"),
    ("blue-green canary rollback deployment", "notes", "ops/deployment-pipeline.md"),
    ("quarterly access review least privilege revoke", "notes", "security/access-review.md"),
    ("rotate api keys ninety days vault", "notes", "security/secrets-rotation.md"),
    ("Q3 roadmap dark mode offline sync mobile", "notes", "product/roadmap-q3.md"),
    ("tiered pricing enterprise seats free pro", "notes", "product/pricing-model.md"),
    ("expense reimbursement receipt approval manager", "notes", "finance/expense-policy.md"),
    ("Japan tourist visa passport validity six months", "notes", "travel/visa-requirements.md"),
    ("sourdough starter feeding hydration ratio discard", "notes", "cooking/sourdough-starter.md"),
    ("tomato blossom end rot watering stake", "notes", "gardening/tomato-care.md"),
]

HARD_PAIRS: list[tuple[str, str, str]] = [
    # Synonym-ish: doesn't use the note's own title word ("vacation"); relies
    # on the shared, uncommon vocabulary ("annual leave" / "manager") instead
    # of an exact phrase match.
    ("annual leave accrual and manager sign-off", "notes", "hr/vacation-policy.md"),
    # A bare code identifier, the way an agent would actually search for a
    # function it half-remembers.
    ("calculate_shipping_cost", "code", "shipping.py"),
    # A phrase that only exists inside the chat transcript, nowhere in notes.
    ("silent failover", "chats", "session.jsonl"),
]

ALL_PAIRS = GOLDEN_PAIRS + HARD_PAIRS

NO_HIT_QUERY = "xylophone quokka zeppelin nebula"


class TestGoldenCorpus(unittest.TestCase):
    """Known-answer search quality: hits@3 over a fixed, synthetic corpus."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name)
        cls.notes_dir, cls.code_dir, cls.chats_dir = build_golden_corpus(cls.base)
        cls.cfg = cls.base / "lux_find.toml"
        cls.db = cls.base / "index.sqlite"

        with captured():
            code = main([
                "init",
                str(cls.notes_dir), str(cls.code_dir), str(cls.chats_dir),
                "--config", str(cls.cfg), "--db", str(cls.db),
            ])
        assert code == EXIT_OK, "golden corpus init failed"

        with captured() as (out, _):
            code = main(["index", "--config", str(cls.cfg), "--quiet"])
        assert code == EXIT_OK, "golden corpus index failed"
        cls.build_summary = json.loads(out.getvalue()[len("SUMMARY "):])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _root_for(self, root: str) -> Path:
        return {"notes": self.notes_dir, "code": self.code_dir, "chats": self.chats_dir}[root]

    def _find_json(self, query: str) -> dict:
        with captured() as (out, _):
            main(["find", query, "--config", str(self.cfg), "--json", "-n", "5"])
        return json.loads(out.getvalue())

    def _assert_hits_at_3(self, query: str, root: str, relpath: str) -> None:
        expected = (self._root_for(root) / relpath).resolve()
        payload = self._find_json(query)
        top3 = payload["hits"][:3]
        found = [Path(h["path"]).resolve() for h in top3]
        self.assertIn(
            expected, found,
            f"query {query!r}: expected {expected} in top-3, got {found} "
            f"(all {len(payload['hits'])} hits: "
            f"{[Path(h['path']).name for h in payload['hits']]})",
        )

    def test_index_built_the_whole_corpus(self):
        # 30 notes + 4 code files + 1 chat document, minus nothing skipped.
        self.assertTrue(self.build_summary["complete"])
        self.assertGreaterEqual(self.build_summary["indexed"], 30 + 4 + 1)
        self.assertEqual(self.build_summary["errors"], [])

    def test_no_hit_query_returns_no_hits_exit_code(self):
        with captured():
            code = main(["find", NO_HIT_QUERY, "--config", str(self.cfg)])
        self.assertEqual(code, EXIT_NO_HITS)

    def test_no_hit_query_json_reports_zero_hits(self):
        payload = self._find_json(NO_HIT_QUERY)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["hits"], [])


def _make_test(query: str, root: str, relpath: str):
    def test(self):
        self._assert_hits_at_3(query, root, relpath)
    return test


# One generated test method per golden pair, so a single wrong answer fails
# with the specific query in its name instead of one big loop swallowing it.
for _i, (_query, _root, _relpath) in enumerate(GOLDEN_PAIRS):
    _name = f"test_golden_{_i:02d}_{_relpath.replace('/', '_').replace('.md', '')}"
    setattr(TestGoldenCorpus, _name, _make_test(_query, _root, _relpath))

for _i, (_query, _root, _relpath) in enumerate(HARD_PAIRS):
    _name = f"test_hard_{_i:02d}_{_relpath.replace('/', '_').replace('.md', '').replace('.py', '')}"
    setattr(TestGoldenCorpus, _name, _make_test(_query, _root, _relpath))


if __name__ == "__main__":
    unittest.main()
