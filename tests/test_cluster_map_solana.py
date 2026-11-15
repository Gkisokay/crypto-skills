"""Step 6 tests: Solana helpers, plus SPIN replayed offline from a Helius fixture."""

import gzip
import json
import os
import tempfile
import unittest

from test_cluster_map_evm import CM, FIXTURES, JARGON, SHORT_ADDR

SPIN_MINT = "3yTAs65Jim4mRKp7Nw9zBBjGX6h4mZoD88Bg4Dh4Upwp"
SPIN_FANOUT = "Hkm9m53BVZsV3SRf23zzq6BVEpcsfz2M6Ucsa837Nzk7"
SPIN_FUNDER = "DU3ivnayD8xsqTV5LwbVLepvPgWVmt7Dqe2rJAvFtppD"
SPIN_POOL = "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM"
SPIN_COLLECTOR = "5odg6zvYmqGgCxz8tsbqFGVCWuEmqXpKnbZ6KHkiz4Ah"


class SolanaHelperTests(unittest.TestCase):
    def test_addresses_keep_their_case(self):
        self.assertEqual(CM.normalize_address(SPIN_FANOUT), SPIN_FANOUT)
        self.assertTrue(CM.is_valid_address(SPIN_MINT, "solana"))
        self.assertFalse(CM.is_valid_address(SPIN_MINT, "robinhood"))
        self.assertFalse(CM.is_valid_address("0x" + "ab" * 20, "solana"))

    def test_wallets_are_on_the_curve_and_pools_are_not(self):
        self.assertTrue(CM.on_ed25519_curve(CM.b58decode32(SPIN_FANOUT)))
        self.assertTrue(CM.on_ed25519_curve(CM.b58decode32(SPIN_FUNDER)))
        self.assertFalse(CM.on_ed25519_curve(CM.b58decode32(SPIN_POOL)))

    def compact(self):
        tx = {
            "slot": 7,
            "blockTime": 1794411894,
            "transaction": {
                "signatures": ["sig1"],
                "message": {
                    "accountKeys": [{"pubkey": "PAYER"}],
                    "instructions": [
                        {"program": "system", "parsed": {"type": "transfer", "info": {"source": "A", "destination": "B", "lamports": 1500000000}}},
                        {"program": "spl-token", "parsed": {"type": "transfer", "info": {}}},
                    ],
                },
            },
            "meta": {
                "innerInstructions": [
                    {"instructions": [{"program": "system", "parsed": {"type": "transfer", "info": {"source": "A", "destination": "C", "lamports": 0}}}]}
                ],
                "preTokenBalances": [
                    {"mint": SPIN_MINT, "owner": "S1", "uiTokenAmount": {"amount": "300"}},
                    {"mint": SPIN_MINT, "owner": "S2", "uiTokenAmount": {"amount": "100"}},
                    {"mint": "OTHER", "owner": "S1", "uiTokenAmount": {"amount": "999"}},
                ],
                "postTokenBalances": [
                    {"mint": SPIN_MINT, "owner": "S1", "uiTokenAmount": {"amount": "0"}},
                    {"mint": SPIN_MINT, "owner": "S2", "uiTokenAmount": {"amount": "0"}},
                    {"mint": SPIN_MINT, "owner": "R", "uiTokenAmount": {"amount": "400"}},
                ],
            },
        }
        return CM.compact_solana_tx(tx, SPIN_MINT)

    def test_compact_tx_keeps_sol_transfers_and_this_mints_changes(self):
        ctx = self.compact()
        self.assertEqual(ctx["sig"], "sig1")
        self.assertEqual(ctx["payer"], "PAYER")
        self.assertEqual(ctx["xfer"], [["A", "B", 1500000000]])
        self.assertEqual(ctx["tok"], {"S1": -300, "S2": -100, "R": 400})

    def test_token_receipts_split_across_senders_pro_rata(self):
        rows = CM.solana_token_rows(self.compact(), SPIN_MINT)
        self.assertEqual({(r["from"], r["to"], r["value"]) for r in rows}, {("S1", "R", "300"), ("S2", "R", "100")})
        self.assertTrue(all(r["contractAddress"] == SPIN_MINT for r in rows))

    def test_sol_compares_like_eth(self):
        rows = CM.solana_native_rows(self.compact())
        self.assertEqual(int(rows[0]["value"]), 15 * 10**17)

    def test_rate_limits_are_retried_and_failures_replay_the_same(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("RPC request failed with HTTP 429: Too Many Requests")
            return {"ok": 1}

        def broken():
            raise RuntimeError("RPC request failed with HTTP 500")

        source = CM.SolanaSource(None)
        old_sleep = CM.time.sleep
        CM.time.sleep = lambda s: None
        try:
            self.assertEqual(source.remember(["a"], flaky), {"ok": 1})
            with self.assertRaises(RuntimeError):
                source.remember(["b"], broken)
        finally:
            CM.time.sleep = old_sleep
        self.assertEqual(len(calls), 3)
        replay = CM.SolanaSource(None, json.loads(json.dumps(source.memo)), replay=True)
        self.assertEqual(replay.remember(["a"], flaky), {"ok": 1})
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            replay.remember(["b"], broken)

    def test_follow_forward_walks_sol_sent_after_launch(self):
        def key(*parts):
            return json.dumps(list(parts), separators=(",", ":"))

        start, funder, pool, dust = SPIN_FANOUT, SPIN_FUNDER, SPIN_POOL, SPIN_MINT
        buy = "3CLgGkc15K7RnfaCSpyTKnVJqvGkVSB1Jji2MjVosLhyvE8KEfo5jiFJJsXJ6v9gxxSLzsFJZVhM8ZBeuZXyB4kA"
        home = "5" + buy[1:]
        after = {"txs": [
            {"sig": buy, "slot": 101, "ts": 1, "payer": start, "xfer": [[start, pool, 9 * 10**9]], "tok": {}},
            {"sig": home, "slot": 150, "ts": 2, "payer": start, "xfer": [[start, funder, 2 * 10**9], [start, dust, 10_000]], "tok": {}},
        ], "more": False}
        memo = {
            key("after", start, "MINT", 100, CM.SOL_FOLLOW_PAGES): after,
            key("kind", funder): "person", key("sigs-span", funder): 5,
        }
        source = CM.SolanaSource(None, memo, replay=True)
        source.token = "MINT"
        follow = CM.follow_forward(
            [start], {}, {start: {"from": funder}}, [], source, CM.SolanaLabels(source), 100,
            skip_hashes=frozenset({buy}), token="MINT",
        )
        flows = [f for f in follow["flows"] if f["hops"]]
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["stop"], "back to the start's first funder")
        self.assertEqual(flows[0]["hops"][0]["hash"], home)
        self.assertAlmostEqual(flows[0]["total_eth"], 2.0)

    def test_sol_wrapped_into_own_wsol_account_is_not_followed(self):
        wallet, own_wsol = "45YFTbsUeJKqaSVVmgUT9NMUHVohDVUWMSmq1qfysDdb", "HXuUcGr3hLKEKzzWtWZgKacXiwjuMjRNco2jsdut27df"
        self.assertEqual(CM.wsol_account(wallet), own_wsol)
        page = {"txs": [{"sig": "x", "slot": 5, "ts": 1, "payer": wallet, "xfer": [[wallet, own_wsol, 3 * 10**9], [wallet, SPIN_FUNDER, 10**9]], "tok": {}}], "more": False}
        source = CM.SolanaSource(None, {json.dumps(["after", wallet, "MINT", 1, CM.SOL_FOLLOW_PAGES], separators=(",", ":")): page}, replay=True)
        source.token = "MINT"
        self.assertEqual([r["to"] for r in source.outgoing_native(wallet, 1)], [SPIN_FUNDER])

    def test_a_wallet_doing_1000_txs_in_minutes_counts_past_the_exchange_line(self):
        # Nov 15, mihao control: AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51 did 1,000 txs in 192 s (an exchange hot
        # wallet) but read as 1,000 txs, under SUPERNODE_NONCE, so it linked 52 unrelated buyers
        class Rpc:
            def __init__(self, span):
                self.span = span

            def call(self, method, params, cacheable=True):
                return [{"signature": f"s{i}", "blockTime": 1_000_000 - i * self.span // 999} for i in range(1000)]

        fast = CM.SolanaSource(Rpc(192))
        slow = CM.SolanaSource(Rpc(30 * 86400))
        self.assertGreaterEqual(fast.signature_count(SPIN_FUNDER), CM.SUPERNODE_NONCE)
        self.assertEqual(slow.signature_count(SPIN_FUNDER), 1000)

    def test_missing_helius_key_says_where_to_get_one(self):
        old = os.environ.pop("HELIUS_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit) as ctx:
                    CM.build_clients("solana", CM.DiskCache(tmp), CM.RequestStats())
            self.assertIn("helius.dev", str(ctx.exception))
        finally:
            if old is not None:
                os.environ["HELIUS_API_KEY"] = old


class SpinFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(os.path.join(FIXTURES, "spin-solana.json.gz"), "rt", encoding="utf-8") as handle:
            fixture = json.load(handle)
        source = CM.SolanaSource(None, fixture["source"]["solana"], replay=True)
        with tempfile.TemporaryDirectory() as tmp:
            cls.report, cls.summary, _ = CM.run_cluster_map(
                SPIN_MINT, None, fixture["meta"]["top"], os.path.join(tmp, "cache"),
                blockscout_client=source, rpc_client=CM.SolanaLabels(source),
                generated_at="2026-11-14T00:00:00Z", chain_key="solana",
            )

    def fanout_cluster(self):
        found = [c for c in self.report["clusters"] if SPIN_FANOUT in c["wallets"]]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_fanout_is_one_cluster(self):
        cluster = self.fanout_cluster()
        linked = {
            link["a"] if link["b"] == SPIN_FANOUT else link["b"]
            for link in cluster["links"]["direct"]
            if SPIN_FANOUT in (link["a"], link["b"])
        }
        self.assertGreaterEqual(len(linked), 40)
        self.assertTrue(linked <= set(cluster["wallets"]))
        others = [c for c in self.report["clusters"] if c is not cluster and set(c["wallets"]) & linked]
        self.assertEqual(others, [])

    def test_funder_shows_as_a_magic_node(self):
        cluster = self.fanout_cluster()
        magic = {m["address"]: m for m in cluster["links"]["magic"]}
        self.assertIn(SPIN_FUNDER, magic)
        self.assertGreaterEqual(len(magic[SPIN_FUNDER]["wallets"]), 40)

    def test_pool_is_not_a_person(self):
        self.assertNotIn(SPIN_POOL, {p["address"] for p in self.report["persons"]})

    def test_text_says_sol_not_eth(self):
        self.assertIn("SOL", self.fanout_cluster()["why"])
        self.assertNotRegex(self.fanout_cluster()["why"], r"\bETH\b")
        self.assertNotRegex(self.summary, r"\bETH\b")

    def test_card_says_where_the_fanout_clusters_sol_went(self):
        after = self.fanout_cluster()["after"]
        self.assertTrue(after.startswith(f"After launch: SOL went to {SPIN_COLLECTOR} (the coin's creator) · 19 wallets · 23.68 SOL"), after)
        self.assertIn(after, self.summary)
        collector = [c for c in self.report["follow_forward"]["converge"] if c["address"] == SPIN_COLLECTOR]
        self.assertEqual(len(collector[0]["starts"]), 19)

    def test_sol_wrapped_for_trades_is_not_followed(self):
        own = {CM.wsol_account(s) for s in self.report["follow_forward"]["starts"]}
        ends = {f["ends_at"] for f in self.report["follow_forward"]["flows"]}
        self.assertEqual(own & ends, set())
        self.assertIn("read: money after launch: SOL sent in each wallet's first 200 transactions after launch", self.summary)

    def test_summary_has_no_shortened_address(self):
        self.assertNotRegex(self.summary, SHORT_ADDR)
        self.assertIn(f"All 94 wallets got SOL from the same wallet {SPIN_FUNDER}", self.summary)

    def test_headline_says_bought_and_held(self):
        self.assertIn("\n94 wallets that are probably one person or team bought 8.3% of the supply at launch and hold 1.3% now. "
                      "2 more groups bought 5.5% at launch and hold 1.4% now.\n", self.summary)

    def test_money_after_launch_in_plain_words(self):
        self.assertIn("\nFollowed 49 SOL payments out of 20 wallets; 0 went back to a wallet that funded them or is linked to them.\n",
                      self.summary)
        self.assertIn(f"\n  {SPIN_COLLECTOR} received SOL from 19 of those wallets\n", self.summary)
        for word in JARGON:
            self.assertNotIn(word, self.summary)

    def test_creator_line_says_it_received_followed_sol(self):
        self.assertEqual(self.report["header"]["creator"], SPIN_COLLECTOR)
        self.assertIn(f"\nCreator {SPIN_COLLECTOR} is not in a cluster, but 19 followed wallets sent it SOL after launch.\n", self.summary)
        self.assertNotIn("is not linked to any cluster", self.summary)
        self.assertIn(f"After launch: SOL went to {SPIN_COLLECTOR} (the coin's creator) · 19 wallets · 23.68 SOL", self.summary)

    def test_big_clusters_list_twenty_wallets(self):
        lines = self.summary.splitlines()
        self.assertLess(len(lines), 110)
        rows = sorted(self.report["clusters"][0]["wallet_rows"], key=lambda r: (-r["share_now"], -r["bought_at_launch"]))
        self.assertIn(f"  {rows[19]['address']} · holds", self.summary)
        self.assertNotIn(f"  {rows[20]['address']} · holds", self.summary)
        rest = rows[20:]
        more = (f"  +74 more wallets holding {CM.fmt_share(sum(r['share_now'] for r in rest))} now, "
                f"bought {CM.fmt_share(sum(r['bought_at_launch'] for r in rest))} at launch (full list: run with --out <folder>)")
        self.assertIn(more, lines)

    def test_header_names_the_chain_once(self):
        self.assertTrue(self.summary.startswith(f"SPIN · Solana · {SPIN_MINT}\n"))


if __name__ == "__main__":
    unittest.main()
