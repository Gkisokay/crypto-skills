import json
import sys
import unittest
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
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


if __name__ == "__main__":
    unittest.main()
