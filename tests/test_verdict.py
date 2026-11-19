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

    def test_real_money_flags_get_codes(self):
        got = verdict.flag_codes([
            "The pool is mostly this coin's own tokens; real money in it is about $5,417.",
            "FDV is $1,000,000 while real money in its pools is $5,417 (185x); that gap is far larger than this liquidity can support.",
        ])
        self.assertEqual([x["code"] for x in got], ["COIN_SIDE_POOL", "FAKE_MARKET_CAP"])


class CreatorPullLeads(unittest.TestCase):
    CLUSTER = "Likely bundle: 6 wallets bought 43.0% of supply within 4 s of trading opening, funded together."

    def note(self, pct):
        return (f"The creator 0x{'c' * 40} removed {pct:.1%} of pool 0x{'e' * 64}'s peak liquidity positions 26.0h after "
                "liquidity was first added to that pool, but the pool still shows $2,400 of liquidity (likely held by its "
                "hook). Check where the liquidity sits.")

    def test_a_creator_full_pull_note_leads_ahead_of_a_cluster_share(self):
        got = verdict.main_verdict([self.CLUSTER], notes=[self.note(1.0)])
        self.assertTrue(got.startswith(
            "Main risk: the creator removed 100% of its liquidity position 26.0h after that pool opened; the pool still shows $2,400 "
            "(launch-check: likely held by its hook). Next: a group of wallets took a large share at launch"), got)

    def test_a_creator_pull_flag_is_worded_plainly(self):
        flag = (f"Liquidity pulled: The creator 0x{'c' * 40} removed 95.0% of pool 0x{'e' * 64}'s peak liquidity, 3 min after "
                "liquidity was first added to that pool. Last removal: tx 0x01")
        self.assertEqual(verdict.main_verdict([flag]), "Main risk: the creator pulled 95% of the pool's liquidity 3 min after that pool opened.")

    def test_cluster_only_and_a_half_pull_keep_the_old_verdict(self):
        old = verdict.primary_flag([self.CLUSTER])["verdict"]
        self.assertEqual(verdict.main_verdict([self.CLUSTER]), old)
        self.assertEqual(verdict.main_verdict([self.CLUSTER], notes=[self.note(0.5)]), old)


if __name__ == "__main__":
    unittest.main()
