import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import verdict  # noqa: E402


class VerdictRules(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures" / "verdict_cases.json"

    def load_cases(self):
        return json.loads(self.FIX.read_text())

    def test_primary_rule_order_and_sentence(self):
        for case in self.load_cases():
            with self.subTest(case=case["name"]):
                primary = verdict.primary_flag(case["flags"])
                self.assertEqual(primary["code"], case["expected_code"])
                self.assertIn(case["verdict_contains"], primary["verdict"])

    def test_no_flags_with_and_without_launch_checks(self):
        self.assertEqual(
            verdict.main_verdict([], launch_checks_ran=True),
            "No red flags found by these checks; that isn't the same as safe.",
        )
        self.assertEqual(
            verdict.main_verdict([], launch_checks_ran=False),
            "No red flags found by these checks, but the launch checks didn't run; this says little.",
        )

    def test_each_flag_gets_a_stable_code(self):
        flags = [
            "Liquidity pulled: Wallet 0x1111111111111111111111111111111111111111 removed 8.0 WETH from pool 0x2222222222222222222222222222222222222222, 11 min after the first pool opened; the pool has 0.00 WETH left. Last removal: tx 0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "Top 10 people (pools, burns, contracts excluded) hold 41.0%.",
            "Unmapped new flag text from another module.",
        ]
        got = verdict.flag_codes(flags)
        self.assertEqual([x["code"] for x in got], ["LP_PULLED", "TOP10_PEOPLE", "FLAG_OTHER"])
        self.assertEqual([x["text"] for x in got], flags)


if __name__ == "__main__":
    unittest.main()
