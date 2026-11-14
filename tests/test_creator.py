import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import creator  # noqa: E402
import launch_check as cc  # noqa: E402


class CreatorChecks(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures"

    def load(self, name):
        return json.loads((self.FIX / name).read_text())

    def test_solana_creator_exit_3_seconds(self):
        d = self.load("creator_exit_solana.json")
        line = creator.creator_exit_solana(
            cc,
            d["txs"],
            d["mint"],
            d["creator"],
            d["launch_total"],
            d["launch_ts"],
            {"Curve111111111111111111111111111111111111111"},
        )
        self.assertIn("4.79%", line)
        self.assertIn("sold", line)
        self.assertIn("back to the curve/pool", line)
        self.assertIn("3 s later", line)
        self.assertIn("3CLgGkc15K7RnfaCSpyTKnVJqvGkVSB1Jji2MjVosLhyvE8KEfo5jiFJJsXJ6v9gxxSLzsFJZVhM8ZBeuZXyB4kA", line)

    def test_solana_creator_exit_non_pool_is_moved(self):
        d = self.load("creator_exit_solana.json")
        moved = dict(d["txs"][1])
        moved["meta"] = {
            "preTokenBalances": [
                {"mint": d["mint"], "owner": "OtherWallet111111111111111111111111111111111", "uiTokenAmount": {"uiAmount": 0.0}},
                {"mint": d["mint"], "owner": d["creator"], "uiTokenAmount": {"uiAmount": 47900.0}},
            ],
            "postTokenBalances": [
                {"mint": d["mint"], "owner": "OtherWallet111111111111111111111111111111111", "uiTokenAmount": {"uiAmount": 47900.0}},
                {"mint": d["mint"], "owner": d["creator"], "uiTokenAmount": {"uiAmount": 0.0}},
            ],
        }
        line = creator.creator_exit_solana(
            cc,
            [d["txs"][0], moved],
            d["mint"],
            d["creator"],
            d["launch_total"],
            d["launch_ts"],
            {"Curve111111111111111111111111111111111111111"},
        )
        self.assertIn("moved", line)
        self.assertNotIn("sold", line)

    def test_solana_creator_history_flag_from_fixture(self):
        d = self.load("creator_history_solana.json")
        saved_http = cc.http_json
        pages = list(d["pages"])
        seen_opts = []

        def fake_call(method, params):
            if method != "getTransactionsForAddress":
                raise AssertionError((method, params))
            seen_opts.append(params[1])
            token = params[1].get("paginationToken")
            if token is None:
                return {"data": pages[0], "paginationToken": "p2"}
            if token == "p2":
                return {"data": pages[1], "paginationToken": None}
            raise AssertionError(token)

        cc.http_json = lambda url, body=None, tries=7: d["dex_pairs"]
        try:
            r = {"notes": [], "extra_flags": []}
            cant = creator.add_solana_creator_checks(
                cc, fake_call, "https://mainnet.helius-rpc.com/?api-key=test", r,
                d["current_mint"], d["creator"], d["launch_ts"], 1_000_000.0, []
            )
        finally:
            cc.http_json = saved_http
        self.assertEqual(cant, [])
        self.assertTrue(seen_opts, "expected getTransactionsForAddress options")
        self.assertEqual(seen_opts[0]["encoding"], "jsonParsed")
        # Helius rejects version-1 transactions unless the client accepts them (live error -32015, Nov 14 2026)
        self.assertEqual(seen_opts[0]["maxSupportedTransactionVersion"], 1)
        self.assertEqual(seen_opts[0]["filters"]["blockTime"]["lte"], d["launch_ts"] + 86400)
        flag = next(x for x in r["extra_flags"] if x.startswith("This creator wallet"))
        self.assertIn("launched 20+ other coins", flag)
        self.assertRegex(flag, r"\(\d{4}-\d{2}-\d{2}( to \d{4}-\d{2}-\d{2})?\)")
        self.assertRegex(flag, r"of the 10 checked, \d+ are under \$5K FDV now")
        self.assertTrue(any("HighNow724k11111111111111111111111111111111" in n for n in r["notes"]), r["notes"])

    def test_solana_history_extracts_pumpfun_from_inner_instructions(self):
        tx = {
            "transaction": {
                "message": {
                    "instructions": [
                        {
                            "programId": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                            "accounts": ["MintInner1111111111111111111111111111111111", "CurveInner111111111111111111111111111111111"]
                        }
                    ]
                }
            },
            "meta": {
                "innerInstructions": [
                    {"instructions": [{"parsed": {"type": "initializeMint2", "info": {"mint": "MintInner1111111111111111111111111111111111"}}}]}
                ],
                "preTokenBalances": [],
                "postTokenBalances": [
                    {"mint": "MintInner1111111111111111111111111111111111", "owner": "CurveInner111111111111111111111111111111111", "uiTokenAmount": {"uiAmount": 1}}
                ]
            }
        }
        got = creator._extract_created_mints(tx)
        self.assertIn("MintInner1111111111111111111111111111111111", got)

    def test_evm_lp_add_then_hold_has_no_exit_line(self):
        d = self.load("creator_exit_evm.json")
        creator_addr = d["creator"]
        pool = d["pool"]
        token = "0x9999999999999999999999999999999999999999"
        create_tx = d["create_tx"]
        lp_tx = "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

        def topic(addr):
            return "0x" + "0" * 24 + addr[2:]

        logs = [
            {"blockNumber": "0x64", "transactionIndex": "0x0", "logIndex": "0x0", "transactionHash": create_tx,
             "topics": [cc.TRANSFER, topic(pool), topic(creator_addr)], "data": "0xbb80"},
            {"blockNumber": "0x65", "transactionIndex": "0x0", "logIndex": "0x0", "transactionHash": lp_tx,
             "topics": [cc.TRANSFER, topic(creator_addr), topic(pool)], "data": "0xbb80"},
        ]

        class FakeRpc:
            def logs(self, flt, frm, to, cap=None):
                return logs, True

            def block_ts(self, n):
                return {100: 1700000000, 101: 1700000060}[n]

            def call(self, method, params):
                if method == "eth_getTransactionReceipt":
                    return {
                        "logs": [
                            {"address": pool, "topics": [cc.MINT_V2]},
                            {"address": "0x7777777777777777777777777777777777777777",
                             "topics": [cc.TRANSFER, topic(creator_addr), topic(pool)]},
                        ]
                    }
                if method == "trace_filter":
                    return []
                raise AssertionError((method, params))

            def batch(self, calls):
                return []

        r = {
            "creator": creator_addr,
            "chain": "base",
            "launch": {"block_time_s": 1.0},
            "market": {"pairs": [pool], "pairs_any": [pool]},
            "notes": [],
            "extra_flags": [],
        }
        creator.add_evm_creator_checks(cc, FakeRpc(), r, token, create_tx, 100, 1700000000, 200, 1, 1_000_000.0, logs)
        self.assertFalse(any("sold" in x for x in r["extra_flags"]), r["extra_flags"])

    def test_evm_lp_add_then_sell_has_20_min_exit_line(self):
        d = self.load("creator_exit_evm.json")
        creator_addr = d["creator"]
        pool = d["pool"]
        token = "0x9999999999999999999999999999999999999999"
        create_tx = d["create_tx"]
        lp_tx = "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
        sell_tx = d["exit_tx"]

        def topic(addr):
            return "0x" + "0" * 24 + addr[2:]

        logs = [
            {"blockNumber": "0x64", "transactionIndex": "0x0", "logIndex": "0x0", "transactionHash": create_tx,
             "topics": [cc.TRANSFER, topic(pool), topic(creator_addr)], "data": "0xbb80"},
            {"blockNumber": "0x65", "transactionIndex": "0x0", "logIndex": "0x0", "transactionHash": lp_tx,
             "topics": [cc.TRANSFER, topic(creator_addr), topic(pool)], "data": "0x5dc0"},
            {"blockNumber": "0x66", "transactionIndex": "0x0", "logIndex": "0x0", "transactionHash": sell_tx,
             "topics": [cc.TRANSFER, topic(creator_addr), topic(pool)], "data": "0xbb80"},
        ]

        class FakeRpc:
            def logs(self, flt, frm, to, cap=None):
                return logs, True

            def block_ts(self, n):
                return {100: 1700000000, 101: 1700000060, 102: 1700001200}[n]

            def call(self, method, params):
                if method == "eth_getTransactionReceipt":
                    txh = params[0]
                    if txh == lp_tx:
                        return {
                            "logs": [
                                {"address": pool, "topics": [cc.MINT_V2]},
                                {"address": "0x7777777777777777777777777777777777777777",
                                 "topics": [cc.TRANSFER, topic(creator_addr), topic(pool)]},
                            ]
                        }
                    return {"logs": [{"address": token, "topics": [cc.TRANSFER, topic(creator_addr), topic(pool)]}]}
                if method == "trace_filter":
                    return []
                raise AssertionError((method, params))

            def batch(self, calls):
                return []

        r = {
            "creator": creator_addr,
            "chain": "base",
            "launch": {"block_time_s": 1.0},
            "market": {"pairs": [pool], "pairs_any": [pool]},
            "notes": [],
            "extra_flags": [],
        }
        creator.add_evm_creator_checks(cc, FakeRpc(), r, token, create_tx, 100, 1700000000, 200, 1, 1_000_000.0, logs)
        line = next(x for x in r["extra_flags"] if "20 min later" in x)
        self.assertIn("sold", line)
        self.assertIn(sell_tx, line)

    def test_evm_plain_transfer_reports_moved_not_sold(self):
        creator_addr = "0x1111111111111111111111111111111111111111"
        pool = "0x2222222222222222222222222222222222222222"
        other = "0x3333333333333333333333333333333333333333"
        token = "0x9999999999999999999999999999999999999999"
        create_tx = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        move_tx = "0xdddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"

        def topic(addr):
            return "0x" + "0" * 24 + addr[2:]

        logs = [
            {"blockNumber": "0x64", "transactionIndex": "0x0", "logIndex": "0x0", "transactionHash": create_tx,
             "topics": [cc.TRANSFER, topic(pool), topic(creator_addr)], "data": "0xbb80"},
            {"blockNumber": "0x65", "transactionIndex": "0x0", "logIndex": "0x0", "transactionHash": move_tx,
             "topics": [cc.TRANSFER, topic(creator_addr), topic(other)], "data": "0xbb80"},
        ]

        class FakeRpc:
            def logs(self, flt, frm, to, cap=None):
                return logs, True

            def block_ts(self, n):
                return {100: 1700000000, 101: 1700000100}[n]

            def call(self, method, params):
                if method == "trace_filter":
                    return []
                if method == "eth_getTransactionReceipt":
                    return {"logs": []}
                raise AssertionError((method, params))

            def batch(self, calls):
                return []

        r = {"creator": creator_addr, "chain": "base", "launch": {"block_time_s": 1.0},
             "market": {"pairs": [pool], "pairs_any": [pool]}, "notes": [], "extra_flags": []}
        creator.add_evm_creator_checks(cc, FakeRpc(), r, token, create_tx, 100, 1700000000, 200, 1, 1_000_000.0, logs)
        self.assertTrue(any("moved" in x and other in x for x in r["extra_flags"]), r["extra_flags"])
        self.assertFalse(any("sold" in x for x in r["extra_flags"]), r["extra_flags"])

    def test_solana_history_public_rpc_tells_how_to_fix(self):
        def failing_call(method, params):
            raise RuntimeError("method not found")

        cant = creator.add_solana_creator_checks(
            cc, failing_call, "https://api.mainnet-beta.solana.com", {"notes": [], "extra_flags": []},
            "mint", "creator", 1700000000, 1000.0, []
        )
        self.assertTrue(any("HELIUS_API_KEY" in x for x in cant), cant)

    def test_exit_errors_are_reported_not_silent(self):
        saved = creator.creator_exit_solana
        creator.creator_exit_solana = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            cant = creator.add_solana_creator_checks(
                cc,
                lambda m, p: {"data": [], "paginationToken": None},
                "https://mainnet.helius-rpc.com/?api-key=test",
                {"notes": [], "extra_flags": []},
                "mint",
                "creator",
                1700000000,
                1000.0,
                [],
            )
        finally:
            creator.creator_exit_solana = saved
        self.assertTrue(any("Creator exit check couldn't finish: RuntimeError" in x for x in cant), cant)

    def test_evm_exit_errors_are_reported_not_silent(self):
        saved = creator.creator_exit_evm
        creator.creator_exit_evm = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad"))

        class FakeRpc:
            def call(self, method, params):
                if method == "trace_filter":
                    return []
                raise AssertionError((method, params))

            def batch(self, calls):
                return []

        try:
            r = {"creator": "0x1111111111111111111111111111111111111111", "chain": "base",
                 "launch": {"block_time_s": 1.0}, "market": {"pairs": []}, "notes": [], "extra_flags": []}
            cant = creator.add_evm_creator_checks(
                cc, FakeRpc(), r, "0x9999999999999999999999999999999999999999",
                "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                100, 1700000000, 200, 1, 1000.0, []
            )
        finally:
            creator.creator_exit_evm = saved
        self.assertTrue(any("Creator exit check couldn't finish: ValueError" in x for x in cant), cant)


if __name__ == "__main__":
    unittest.main()
