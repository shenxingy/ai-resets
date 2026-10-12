"""The written contract and the public methodology say what the code does.

Each test reads one shipped document and checks a claim in it against the
module that implements it, or against the other place the same claim is
written. Prose that outlives its rule is how the contract came to call the
2026-09-08 clear `confirmed` after the code had stopped trusting the post
beside it, and how the methodology came to say "two Codex accounts" before the
second one was installed (backtest and privacy reviews). Both read as fact.

Only tracked files are read: docs/reset-attribution.md, README.md and
site/methodology.html. Nothing is written.
"""

from __future__ import annotations

import html
import json
import re
import unittest
from pathlib import Path

from scripts import build, incidents

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def flat(text):
    """One line, single spaces: prose wraps wherever its editor left it."""
    return re.sub(r"\s+", " ", text).strip()


def visible_text(page):
    """What a reader of the page sees: no scripts, no tags, entities decoded."""
    body = re.sub(r"<script\b.*?</script>", " ", page, flags=re.S)
    return flat(html.unescape(re.sub(r"<[^>]+>", " ", body)))


def json_ld_faq(page):
    """(question, answer) for every FAQ entry in the page's JSON-LD."""
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    graph = json.loads(match.group(1))["@graph"]
    faq = next(node for node in graph if node.get("@type") == "FAQPage")
    return [
        (flat(entry["name"]), flat(entry["acceptedAnswer"]["text"]))
        for entry in faq["mainEntity"]
    ]


def visible_faq(page):
    """(question, answer) for every <h3> and the paragraph under it."""
    section = re.search(r'<section id="faq">(.*?)</section>', page, re.S).group(1)
    return [
        (visible_text(question), visible_text(answer))
        for question, answer in re.findall(r"<h3>(.*?)</h3>\s*<p>(.*?)</p>", section, re.S)
    ]


# ─── The contract's historical table ─────────────────────────────────────────

# Each tracked Codex vendor reset, the minute our account's clear was read
# (one reading per minute, as the live probe reads), and the post nearest it,
# copied field for field from data/openai.json, which is not tracked.
BACKTEST = json.loads((FIXTURES / "backtest-posts.json").read_text(encoding="utf-8"))

TABLE_ROW = re.compile(
    r"^\| (\d\d-\d\d \d\d:\d\d) \| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \|$",
    re.M,
)


def contract_rows():
    """`MM-DD HH:MM` -> (account, cause, evidence, rule), from the table."""
    return {
        when: (account, cause, evidence, rule)
        for when, account, cause, evidence, rule in TABLE_ROW.findall(
            read("docs/reset-attribution.md")
        )
    }


def bottom_line():
    contract = read("docs/reset-attribution.md")
    return flat(contract.split("**Bottom line.**", 1)[1].split("\n\n", 1)[0])


def single_account_rule(case):
    """What one account's `unattributed` clear earns beside this post."""
    confirms = incidents.corroborates(case["post"], incidents.VERDICT_UNATTRIBUTED, case["clear_at"])
    return "confirmed" if confirms else "unattributed"


class BacktestTableTests(unittest.TestCase):
    def test_every_vendor_row_is_in_the_backtest(self):
        # A vendor row added to the table without its post here would be a
        # Rule cell nothing checks.
        vendor_rows = {when for when, row in contract_rows().items() if row[1] == "vendor"}
        self.assertEqual(vendor_rows, {case["row"] for case in BACKTEST})

    def test_each_rule_cell_is_what_the_post_beside_the_clear_earns(self):
        rows = contract_rows()
        for case in BACKTEST:
            with self.subTest(row=case["row"]):
                rule = rows[case["row"]][3]
                if rule.startswith("vendor_reset"):
                    # Decided by the second account, not by the post; the
                    # replay notes say the post would confirm the clear
                    # either way, and that has to stay true.
                    self.assertEqual(single_account_rule(case), "confirmed")
                    continue
                self.assertEqual(rule.split()[0], single_account_rule(case))

    def test_the_2026_09_08_post_is_a_paraphrase_that_confirms_nothing(self):
        # The backtest finding, pinned on the record itself: the tracker's
        # text was never verified against the post, so it is not trusted.
        case = next(case for case in BACKTEST if case["row"] == "09-08 01:22")
        self.assertFalse(incidents.is_trusted_announcement(case["post"]))
        self.assertEqual(single_account_rule(case), "unattributed")

    def test_the_bottom_line_names_each_vendor_clear_the_rule_leaves_private(self):
        # "Every historical clear lands on its true cause" was the claim; a
        # vendor clear left `unattributed` is the exception it must name.
        rows = contract_rows()
        left = [
            case for case in BACKTEST
            if not rows[case["row"]][3].startswith("vendor_reset")
            and single_account_rule(case) == "unattributed"
        ]
        self.assertTrue(left, "the backtest no longer has an exception; reword this test")
        line = bottom_line()
        for case in left:
            with self.subTest(row=case["row"]):
                self.assertTrue(case["clear_at"][:10] in line, f"bottom line: {line}")


# ─── The methodology page ───────────────────────────────────────────────────

# A count of Codex accounts, in any spelling the page could use. The page is
# published from the checkout every five minutes, while the second Codex
# account is read only once the probe unit is reinstalled: a number here is
# false on one side of that step. The coverage sentence says what is live.
CODEX_COUNT = re.compile(
    r"\b(?:one|two|three|four|both|\d+)\s+(?:of\s+our\s+)?Codex\s+(?:\w+\s+)?accounts?\b",
    re.I,
)


class MethodologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = read("site/methodology.html")
        cls.text = visible_text(cls.page)

    def test_no_count_of_codex_accounts_is_stated_as_fact(self):
        for where, text in [("page", self.text)] + [
            (question, answer) for question, answer in json_ld_faq(self.page)
        ]:
            with self.subTest(where=where):
                self.assertIsNone(CODEX_COUNT.search(text))

    def test_it_says_where_the_live_count_is_published(self):
        self.assertTrue("coverage sentence" in self.text, "the page never names it")
        self.assertIn("coverage sentence", json_ld_faq(self.page)[0][1])

    def test_the_structured_faq_is_the_visible_faq(self):
        # The answer search engines show is the answer on the page: two copies
        # of one claim are how one of them kept "two Codex accounts".
        self.assertEqual(json_ld_faq(self.page), visible_faq(self.page))

    def test_the_seen_line_it_quotes_is_the_one_the_page_draws(self):
        # The page promised "marked as seen on our account, with the time"
        # while neither renderer drew it; now that both do, the quote is the
        # renderer's own output, so a reworded line fails here first.
        line = build.seen_line({
            "observed": {"status": "confirmed", "verdict": "unattributed",
                         "accounts": ["a"], "lead_minutes": 2},
        })
        self.assertTrue(line)
        self.assertTrue(line in self.text, f"the methodology does not quote {line!r}")
        self.assertTrue(line in flat(read("README.md")), f"README.md does not quote {line!r}")


if __name__ == "__main__":
    unittest.main()
