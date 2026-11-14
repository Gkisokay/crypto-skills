import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import holder_patterns as hp  # noqa: E402


class HolderPatterns(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures" / "holder_patterns_cases.json"

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(cls.FIX.read_text())

    def test_uodf_equal_slices_flag_even_when_one_hop_funders_are_distinct(self):
        holders = []
        tx_counts = {}
        funders = {}
        for i in range(100):
            wallet = f"UODF{i + 1:03d}"
            holders.append((wallet, 0.00999))
            tx_counts[wallet] = 2 if i < 99 else 8
            funders[wallet] = f"HOP{i + 1:03d}"
        flag = hp.wallet_farm_from_rows(holders, tx_counts, funders)
        self.assertIsNotNone(flag)
        self.assertIn("100 wallets hold 99.9%", flag)
        self.assertIn("one-hop funders were distinct", flag)
        self.assertIn("most have <=4 txs", flag)

    def test_inuhao_shape_is_flagged_with_active_trading_text(self):
        holders = []
        tx_counts = {}
        funders = {}
        for i in range(92):
            wallet = f"INU{i + 1:03d}"
            share = 0.008 if i < 5 else 0.0090459770115
            holders.append((wallet, share))
            tx_counts[wallet] = 40 + (i % 21)
            funders[wallet] = "FARMROOT" if i < 50 else f"DISTINCT{i + 1:03d}"
        flag = hp.wallet_farm_from_rows(holders, tx_counts, funders)
        self.assertIsNotNone(flag)
        self.assertIn("92 wallets hold 82.7%", flag)
        self.assertIn("they trade actively (median 50 txs)", flag)
        self.assertIn("one wallet FARMROOT one hop up", flag)

    def test_affair_inu_sold_down_shape_does_not_trigger_wallet_farm(self):
        rows = self.data["affair_inu"]["holders"]
        holders = [(r["wallet"], r["share"]) for r in rows]
        tx_counts = {r["wallet"]: r["txs"] for r in rows}
        funders = {r["wallet"]: r["funder"] for r in rows}
        self.assertIsNone(hp.wallet_farm_from_rows(holders, tx_counts, funders))

    def test_equal_slices_with_small_combined_share_do_not_trigger(self):
        holders = [(f"SMALL{i:02d}", 0.002) for i in range(25)]
        tx_counts = {w: 2 for w, _ in holders}
        funders = {w: "F" for w, _ in holders}
        self.assertIsNone(hp.wallet_farm_from_rows(holders, tx_counts, funders))

    def test_fake_market_cap_threshold(self):
        flagged = hp.fake_market_cap_flag({"dex_fdv": 10_000_000, "liquidity": 20_000})
        clear = hp.fake_market_cap_flag({"dex_fdv": 1_000_000, "liquidity": 20_000})
        self.assertIn("(500x)", flagged)
        self.assertIsNone(clear)

    def test_fake_volume_back_and_forth_pattern(self):
        stats = {}
        for i in range(4):
            stats[f"W{i}"] = {"swaps": 50, "inflow": 100_000.0, "outflow": 96_000.0}
        for i in range(12):
            stats[f"N{i}"] = {"swaps": 2, "inflow": 800.0, "outflow": 100.0}
        flag = hp.fake_volume_flag_from_stats(stats)
        self.assertIsNotNone(flag)
        self.assertIn("top 4 wallets account for", flag)

    def test_fake_volume_needs_symmetry(self):
        stats = {}
        for i in range(4):
            stats[f"W{i}"] = {"swaps": 50, "inflow": 100_000.0, "outflow": 2_000.0}
        self.assertIsNone(hp.fake_volume_flag_from_stats(stats))

    def test_solana_holder_read_timeout_reports_error_class(self):
        old = hp.time.monotonic
        ticks = iter([0.0, 21.0, 21.0, 21.0, 21.0])
        hp.time.monotonic = lambda: next(ticks)

        def call(method, params):
            if method == "getProgramAccounts":
                return []
            if method == "getSignaturesForAddress":
                return []
            raise AssertionError((method, params))

        try:
            _, cant = hp.solana_holder_pattern_flags(call, "MINT", 1_000_000.0, {"symbol": "M"}, set(), lambda ws: {})
        finally:
            hp.time.monotonic = old
        self.assertTrue(any("RuntimeError" in c and "HELIUS_API_KEY" in c for c in cant), cant)

    def test_solana_wallet_farm_rpc_calls_stay_bounded(self):
        calls = []
        holders = [{"account": {"data": {"parsed": {"info": {"owner": f"W{i:02d}", "tokenAmount": {"uiAmount": 20.0}}}}}}
                   for i in range(25)]

        def call(method, params):
            calls.append(method)
            if method == "getProgramAccounts":
                return holders if len([m for m in calls if m == "getProgramAccounts"]) == 1 else []
            if method == "getSignaturesForAddress":
                target = params[0]
                if target == "MINT":
                    return []
                return [{"signature": f"{target}-a"}, {"signature": f"{target}-b"}]
            if method == "getTransaction":
                return {"transaction": {"message": {"accountKeys": [{"pubkey": "FUNDER"}]}}}
            raise AssertionError((method, params))

        def kind_lookup(wallets):
            return {w: "person" for w in wallets}

        flags, _ = hp.solana_holder_pattern_flags(call, "MINT", 1000.0, {"symbol": "M"}, set(), kind_lookup)
        self.assertTrue(any("wallets hold" in f for f in flags), flags)
        self.assertLessEqual(len(calls), 45, len(calls))

    def test_solana_token2022_holders_are_read_without_size_filter(self):
        # Token-2022 accounts are 170+ bytes; a dataSize=165 filter returns none of them (inuhao, Nov 14 2026)
        rows = [{"account": {"data": {"parsed": {"info": {"owner": f"W{i:02d}", "tokenAmount": {"uiAmount": 20.0}}}}}}
                for i in range(25)]

        def call(method, params):
            if method == "getProgramAccounts":
                program, flt = params[0], params[1]["filters"]
                sized = any("dataSize" in f for f in flt)
                if program == hp.SOLANA_TOKEN_PROGRAMS[1]:
                    return [] if sized else rows
                return []
            return []

        balances = hp._solana_holders(call, "MINT")
        self.assertEqual(len(balances), 25)

    def test_solana_unexpected_error_type_is_not_swallowed(self):
        def call(method, params):
            if method == "getProgramAccounts":
                raise ValueError("bad payload")
            return []

        with self.assertRaises(ValueError):
            hp.solana_holder_pattern_flags(call, "MINT", 1000.0, {"symbol": "M"}, set(), lambda ws: {})


if __name__ == "__main__":
    unittest.main()
