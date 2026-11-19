import hashlib
import json
import sys
import unittest
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import solana_check as sc  # noqa: E402
import solana_liquidity as sl  # noqa: E402


class SolanaLiquidity(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures"

    def load(self, name):
        return json.loads((self.FIX / name).read_text())

    def test_program_id_detection_uses_instructions_not_log_text(self):
        tx_top = {
            "transaction": {
                "signatures": ["s1"],
                "message": {"accountKeys": [{"pubkey": "payer"}, {"pubkey": "acc"}], "instructions": [{"programId": sl.PUMPSWAP_AMM}]},
            },
            "meta": {"innerInstructions": []},
        }
        tx_inner = {
            "transaction": {
                "signatures": ["s2"],
                "message": {"accountKeys": [{"pubkey": "payer"}, {"pubkey": sl.RAYDIUM_CPMM}], "instructions": [{"programIdIndex": 1}]},
            },
            "meta": {"innerInstructions": [{"index": 0, "instructions": [{"programId": sl.METEORA_DAMM_V2}]}]},
        }
        self.assertEqual(sl.detect_amm([tx_top]), "pumpswap")
        self.assertEqual(sl.detect_amm([tx_inner]), "meteora")

    def test_pump_fun_curve_program_is_not_pumpswap(self):
        tx = {
            "transaction": {
                "signatures": ["s1"],
                "message": {"accountKeys": [{"pubkey": "payer"}], "instructions": [{"programId": sl.PUMP_FUN_CURVE}]},
            },
            "meta": {"innerInstructions": []},
        }
        self.assertNotEqual(sl.detect_amm([tx]), "pumpswap")

    def test_synthetic_meteora_pool_formats_pulled_flag(self):
        d = self.load("solana_liquidity_synthetic.json")
        pool = d["pools"][0]
        text = sl.format_liquidity_pull(pool)
        self.assertIn("Liquidity pulled", text)
        self.assertIn("HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC", text)
        self.assertIn("44,700", text)
        self.assertIn("0.16", text)

    def make_tx(self, sig, ts, payer, keys, pre, post, instructions=None, inner=None, pre_bal=None, post_bal=None):
        return {
            "slot": ts,
            "blockTime": ts,
            "transaction": {
                "signatures": [sig],
                "message": {
                    "accountKeys": [{"pubkey": k} for k in keys],
                    "instructions": instructions or [],
                },
            },
            "meta": {
                "err": None,
                "preTokenBalances": pre,
                "postTokenBalances": post,
                "innerInstructions": inner or [],
                "preBalances": pre_bal or [0] * len(keys),
                "postBalances": post_bal or [0] * len(keys),
            },
        }

    def tb(self, owner, mint, amt):
        return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}

    def fake_call_for_pulls(self, pool, quote_mint, now_quote, sigs, tx_by_sig):
        calls = collections.Counter()

        def call(method, params):
            calls[method] += 1
            if method == "getSignaturesForAddress":
                if params[0] != pool:
                    return []
                return [{"signature": s, "blockTime": i + 2000, "err": None} for i, s in enumerate(sigs)]
            if method == "getTransaction":
                return tx_by_sig.get(params[0])
            if method == "getTokenAccountsByOwner":
                owner, flt = params[0], params[1]
                if len(params) < 3 or params[2].get("encoding") != "jsonParsed":
                    raise RuntimeError("getTokenAccountsByOwner: Encoded binary (base 58) data should be less than 128 bytes")
                if owner == pool and flt.get("mint") == quote_mint:
                    return {"value": [{"pubkey": "vault-1", "account": {"data": {"parsed": {"info": {
                        "mint": quote_mint, "tokenAmount": {"uiAmount": now_quote}}}}}}]}
                return {"value": []}
            raise AssertionError((method, params))

        return call, calls

    def test_drained_by_removal_flags_wallet_and_tx(self):
        mint, quote, pool, wallet = "MINT", "USDC", "POOL", "WALLET"
        launch = [self.make_tx(
            "launch-1", 1000, "CREATOR", [pool, wallet, sl.METEORA_DAMM_V2],
            [self.tb(pool, mint, 900000), self.tb(pool, quote, 1000.0)],
            [self.tb(pool, mint, 900000), self.tb(pool, quote, 1000.0)],
            instructions=[{"programId": sl.METEORA_DAMM_V2}],
        )]
        drain = self.make_tx(
            "drain-1", 2000, "CREATOR", [pool, wallet, sl.METEORA_DAMM_V2],
            [self.tb(pool, mint, 900000), self.tb(pool, quote, 1000.0), self.tb(wallet, mint, 0.0), self.tb(wallet, quote, 0.0)],
            [self.tb(pool, mint, 10000), self.tb(pool, quote, 20.0), self.tb(wallet, mint, 890000.0), self.tb(wallet, quote, 980.0)],
            instructions=[{"programId": sl.METEORA_DAMM_V2, "parsed": {"type": "withdraw"}}],
        )
        call, _ = self.fake_call_for_pulls(pool, quote, 20.0, ["drain-1"], {"drain-1": drain})
        out = sl.pool_pull_flag(call, mint, pool, launch, max_recent=10)
        self.assertIsNotNone(out)
        self.assertIn("Liquidity pulled", out["text"])
        self.assertIn(wallet, out["text"])
        self.assertIn("drain-1", out["text"])

    def test_drained_by_many_sells_is_not_flagged_as_pull(self):
        mint, quote, pool = "MINT", "USDC", "POOL"
        launch = [self.make_tx(
            "launch-1", 1000, "CREATOR", [pool, "s1", sl.RAYDIUM_CPMM],
            [self.tb(pool, mint, 900000), self.tb(pool, quote, 1000.0)],
            [self.tb(pool, mint, 900000), self.tb(pool, quote, 1000.0)],
            instructions=[{"programId": sl.RAYDIUM_CPMM}],
        )]
        sell = self.make_tx(
            "sell-1", 2000, "TRADER", [pool, "TRADER", sl.RAYDIUM_CPMM],
            [self.tb(pool, mint, 900000), self.tb(pool, quote, 1000.0), self.tb("TRADER", mint, 10000.0), self.tb("TRADER", quote, 0.0)],
            [self.tb(pool, mint, 910000), self.tb(pool, quote, 900.0), self.tb("TRADER", mint, 0.0), self.tb("TRADER", quote, 100.0)],
            instructions=[{"programId": sl.RAYDIUM_CPMM}],
        )
        call, _ = self.fake_call_for_pulls(pool, quote, 20.0, ["sell-1"], {"sell-1": sell})
        out = sl.pool_pull_flag(call, mint, pool, launch, max_recent=10)
        self.assertIsNone(out)

    def test_pull_budget_under_25_calls_per_pool(self):
        mint, quote, pool = "MINT", "USDC", "POOL"
        launch = [self.make_tx(
            "launch-1", 1000, "CREATOR", [pool, "w", sl.PUMPSWAP_AMM],
            [self.tb(pool, mint, 900000), self.tb(pool, quote, 1000.0)],
            [self.tb(pool, mint, 900000), self.tb(pool, quote, 1000.0)],
            instructions=[{"programId": sl.PUMPSWAP_AMM}],
        )]
        txs = {}
        sigs = []
        for i in range(20):
            sig = f"sig-{i}"
            sigs.append(sig)
            txs[sig] = self.make_tx(
                sig, 2000 + i, "TRADER", [pool, "TRADER", sl.PUMPSWAP_AMM],
                [self.tb(pool, mint, 900000 + i), self.tb(pool, quote, 1000.0 - i)],
                [self.tb(pool, mint, 900001 + i), self.tb(pool, quote, 999.0 - i)],
                instructions=[{"programId": sl.PUMPSWAP_AMM}],
            )
        call, calls = self.fake_call_for_pulls(pool, quote, 980.0, sigs, txs)
        _ = sl.pool_pull_flag(call, mint, pool, launch, max_recent=10)
        self.assertLessEqual(sum(calls.values()), 25)


def _addr(label):
    return sc.b58encode(hashlib.sha256(label.encode()).digest())


def _ix_data(name):
    return sc.b58encode(hashlib.sha256(f"global:{name}".encode()).digest()[:8] + bytes(16))


class MeteoraPulls(unittest.TestCase):
    MINT, POOL, CREATOR = _addr("synthetic-mint"), _addr("synthetic-pool"), _addr("synthetic-creator")

    def setUp(self):
        self.calls = collections.Counter()

    def reserves(self, kind):
        """(owner, coin reserve account, SOL reserve account) the way each Meteora program holds a pool's tokens."""
        if kind == "damm_v2":
            vault = lambda m: sc.pda([b"token_vault", sc.b58decode(m), sc.b58decode(self.POOL)], sl.METEORA_DAMM_V2)
            return sl.METEORA_DAMM_V2_AUTHORITY, vault(self.MINT), vault(sl.SOL_MINT)
        return self.POOL, _addr("synthetic-reserve-x"), _addr("synthetic-reserve-y")

    def tx(self, sig, ts, kind, coin, sol, program, payer=None, data=None, gained=None, other_pool_sol=None):
        """One pool tx moving the reserves from coin[0]/sol[0] to coin[1]/sol[1]; `gained` is coin landing with the payer.
        The payer's SOL comes out unwrapped (its wrapped-SOL account is closed), as Meteora removals usually do."""
        owner, coin_acct, sol_acct = self.reserves(kind)
        payer = payer or self.CREATOR
        keys = [payer, self.POOL, coin_acct, sol_acct, program, _addr("synthetic-payer-ata")]
        pre = [{"accountIndex": 2, "owner": owner, "mint": self.MINT, "uiTokenAmount": {"uiAmount": coin[0]}},
               {"accountIndex": 3, "owner": owner, "mint": sl.SOL_MINT, "uiTokenAmount": {"uiAmount": sol[0]}}]
        post = [{"accountIndex": 2, "owner": owner, "mint": self.MINT, "uiTokenAmount": {"uiAmount": coin[1]}},
                {"accountIndex": 3, "owner": owner, "mint": sl.SOL_MINT, "uiTokenAmount": {"uiAmount": sol[1]}}]
        if gained:
            post.append({"accountIndex": 5, "owner": payer, "mint": self.MINT, "uiTokenAmount": {"uiAmount": gained}})
        if other_pool_sol is not None:
            # another DAMM v2 pool in the same route: same shared authority, different vault
            keys.append(_addr("synthetic-other-vault"))
            for side, amt in ((pre, other_pool_sol[0]), (post, other_pool_sol[1])):
                side.append({"accountIndex": 6, "owner": sl.METEORA_DAMM_V2_AUTHORITY, "mint": sl.SOL_MINT,
                             "uiTokenAmount": {"uiAmount": amt}})
        ix = {"programId": program, "accounts": [self.POOL], "data": data or _ix_data("swap")}
        return {"slot": ts, "blockTime": ts,
                "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": k} for k in keys],
                                                                 "instructions": [ix]}},
                "meta": {"err": None, "preTokenBalances": pre, "postTokenBalances": post, "innerInstructions": []}}

    def fake_call(self, kind, history, now_sol):
        """history: the pool's txs, oldest first; signatures come back newest first."""
        by_sig = {t["transaction"]["signatures"][0]: t for t in history}
        newest_first = [{"signature": s, "err": None} for s in reversed(list(by_sig))]
        _, _, sol_acct = self.reserves(kind)

        def call(method, params):
            self.calls[method] += 1
            if method == "getSignaturesForAddress":
                self.assertEqual(params[0], self.POOL)
                return newest_first[:params[1]["limit"]]
            if method == "getTransaction":
                return by_sig.get(params[0])
            if method == "getTokenAccountsByOwner":
                if kind == "dlmm" and params[0] == self.POOL and params[1].get("mint") == sl.SOL_MINT:
                    return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": now_sol}}}}}}]}
                return {"value": []}
            if method == "getTokenAccountBalance":
                self.assertEqual(params[0], sol_acct)
                return {"value": {"uiAmount": now_sol}}
            raise AssertionError((method, params))

        return call

    def launch_and_trades(self, kind, program, n_trades):
        """Opening deposit of 1B coins and 100 SOL, then buys that add 0.5 SOL each."""
        hist = [self.tx("open", 1000, kind, (0, 1e9), (0, 100.0), program, data=_ix_data("initialize_pool"))]
        coin, sol = 1e9, 100.0
        for i in range(n_trades):
            hist.append(self.tx(f"buy-{i}", 1001 + i, kind, (coin, coin - 1e5), (sol, sol + 0.5), program,
                                payer=_addr(f"synthetic-buyer-{i}"), gained=1e5))
            coin, sol = coin - 1e5, sol + 0.5
        return hist, coin, sol

    def test_pool_authority_pda_matches_the_public_program_constant(self):
        self.assertEqual(sc.pda([b"pool_authority"], sl.METEORA_DAMM_V2), sl.METEORA_DAMM_V2_AUTHORITY)

    def test_damm_v2_remove_all_minutes_after_opening_is_a_pull(self):
        hist, coin, sol = self.launch_and_trades("damm_v2", sl.METEORA_DAMM_V2, 300)
        hist.append(self.tx("drain", 1400, "damm_v2", (coin, 30000.0), (sol, 3e-7), sl.METEORA_DAMM_V2,
                            data=_ix_data("remove_all_liquidity"), gained=coin - 30000.0))
        for i in range(5):  # dust trades after the drain, routed through a second DAMM v2 pool too
            hist.append(self.tx(f"after-{i}", 1500 + i, "damm_v2", (30000.0, 30000.0), (3e-7, 3e-7), sl.METEORA_DAMM_V2,
                                payer=_addr(f"synthetic-bot-{i}"), other_pool_sol=(50.0, 49.0)))
        call = self.fake_call("damm_v2", hist, 3e-7)
        out = sl.pool_pull_flag(call, self.MINT, self.POOL, hist[:150], max_recent=10)
        self.assertIsNotNone(out)
        self.assertIn("Liquidity pulled", out["text"])
        self.assertIn("tx drain ", out["text"])
        self.assertIn(self.CREATOR, out["text"])
        self.assertIn("from 250 SOL", out["text"])
        self.assertLessEqual(sum(self.calls.values()), 25)

    def test_dlmm_remove_all_minutes_after_opening_is_a_pull(self):
        hist, coin, sol = self.launch_and_trades("dlmm", sl.METEORA_DLMM, 20)
        hist.append(self.tx("drain", 1100, "dlmm", (coin, 0.0), (sol, 0.0), sl.METEORA_DLMM,
                            data=_ix_data("remove_all_liquidity"), gained=coin))
        hist.append(self.tx("after-0", 1200, "dlmm", (0.0, 0.0), (0.0, 0.0), sl.METEORA_DLMM, payer=_addr("synthetic-bot")))
        call = self.fake_call("dlmm", hist, 0.0)
        out = sl.pool_pull_flag(call, self.MINT, self.POOL, hist[:5], max_recent=10)
        self.assertIsNotNone(out)
        self.assertIn("tx drain ", out["text"])
        self.assertIn(self.CREATOR, out["text"])

    def test_partial_meteora_remove_is_not_a_pull(self):
        for kind, program, name in (("damm_v2", sl.METEORA_DAMM_V2, "remove_liquidity"),
                                    ("dlmm", sl.METEORA_DLMM, "remove_liquidity_by_range")):
            with self.subTest(kind=kind):
                hist, coin, sol = self.launch_and_trades(kind, program, 20)
                hist.append(self.tx("half", 1100, kind, (coin, coin / 2), (sol, sol / 2), program,
                                    data=_ix_data(name), gained=coin / 2))
                call = self.fake_call(kind, hist, sol / 2)
                self.assertIsNone(sl.pool_pull_flag(call, self.MINT, self.POOL, hist[:5], max_recent=10))


if __name__ == "__main__":
    unittest.main()
