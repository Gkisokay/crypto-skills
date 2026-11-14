import sys
import unittest
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import launch_check as cc  # noqa: E402

CREATOR = "0x48ad71ab6467b0d0d9c5e23caf77452a343cdeeb"


class WalletKind(unittest.TestCase):
    def test_eip7702_smart_eoa_is_still_a_person(self):
        # a wallet seen on Base with EIP-7702 delegation code 0xef0100…
        self.assertEqual(cc.wallet_kind("0xef0100e6cae83bde06e4c305530e199d7217f42808555b"), "person (7702)")

    def test_plain_eoa_and_contract(self):
        self.assertEqual(cc.wallet_kind("0x"), "person")
        self.assertEqual(cc.wallet_kind("0x6080604052"), "contract")


class FunderClusters(unittest.TestCase):
    def test_creator_funding_counts_at_any_size(self):
        # seen on a real launch: the minting wallet itself sent gas money to the first buyers
        cl = cc.funder_clusters([(CREATOR, "0xa"), (CREATOR, "0xb")], CREATOR)
        self.assertEqual(cl, {CREATOR: ["0xa", "0xb"]})

    def test_other_funder_needs_three_wallets(self):
        sends = [("0xf1", "0xa"), ("0xf1", "0xb"), ("0xf2", "0xa"), ("0xf2", "0xb"), ("0xf2", "0xc")]
        self.assertEqual(list(cc.funder_clusters(sends, CREATOR)), ["0xf2"])

    def test_repeat_sends_to_one_wallet_dont_make_a_cluster(self):
        self.assertEqual(cc.funder_clusters([("0xf1", "0xa")] * 5, CREATOR), {})


class Supply(unittest.TestCase):
    def test_real_fdv_excludes_burned_supply(self):
        # 4B minted, 3B burned: an FDV on 4B reads ~$17.5M, on the 1B still in existence ~$4.4M at $0.004386
        self.assertAlmostEqual(cc.real_fdv(0.004386, 4e9, 3e9), 4.386e6)


class Replay(unittest.TestCase):
    def test_balances_from_transfers(self):
        def log(frm, to, amt):
            return {"topics": [cc.TRANSFER, "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]], "data": hex(amt)}
        z, a, b = cc.ZERO, "0x" + "a" * 40, "0x" + "b" * 40
        bal = cc.replay([log(z, a, 100), log(a, b, 30)])
        self.assertEqual((bal[a], bal[b], bal[z]), (70, 30, -100))


class Flags(unittest.TestCase):
    def base(self, **kw):
        r = {"supply": {"real_fdv": 4.4e6}, "market": {"dex_fdv": 4.5e6}, "creator": CREATOR,
             "creator_share": 0.0, "clusters": [], "holders": {}}
        r.update(kw)
        return r

    def test_clean_report_has_no_flags(self):
        self.assertEqual(cc.flags(self.base()), [])

    def test_creator_share_over_5pct(self):
        self.assertIn("40.0%", cc.flags(self.base(creator_share=0.40))[0])

    def test_dexscreener_fdv_mismatch(self):
        f = cc.flags(self.base(market={"dex_fdv": 17.5e6}))
        self.assertTrue(any("DexScreener FDV" in x for x in f))

    def test_creator_funded_cluster_names_the_creator(self):
        f = cc.flags(self.base(clusters=[{"funder": CREATOR, "wallets": ["0xa"] * 18, "bought_share": 0.259,
                                          "held_share": 0.229}]))
        self.assertTrue(f[0].startswith("The creator funded 18"))

    def test_cluster_flow_note_joins_without_dot_semicolon(self):
        f = cc.flags(self.base(clusters=[{"funder": CREATOR, "wallets": ["0xa"] * 4, "bought_share": 0.50,
                                          "held_share": 0.0, "flow_note": "; they hold 0.0% now (sold 50.0% into the pools)."}]))
        self.assertIn("bought 50.0%; they hold 0.0% now", f[0])
        self.assertNotIn(".;", f[0])


class RpcBatch(unittest.TestCase):
    def setUp(self):
        self._http, self._sleep = cc.http_json, cc.time.sleep
        cc.time.sleep = lambda s: None

    def tearDown(self):
        cc.http_json, cc.time.sleep = self._http, self._sleep

    def test_item_error_is_retried_not_read_as_empty(self):
        # Robinhood's RPC rate-limits single items inside a batch (Nov 10); those must not become empty blocks
        replies = [[{"id": 0, "result": "0x1"}, {"id": 1, "error": {"code": 429}}],
                   [{"id": 0, "result": "0x1"}, {"id": 1, "result": "0x2"}]]
        cc.http_json = lambda url, body=None: replies.pop(0)
        self.assertEqual(cc.Rpc("u", 1000, 1).batch([("m", []), ("m", [])]), ["0x1", "0x2"])

    def test_reverted_item_returns_none_without_retry(self):
        calls = []

        def one_revert(url, body=None):
            calls.append((url, body))
            return [{"id": 1, "result": "0x2"}, {"id": 0, "error": {"code": 3, "message": "execution reverted"}}]

        cc.http_json = one_revert
        self.assertEqual(cc.Rpc("u", 1000, 1).batch([("m", []), ("m", [])]), [None, "0x2"])
        self.assertEqual(len(calls), 1)

    def test_block_search_never_asks_for_ancient_blocks(self):
        # publicnode refuses old blocks as archive requests, so the search starts near the target time
        asked = []
        r = cc.Rpc("u", 1000, 1)
        r.block_ts = lambda n: asked.append(n) or n * 2  # 2s blocks, block n at t = 2n
        self.assertEqual(r.block_at(2 * 9_990_000, 10_000_000, 20_000_000, 2.0), 9_990_000)
        self.assertGreater(min(asked), 9_900_000)


class RpcCall(unittest.TestCase):
    def setUp(self):
        self._http, self._sleep = cc.http_json, cc.time.sleep
        cc.time.sleep = lambda s: None

    def tearDown(self):
        cc.http_json, cc.time.sleep = self._http, self._sleep

    def test_execution_reverted_is_not_retried(self):
        calls = []

        def reverted(url, body=None):
            calls.append((url, body))
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}}

        cc.http_json = reverted
        with self.assertRaisesRegex(RuntimeError, "execution reverted"):
            cc.Rpc("u", 1000, 1).call("eth_call", [])
        self.assertEqual(len(calls), 1)

    def test_logs_range_limit_error_is_not_retried(self):
        calls = []

        def limited(url, body=None):
            calls.append((url, body))
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "query spans 13223085 blocks; narrow the block range"}}

        cc.http_json = limited
        with self.assertRaisesRegex(RuntimeError, "query spans"):
            cc.Rpc("u", 1000, 1).call("eth_getLogs", [{}])
        self.assertEqual(len(calls), 1)


class HttpJson(unittest.TestCase):
    def setUp(self):
        self._open, self._sleep = cc.urllib.request.urlopen, cc.time.sleep
        cc.time.sleep = lambda s: None

    def tearDown(self):
        cc.urllib.request.urlopen, cc.time.sleep = self._open, self._sleep

    def test_archive_refusal_is_not_retried_as_throttling(self):
        # publicnode BNB without a token (Nov 11): old state answers HTTP 403 "Archive requests require a personal
        # token"; it must reach the pruned-state fallbacks as a RuntimeError, not be retried as rate limiting
        import io
        body = b'{"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a personal token."},"id":1}'
        calls = []

        def refuse(req, timeout=None):
            calls.append(req)
            raise cc.urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(body))

        cc.urllib.request.urlopen = refuse
        with self.assertRaisesRegex(RuntimeError, "Archive"):
            cc.http_json("https://rpc.example", {})
        self.assertEqual(len(calls), 1)


class NoArchive(unittest.TestCase):
    """publicnode Base/BNB without a token (Nov 11): an old token's first pool is past the RPC's recent state."""

    def setUp(self):
        self._saved = cc.Rpc, cc.market, cc.gt_holders

        class FakeRpc:
            url = "https://base-rpc.publicnode.com"

            def __init__(self, *a):
                pass

            def eth_call(self, to, data):
                return "0x12" if data == "0x313ce567" else hex(10**27)

            def batch(self, calls):
                return ["0x0"] * len(calls)

            def call(self, method, params):
                return "0x3556f6d"

            def block_ts(self, n):
                return n * 2

            def block_at(self, *a):
                raise RuntimeError('{"error":{"message":"Archive requests require a personal token."}}')

        cc.Rpc = FakeRpc
        cc.market = lambda addr, chain: ("base", {"symbol": "OLD", "price": 1.0, "liquidity": 1.0, "dex_fdv": 1.0,
                                                 "first_pair_ts": 1, "pairs": []})
        cc.gt_holders = lambda chain, addr: None

    def tearDown(self):
        cc.Rpc, cc.market, cc.gt_holders = self._saved

    def test_old_token_gets_a_note_not_a_crash(self):
        r = cc.check("0x" + "ab" * 20, "base")
        self.assertTrue(any("first 7 days" in n for n in r["notes"]), r["notes"])

    def test_report_says_launch_checks_did_not_run(self):
        out = cc.fmt(cc.check("0x" + "ab" * 20, "base"))
        self.assertIn("launch checks didn't run", out)
        self.assertNotIn("Too many transfers", out)


class AgeGate(unittest.TestCase):
    def setUp(self):
        self._saved = cc.Rpc, cc.market, cc.gt_holders
        made = []

        class FakeRpc:
            url = "https://rpc.example"

            def __init__(self, *a):
                self.logs_calls = 0
                made.append(self)

            def eth_call(self, to, data):
                if data == "0x313ce567":
                    return "0x12"
                if data == "0x18160ddd":
                    return hex(10**24)
                if data.startswith("0x70a08231"):
                    return "0x0"
                raise AssertionError((to, data))

            def batch(self, calls):
                out = []
                for m, p in calls:
                    if m == "eth_call":
                        out.append("0x0")
                    else:
                        raise AssertionError((m, p))
                return out

            def call(self, method, params):
                if method == "eth_blockNumber":
                    return hex(2_000_000)
                raise AssertionError((method, params))

            def block_ts(self, n):
                return n

            def logs(self, flt, frm, to, cap=None):
                self.logs_calls += 1
                return [], True

        cc.Rpc = FakeRpc
        cc.market = lambda addr, chain: ("base", {"symbol": "OLD", "price": 1.0, "liquidity": 1.0, "dex_fdv": 1.0,
                                                   "first_pair_ts": 2_000_000 - 16 * 86400, "pairs": [], "pairs_any": [], "pair_liquidity": {}})
        cc.gt_holders = lambda chain, addr: None
        self.made = made

    def tearDown(self):
        cc.Rpc, cc.market, cc.gt_holders = self._saved

    def test_old_coin_skips_launch_checks_without_log_scans(self):
        r = cc.check("0x" + "ab" * 20, "base")
        self.assertEqual(self.made[0].logs_calls, 0)
        self.assertTrue(any("16 days old" in n and "first 7 days" in n for n in r["notes"]), r["notes"])
        self.assertIn("launch checks didn't run", cc.fmt(r))


class MintMissingRugChecks(unittest.TestCase):
    def test_rug_checks_run_when_mint_missing_but_pool_block_known(self):
        token = "0x" + "ab" * 20
        pool = "0x" + "cd" * 20
        wallet = "0x" + "11" * 20
        wbnb = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
        saved = cc.Rpc, cc.market, cc.gt_holders

        class FakeRpc:
            url = "https://bsc-rpc.publicnode.com"
            chunk = 50000

            def __init__(self, *a):
                pass

            def eth_call(self, to, data):
                to = to.lower()
                if to == token and data == "0x313ce567":
                    return hex(18)
                if to == token and data == "0x18160ddd":
                    return hex(10**24)
                if to == pool and data == "0x18160ddd":
                    return hex(10**18 + 1000)
                if to == pool and data == "0x0dfe1681":
                    return "0x" + "0" * 24 + wbnb[2:]
                if to == pool and data == "0xd21220a7":
                    return "0x" + "0" * 24 + token[2:]
                if to == pool and data == "0xc45a0155":
                    raise RuntimeError("execution reverted")
                if to == wbnb and data == "0x313ce567":
                    return hex(18)
                if to == wbnb and data == "0x95d89b41":
                    return "0x" + hex(32)[2:].zfill(64) + hex(4)[2:].zfill(64) + b"WBNB".hex().ljust(64, "0")
                if to == wbnb and data.startswith("0x70a08231"):
                    return hex(500_000_000_000_000)  # 0.0005 left
                if data.startswith("0x70a08231"):
                    return "0x0"
                raise AssertionError((to, data))

            def call(self, method, params):
                if method == "eth_blockNumber":
                    return hex(1_000_000)
                raise AssertionError((method, params))

            def block_ts(self, n):
                return n * 3

            def block_at(self, t, head, head_ts, bt):
                return 900_000

            def logs(self, flt, frm, to, cap=None):
                addr = flt["address"]
                topics = flt["topics"]
                if addr == token and topics == [cc.TRANSFER, cc.ZERO_TOPIC]:
                    return [], True  # mint never found
                if addr == pool and topics == [cc.TRANSFER]:
                    return [
                        {"address": pool, "topics": [cc.TRANSFER, cc.ZERO_TOPIC, cc.ZERO_TOPIC], "data": hex(1000)},
                        {"address": pool, "topics": [cc.TRANSFER, cc.ZERO_TOPIC, "0x" + "0" * 24 + wallet[2:]], "data": hex(10**18)},
                    ], True
                if addr == [pool] and topics == [[cc.BURN_V2, cc.BURN_V3, cc.MINT_V2, cc.MINT_V3]]:
                    return [{
                        "address": pool,
                        "blockNumber": "0x100",
                        "transactionHash": "0x" + "9" * 64,
                        "topics": [cc.BURN_V2, "0x" + "0" * 64, "0x" + "0" * 64],
                        "data": "0x" + hex(50 * 10**18)[2:].zfill(64) + hex(1)[2:].zfill(64),
                    }], True
                raise AssertionError((flt, frm, to, cap))

            def batch(self, calls):
                out = []
                for m, p in calls:
                    if m == "eth_call":
                        out.append("0x0")
                    elif m == "eth_getCode":
                        out.append("0x")
                    elif m == "eth_getTransactionByHash":
                        out.append({"from": wallet})
                    else:
                        raise AssertionError((m, p))
                return out

        cc.Rpc = FakeRpc
        cc.market = lambda a, c: ("bsc", {"symbol": "T", "price": 1.0, "dex_fdv": 1.0, "liquidity": 23500.0,
                                          "first_pair_ts": 2_700_000, "pairs": [pool], "pairs_any": [pool],
                                          "pair_liquidity": {pool: 23500.0}})
        cc.gt_holders = lambda c, a: None
        try:
            r = cc.check(token, "bsc")
        finally:
            cc.Rpc, cc.market, cc.gt_holders = saved
        self.assertTrue(any("Couldn't find the mint within 7 days" in n for n in r["notes"]), r["notes"])
        self.assertTrue(any("Liquidity can be pulled: wallet " + wallet in f for f in r["flags"]), r["flags"])
        self.assertTrue(any("Liquidity pulled: Wallet " + wallet in f and "50 WBNB" in f for f in r["flags"]), r["flags"])


class FirstFunders(unittest.TestCase):
    def test_finds_funding_an_hour_before_launch(self):
        # buyers funded 47-87 min before the mint (blocks 86643-87116, mint 87784)
        funded = {"0xa": 86643, "0xb": 87116}

        class FakeRpc:
            def batch(self, calls):
                out = []
                for m, p in calls:
                    if m == "eth_getBalance":
                        out.append(hex(10**18) if int(p[1], 16) >= funded[p[0]] else "0x0")
                    else:
                        b = int(p[0], 16)
                        out.append({"transactions": [{"from": CREATOR, "to": w, "value": hex(10**18)}
                                                     for w, fb in funded.items() if fb == b]})
                return out

        r = {"launch": {}}
        sends, unresolved = cc.first_funders(FakeRpc(), ["0xa", "0xb"], 87784, 88352, 2.6, r)
        self.assertEqual((sorted(sends), unresolved), ([(CREATOR, "0xa"), (CREATOR, "0xb")], 0))
        self.assertIn("first-funded block", r["launch"]["funding_method"])

    def test_funding_budget_timeout_returns_partial_and_note(self):
        funded = {"0xa": 86643, "0xb": 87116}
        state = {"block_batches": 0}
        old_mono, old_budget = cc.MONO, cc.FUNDING_BUDGET_S
        cc.FUNDING_BUDGET_S = 60
        cc.MONO = lambda: 61 if state["block_batches"] >= 1 else 0

        class FakeRpc:
            batch_size = 1

            def batch(self, calls):
                out = []
                for m, p in calls:
                    if m == "eth_getBalance":
                        out.append(hex(10**18) if int(p[1], 16) >= funded[p[0]] else "0x0")
                    elif m == "eth_getBlockByNumber":
                        state["block_batches"] += 1
                        b = int(p[0], 16)
                        out.append({"transactions": [{"from": CREATOR, "to": w, "value": hex(10**18)}
                                                     for w, fb in funded.items() if fb == b]})
                    else:
                        raise AssertionError((m, p))
                return out

        try:
            r = {"launch": {}, "notes": []}
            sends, unresolved = cc.first_funders(FakeRpc(), ["0xa", "0xb"], 87784, 88352, 2.6, r)
        finally:
            cc.MONO, cc.FUNDING_BUDGET_S = old_mono, old_budget
        self.assertEqual((sorted(sends), unresolved), ([(CREATOR, "0xa")], 1))
        self.assertTrue(any("Funding lookups stopped after 60 s" in n and "1 of 2 early buyers traced" in n for n in r["notes"]), r["notes"])

    def test_funding_within_budget_is_unchanged(self):
        funded = {"0xa": 86643, "0xb": 87116}
        old_mono, old_budget = cc.MONO, cc.FUNDING_BUDGET_S
        cc.FUNDING_BUDGET_S = 60
        cc.MONO = lambda: 0

        class FakeRpc:
            batch_size = 1

            def batch(self, calls):
                out = []
                for m, p in calls:
                    if m == "eth_getBalance":
                        out.append(hex(10**18) if int(p[1], 16) >= funded[p[0]] else "0x0")
                    elif m == "eth_getBlockByNumber":
                        b = int(p[0], 16)
                        out.append({"transactions": [{"from": CREATOR, "to": w, "value": hex(10**18)}
                                                     for w, fb in funded.items() if fb == b]})
                    else:
                        raise AssertionError((m, p))
                return out

        try:
            r = {"launch": {}, "notes": []}
            sends, unresolved = cc.first_funders(FakeRpc(), ["0xa", "0xb"], 87784, 88352, 2.6, r)
        finally:
            cc.MONO, cc.FUNDING_BUDGET_S = old_mono, old_budget
        self.assertEqual((sorted(sends), unresolved), ([(CREATOR, "0xa"), (CREATOR, "0xb")], 0))
        self.assertEqual(r["notes"], [])


class LogRanges(unittest.TestCase):
    def test_refused_range_shrinks_future_chunks(self):
        # Chainstack's free plan caps eth_getLogs at 100 blocks; the skill should settle on a size that works
        r = cc.Rpc("u", 10000, 1)
        calls = []

        def call(method, params):
            a, b = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            calls.append(b - a + 1)
            if b - a + 1 > 100:
                raise RuntimeError("eth_getLogs: block range too large")
            return []
        r.call = call
        got, complete = r.logs({}, 0, 999)
        self.assertTrue(complete)
        self.assertLessEqual(r.chunk, 100)

    def test_max_results_split_does_not_shrink_default_chunk(self):
        r = cc.Rpc("u", 1000, 1)
        calls = []
        failed = {"done": False}

        def call(method, params):
            a, b = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            calls.append((a, b))
            if a == 1000 and b == 1999 and not failed["done"]:
                failed["done"] = True
                raise RuntimeError("eth_getLogs: query exceeds max results 20000")
            return []

        r.call = call
        got, complete = r.logs({}, 0, 2999)
        self.assertTrue(complete)
        self.assertEqual(r.chunk, 1000)
        self.assertIn((2000, 2999), calls)  # next top-level span keeps original chunk


class ChainChunks(unittest.TestCase):
    def test_updated_chain_chunks(self):
        self.assertEqual(cc.CHAINS["robinhood"]["chunk"], 5_000_000)
        self.assertEqual(cc.CHAINS["base"]["chunk"], 50_000)
        self.assertEqual(cc.CHAINS["bsc"]["chunk"], 50_000)


class Solana(unittest.TestCase):
    def test_pump_curve_address_matches_known_curve(self):
        import solana_check as sc
        amd = "DDmGnuvroxVrb7CSyjYcezFU8gnAfhKasg4ZZezmpump"
        self.assertEqual(sc.pda([b"bonding-curve", sc.b58decode(amd)], sc.PUMP), "CJyaPwhfcRHL57Nrmr4yTV2egpWoGx9ZeKQjScDfi3Hd")

    def test_owner_deltas_counts_buyers_from_zero_and_ignores_other_mints(self):
        import solana_check as sc
        def bal(owner, mint, amt):
            return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}
        tx = {"meta": {"preTokenBalances": [bal("curve", "M", 1000.0)],
                       "postTokenBalances": [bal("curve", "M", 900.0), bal("buyer", "M", 100.0), bal("x", "OTHER", 5.0)]}}
        self.assertEqual(dict(sc.owner_deltas(tx, "M")), {"curve": -100.0, "buyer": 100.0})

    def test_helius_oldest_first_signatures_and_fallback(self):
        import solana_check as sc
        calls = []

        def helius(method, params):
            calls.append((method, params))
            if method == "getTransactionsForAddress":
                return {"data": [{"signature": "s0", "slot": 1, "blockTime": 1}, {"signature": "s1", "slot": 2, "blockTime": 2}],
                        "paginationToken": "x"}
            raise AssertionError((method, params))

        sigs, capped = sc.launch_sigs(helius, "https://mainnet.helius-rpc.com/?api-key=x", "mint")
        self.assertFalse(capped)
        self.assertEqual([s["signature"] for s in sigs], ["s0", "s1"])
        m, p = calls[0]
        self.assertEqual(m, "getTransactionsForAddress")
        self.assertEqual(p[1]["sortOrder"], "asc")

        fb_calls = []

        def fallback(method, params):
            fb_calls.append((method, params))
            if method == "getTransactionsForAddress":
                raise RuntimeError("boom")
            if method == "getSignaturesForAddress":
                return [{"signature": "new", "err": None}, {"signature": "old", "err": None}]
            raise AssertionError((method, params))

        sigs2, capped2 = sc.launch_sigs(fallback, "https://mainnet.helius-rpc.com/?api-key=x", "mint")
        self.assertFalse(capped2)
        self.assertEqual([s["signature"] for s in sigs2], ["old", "new"])
        self.assertTrue(any(m == "getSignaturesForAddress" for m, _ in fb_calls))

    def test_helius_pages_past_failed_sniper_spam_to_cover_the_launch_window(self):
        # $MAX (Nov 11 2026): 1,000 signatures, 964 of them failed snipes, only reached 55 s after creation
        import solana_check as sc
        calls = []
        page1 = [{"signature": f"f{i}", "slot": 1 + i // 20, "blockTime": 1 + i * 54 // 999, "err": {"x": 1} if i % 28 else None}
                 for i in range(1000)]
        page2 = [{"signature": f"burst{i}", "slot": 60, "blockTime": 55, "err": None} for i in range(24)]

        def helius(method, params):
            calls.append(params[1].get("paginationToken"))
            return {"data": page1, "paginationToken": "p2"} if len(calls) == 1 else {"data": page2, "paginationToken": None}

        sigs, capped = sc.launch_sigs(helius, "https://mainnet.helius-rpc.com/?api-key=x", "mint")
        self.assertFalse(capped)
        self.assertEqual(calls, [None, "p2"])
        self.assertEqual(sum(1 for s in sigs if s["signature"].startswith("burst")), 24)
        self.assertFalse(any(s.get("err") for s in sigs))

    def test_header_says_how_much_of_the_window_was_read(self):
        r = {"address": "M", "chain": "solana", "symbol": "MAX", "market": None, "flags": [], "notes": [], "cant_check": [],
             "supply": {"total": 1e9, "burned": 0, "real_fdv": None}, "holders": {},
             "launch": {"block": 1, "ts": 0, "creator": "C", "early_buyers": 19, "early_bought_share": 0.12, "window_s": 41}}
        self.assertIn("First 41s of trading: 19 wallets", cc.fmt(r))

    def test_bundle_flags_three_minute_on_sol(self):
        import solana_check as sc
        buys = {}
        vals = [4.0, 3.5, 2.8, 2.7, 2.5, 2.6, 2.9, 2.4, 2.2, 3.1, 2.8, 2.5, 2.0, 4.0]  # 40%
        for i, v in enumerate(vals):
            w = "w" + str(i)
            buys[w] = (v, 100 + i * 12)  # spread over 3 minutes
        holds = {w: 0.0 for w in buys}
        out = sc.bundle_flags_with_holding(cc, 100, buys, 100.0, holds)
        self.assertTrue(any("first 3 minutes of trading" in x and "40.0%" in x for x in out), out)

    def test_bundle_flags_likely_bundle_on_sol(self):
        import solana_check as sc
        buys = {}
        for i, v in enumerate((6.0, 6.1, 5.9, 6.0, 6.0)):  # 30%
            buys["w" + str(i)] = (v, 100 + i * 0.4)  # first 2s
        holds = {w: 0.0 for w in buys}
        out = sc.bundle_flags_with_holding(cc, 100, buys, 100.0, holds)
        self.assertTrue(any("Likely bundle" in x and "30.0%" in x for x in out), out)

    def test_bundle_flags_small_early_buying_no_new_flag(self):
        import solana_check as sc
        buys = {"w1": (2.0, 101), "w2": (3.0, 102), "w3": (4.0, 103)}
        out = sc.bundle_flags_with_holding(cc, 100, buys, 100.0, {"w1": 0.0, "w2": 0.0, "w3": 0.0})
        self.assertEqual(out, [])

    def test_early_buys_rebuy_counts_peak_not_gross(self):
        import solana_check as sc

        def bal(owner, mint, amt):
            return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}

        pool, w = "pool", "w1"
        txs = [
            {"slot": 1, "blockTime": 101, "meta": {"preTokenBalances": [bal(pool, "M", 1000)],
                                                    "postTokenBalances": [bal(pool, "M", 990), bal(w, "M", 10)]}},
            {"slot": 2, "blockTime": 102, "meta": {"preTokenBalances": [bal(pool, "M", 990), bal(w, "M", 10)],
                                                    "postTokenBalances": [bal(pool, "M", 1000)]}},
            {"slot": 3, "blockTime": 103, "meta": {"preTokenBalances": [bal(pool, "M", 1000)],
                                                    "postTokenBalances": [bal(pool, "M", 990), bal(w, "M", 10)]}},
        ]
        bought, _, _ = sc.early_buys(txs, "M", {w}, 100)
        self.assertEqual(bought[w], 10.0)

    def test_early_buys_wallet_to_wallet_not_double_counted(self):
        import solana_check as sc

        def bal(owner, mint, amt):
            return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}

        pool, w1, w2 = "pool", "w1", "w2"
        txs = [
            {"slot": 1, "blockTime": 101, "meta": {"preTokenBalances": [bal(pool, "M", 1000)],
                                                    "postTokenBalances": [bal(pool, "M", 990), bal(w1, "M", 10)]}},
            {"slot": 2, "blockTime": 102, "meta": {"preTokenBalances": [bal(w1, "M", 10)],
                                                    "postTokenBalances": [bal(w2, "M", 10)]}},
        ]
        bought, _, _ = sc.early_buys(txs, "M", {w1, w2}, 100)
        self.assertEqual((bought[w1], bought[w2]), (10.0, 0.0))

    def test_early_buys_total_stays_under_supply(self):
        import solana_check as sc

        def bal(owner, mint, amt):
            return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}

        pool, w1, w2 = "pool", "w1", "w2"
        txs = [
            {"slot": 1, "blockTime": 101, "meta": {"preTokenBalances": [bal(pool, "M", 1000)],
                                                    "postTokenBalances": [bal(pool, "M", 900), bal(w1, "M", 100)]}},
            {"slot": 2, "blockTime": 102, "meta": {"preTokenBalances": [bal(pool, "M", 900), bal(w1, "M", 100)],
                                                    "postTokenBalances": [bal(pool, "M", 950), bal(w1, "M", 50)]}},
            {"slot": 3, "blockTime": 103, "meta": {"preTokenBalances": [bal(pool, "M", 950)],
                                                    "postTokenBalances": [bal(pool, "M", 900), bal(w2, "M", 50)]}},
        ]
        kinds = {pool: "program", w1: "person", w2: "person"}
        bought, _, _, _, _, held = sc.early_flows(txs, "M", kinds, 100)
        self.assertEqual((bought[w1], bought[w2]), (100.0, 50.0))  # what each really bought, unscaled
        self.assertEqual(held, 100.0)  # the most held at once never exceeds what left the pool


class SolanaPeaks(unittest.TestCase):
    def test_per_wallet_buys_are_not_scaled_and_aggregate_is_peak_held(self):
        # A buys 30% and sells it back, then B buys 30%: each bought 30% (bundle sizes depend on it), and early
        # buyers never held more than 30% at once
        import solana_check as sc
        POOL, A, B, MINT = "pool", "walletA", "walletB", "m"

        def tx(slot, deltas):
            pre = [{"mint": MINT, "owner": o, "uiTokenAmount": {"uiAmount": 100.0}} for o in deltas]
            post = [{"mint": MINT, "owner": o, "uiTokenAmount": {"uiAmount": 100.0 + v}} for o, v in deltas.items()]
            return {"slot": slot, "blockTime": slot, "meta": {"preTokenBalances": pre, "postTokenBalances": post}}

        txs = [tx(1, {POOL: -30.0, A: 30.0}), tx(2, {A: -30.0, POOL: 30.0}), tx(3, {POOL: -30.0, B: 30.0})]
        kinds = {POOL: "program", A: "person", B: "person"}
        bought, _, _, _, _, peak_held = sc.early_flows(txs, MINT, kinds, 0)
        self.assertAlmostEqual(bought[A], 30.0)
        self.assertAlmostEqual(bought[B], 30.0)
        self.assertAlmostEqual(peak_held, 30.0)

    def test_three_minute_flag_uses_peak_held_not_resold_sum(self):
        buys = {"a": (60.0, 10), "b": (60.0, 20)}  # 120% by sum after resales; at most 40% held at once
        self.assertEqual(cc.bundle_groups(0, buys, 100.0, held=40.0)[0]["text"],
                         "2 wallets took 40.0% of supply in the first 3 minutes of trading.")


class SolanaFixtures(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures"

    def load(self, name):
        return json.loads((self.FIX / name).read_text())

    def kinds_from_fixture(self, sc, d):
        wallets = list(d["owner_kinds"])

        def call(method, params):
            if method != "getMultipleAccounts":
                raise AssertionError((method, params))
            return {"value": [None for _ in params[0]]}

        return sc.account_kinds(call, wallets)

    def test_bro_pool_curve_is_program_and_share_not_over_100(self):
        import solana_check as sc
        d = self.load("solana_bro.json")
        kinds = self.kinds_from_fixture(sc, d)
        self.assertEqual(kinds["2tHSj5CRX9uXRuDw5PJoSuWvw4JT9TFxFc8zZYFSVnyP"], "program")
        self.assertEqual(sum(1 for v in kinds.values() if v == "program"), 1)
        t0 = d["txs"][0]["blockTime"]
        launch = sc.launch_supply(d["supply_ui"], d["txs"], d["mint"], kinds)
        bought, _, _, _, _, held = sc.early_flows(d["txs"], d["mint"], kinds, t0)
        self.assertLessEqual(held / launch, 1.0)

    def test_spin_pool_program_buyer_and_fanout(self):
        import solana_check as sc
        d = self.load("solana_spin.json")
        kinds = self.kinds_from_fixture(sc, d)
        pool = "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM"
        src = "Hkm9m53BVZsV3SRf23zzq6BVEpcsfz2M6Ucsa837Nzk7"
        self.assertEqual(kinds[pool], "program")
        launch = sc.launch_supply(d["supply_ui"], d["txs"], d["mint"], kinds)
        bought, _, _, handoffs, hand_times, held = sc.early_flows(d["txs"], d["mint"], kinds, d["txs"][0]["blockTime"])
        self.assertAlmostEqual(100 * bought.get(src, 0.0) / launch, 2.3, delta=0.7)
        fresh = [w for w in handoffs.get(src, {}) if bought.get(w, 0) == 0]
        self.assertGreaterEqual(len(fresh), 40)
        flags = sc.handoff_flags(src, bought, handoffs, hand_times, launch, {w: 0.0 for w in fresh})
        self.assertTrue(flags and "sent tokens on to" in flags[0], flags)
        m = re.search(r"to (\d+) wallets that hadn't bought", flags[0])
        self.assertTrue(m and int(m.group(1)) >= 40, flags[0])
        self.assertTrue(all(bought.get(w, 0) == 0 for w in fresh))
        self.assertLessEqual(held / launch, 1.0)

    def test_antitrump_creator_mint_deposit_not_buy(self):
        import solana_check as sc
        d = self.load("solana_antitrump.json")
        kinds = self.kinds_from_fixture(sc, d)
        pool = "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC"
        creator = "7cW45tcSDSiog8poB5LFu7CD1g4TX6JYJ8s4Ur29cb5H"
        self.assertEqual(kinds[pool], "program")
        launch = sc.launch_supply(d["supply_ui"], d["txs"], d["mint"], kinds)
        t0 = d["txs"][0]["blockTime"]
        bought, _, first_ts, _, _, held = sc.early_flows(d["txs"], d["mint"], kinds, t0)
        self.assertEqual(bought.get(creator, 0), 0.0)
        buyers = [w for w, amt in bought.items() if amt > 0]
        buys = {w: (bought[w], first_ts.get(w, t0)) for w in buyers}
        flags = [g["text"] for g in cc.bundle_groups(t0, buys, launch, held)]
        self.assertFalse(any("1 wallets took 100.0%" in f for f in flags), flags)
        self.assertLessEqual(held / launch, 1.0)


class FirstBuy(unittest.TestCase):
    def log(self, blk, frm, to):
        return {"blockNumber": hex(blk), "topics": [cc.TRANSFER, "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]],
                "data": "0x1"}

    def test_trading_starts_when_pool_pays_a_buyer_not_at_mint(self):
        # minted at 23:05, pool added ~11h later
        pool, buyer = "0x" + "1" * 40, "0x" + "2" * 40
        logs = [self.log(100, cc.ZERO, CREATOR), self.log(5000, CREATOR, pool), self.log(5002, pool, buyer)]
        self.assertEqual(cc.first_buy_block(logs, CREATOR, "0x" + "3" * 40), 5002)

    def test_curve_paying_the_creator_is_not_a_buy(self):
        # a bonding curve that first sent tokens to the minting wallet, which burned most of them
        curve, buyer = "0x" + "1" * 40, "0x" + "2" * 40
        logs = [self.log(10, cc.ZERO, curve), self.log(10, curve, CREATOR), self.log(12, curve, buyer)]
        self.assertEqual(cc.first_buy_block(logs, CREATOR, "0x" + "3" * 40), 12)


class Multisend(unittest.TestCase):
    def test_three_buyers_funded_inside_one_contract_call_are_a_cluster(self):
        # seen on BNB: most first buyers were funded inside contract calls, not direct sends
        funded = {"0xa": 500, "0xb": 500, "0xc": 500, "0xd": 700}

        class FakeRpc:
            def batch(self, calls):
                out = []
                for m, p in calls:
                    if m == "eth_getBalance":
                        out.append(hex(1) if int(p[1], 16) >= funded[p[0]] else "0x0")
                    else:
                        out.append({"transactions": [{"from": "0xrouter", "to": "0xdisperse", "value": hex(3)}]})
                return out

        sends, unresolved = cc.first_funders(FakeRpc(), list(funded), 1000, 1100, 3.0, {"launch": {}})
        self.assertEqual(sorted(w for f, w in sends if f == "contract call in block 500"), ["0xa", "0xb", "0xc"])
        self.assertEqual(unresolved, 1)
        f = cc.flags({"supply": {}, "market": {}, "clusters": [{"funder": "contract call in block 500", "wallets": ["0xa"] * 3,
                                                                "bought_share": 0.05, "held_share": 0.0}], "holders": {}})
        self.assertIn("multisend", f[0])

    def test_tiny_fdv_mismatch_is_not_flagged(self):
        self.assertEqual(cc.flags({"supply": {"real_fdv": 0.4}, "market": {"dex_fdv": 2}, "clusters": [], "holders": {}}), [])


class Pacing(unittest.TestCase):
    def test_batches_count_each_call_against_the_rate(self):
        # Conduit (Elysium) allows 25 req/s and counts every call inside a batch
        r = cc.Rpc("u", 1000, 1, rps=20)
        slept = []
        real = cc.time.sleep
        cc.time.sleep = lambda s: slept.append(s)
        try:
            r._pace(20)
            r._pace(20)
        finally:
            cc.time.sleep = real
        self.assertGreaterEqual(slept[-1], 0.9)  # the second batch of 20 waits ~1s


class LiquidityPulls(unittest.TestCase):
    """Recorded from a public Base transaction (tx 0xf5d95153…20aa): the wallet that minted the token removed
    17.72 WETH from its Uniswap v2 pool 29 minutes after adding 15 WETH; the pool was left with 0.00074 WETH.
    The fixture records what the chain shows; it says nothing about why."""
    TOKEN = "0xeebaa9bcf270e227b52b6206bda794a74ebc43fe"
    POOL = "0xad72b3e45a2c999a24ae02cf355d9fc9937d161d"
    WETH = "0x4200000000000000000000000000000000000006"
    LP_REMOVER = "0xf5553fa8eb2991e9f003056e536eec629e292b24"
    ROUTER = "0x0000000000000000000000004752ba5dbc23f44d87826276bf6fd6b1c372ad24"
    TX = "0xf5d9515350ee7a80945f45c0c1e66564ab1d1fe8c5bf884a96cc0365ac9620aa"

    def fake(self, logs, pool_weth, senders=None):
        t = self

        class FakeRpc:
            def logs(self, flt, frm, to, cap=None):
                return [l for l in logs if l["address"] in flt["address"]], True

            def eth_call(self, to, data):
                if data == "0x0dfe1681":
                    return "0x" + "0" * 24 + t.WETH[2:]
                if data == "0xd21220a7":
                    return "0x" + "0" * 24 + t.TOKEN[2:]
                if data == "0x313ce567":
                    return hex(18)
                if data == "0x95d89b41":  # symbol() as an ABI string
                    return "0x" + hex(32)[2:].zfill(64) + hex(4)[2:].zfill(64) + b"WETH".hex().ljust(64, "0")
                return hex(pool_weth)  # balanceOf(pool)

            def batch(self, calls):
                return [{"from": (senders or {}).get(c[1][0], t.LP_REMOVER)} for c in calls]

            def block_ts(self, n):
                return 1794375412 + 1754 if n == 0x3557c00 else 1794375412

        return FakeRpc()

    def burn_v2(self, weth, tokens, tx=TX, block="0x3557c00"):
        return {"address": self.POOL, "blockNumber": block, "transactionHash": tx,
                "topics": [cc.BURN_V2, self.ROUTER, self.ROUTER],
                "data": "0x" + hex(weth)[2:].zfill(64) + hex(tokens)[2:].zfill(64)}

    def test_creator_removal_is_flagged_with_tx(self):
        logs = [self.burn_v2(0xf5e94eda6b32af79, 0x12e182dc4e359f685f17f11d)]
        out = cc.liquidity_pulls(self.fake(logs, 736889873114803), {self.POOL}, self.TOKEN, self.LP_REMOVER, 0, 0x3557c00, 1794375412)
        self.assertEqual(len(out), 1)
        self.assertIn("The creator", out[0])
        self.assertIn("17.72 WETH", out[0])
        self.assertIn(self.TX, out[0])
        self.assertIn("29 min after the first pool", out[0])

    def test_small_withdrawal_is_not_flagged(self):
        logs = [self.burn_v2(10**18, 10**20)]  # 1 WETH out of a pool that still holds 50
        self.assertEqual(cc.liquidity_pulls(self.fake(logs, 50 * 10**18), {self.POOL}, self.TOKEN, self.LP_REMOVER, 0, 0x3557c00, 1794375412), [])

    def test_zero_amount_v3_poke_is_ignored(self):
        poke = {"address": self.POOL, "blockNumber": "0x3557c00", "transactionHash": self.TX,
                "topics": [cc.BURN_V3, self.ROUTER, "0x" + "0" * 64, "0x" + "0" * 64], "data": "0x" + "0" * 192}
        self.assertEqual(cc.liquidity_pulls(self.fake([poke], 0), {self.POOL}, self.TOKEN, self.LP_REMOVER, 0, 0x3557c00, 1794375412), [])

    def mint_v2(self, weth, tokens, block):
        return {"address": self.POOL, "blockNumber": block, "transactionHash": "0x" + "77" * 32,
                "topics": [cc.MINT_V2, self.ROUTER], "data": "0x" + hex(weth)[2:].zfill(64) + hex(tokens)[2:].zfill(64)}

    def test_liquidity_put_back_by_the_same_wallet_is_not_a_pull(self):
        logs = [self.burn_v2(10 * 10**18, 10**20, block="0x3557c00"), self.mint_v2(10 * 10**18, 10**20, "0x3557c01")]
        out = cc.liquidity_pulls(self.fake(logs, 10 * 10**18), {self.POOL}, self.TOKEN, self.LP_REMOVER, 0, 0x3557c01, 1794375412)
        self.assertEqual(out, [])

    def test_removal_before_the_listed_pool_opening_never_prints_negative_minutes(self):
        logs = [self.burn_v2(0xf5e94eda6b32af79, 0x12e182dc4e359f685f17f11d)]
        listed_open = 1794375412 + 1754 + 1120 * 60  # DexScreener's opening time is later than the removal
        out = cc.liquidity_pulls(self.fake(logs, 736889873114803), {self.POOL}, self.TOKEN, self.LP_REMOVER, 0, 0x3557c00, listed_open)
        self.assertEqual(len(out), 1)
        self.assertNotRegex(out[0], r"-\d+(\.\d+)? ?(s|min|h)\b")
        self.assertIn("time unknown after the first pool opened", out[0])

    def test_small_removal_after_another_wallet_emptied_the_pool_is_not_a_pull(self):
        big, small = "0x" + "a1" * 32, "0x" + "b2" * 32
        other_a, other_b = "0x" + "a1" * 20, "0x" + "b2" * 20
        logs = [self.burn_v2(99 * 10**18, 10**20, tx=big), self.burn_v2(5 * 10**16, 10**18, tx=small)]
        rpc = self.fake(logs, 10**15, senders={big: other_a, small: other_b})
        out = cc.liquidity_pulls(rpc, {self.POOL}, self.TOKEN, self.LP_REMOVER, 0, 0x3557c00, 1794375412)
        self.assertEqual(len(out), 1, out)
        self.assertIn(other_a, out[0])

    def test_removal_is_timed_from_that_pools_own_first_liquidity_add(self):
        logs = [self.mint_v2(15 * 10**18, 10**20, "0x3557bff"), self.burn_v2(0xf5e94eda6b32af79, 0x12e182dc4e359f685f17f11d)]
        listed_open = 1794375412 + 1754 + 1120 * 60
        out = cc.liquidity_pulls(self.fake(logs, 736889873114803), {self.POOL}, self.TOKEN, self.LP_REMOVER, 0, 0x3557c00, listed_open)
        self.assertEqual(len(out), 1, out)
        self.assertIn("29 min after liquidity was first added to that pool", out[0])


class LpControl(unittest.TestCase):
    CREATOR = "0xf5553fa8eb2991e9f003056e536eec629e292b24"
    POOL = "0xad72b3e45a2c999a24ae02cf355d9fc9937d161d"
    LOCKER = "0x1111111111111111111111111111111111111111"
    MANAGER = "0x03a520b32c04bf3beef7beb72e919cf822ed34f1"
    MINT_V3 = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
    INCREASE_V3 = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"

    def topic(self, addr):
        return "0x" + "0" * 24 + addr[2:].lower()

    def word(self, n):
        return hex(n)[2:].zfill(64)

    def transfer(self, frm, to, amt):
        return {"address": self.POOL,
                "topics": [cc.TRANSFER, self.topic(frm), self.topic(to)],
                "data": hex(amt)}

    def mint_v3(self, tx, owner, liq, tick_lo="0x" + "0" * 63 + "1", tick_hi="0x" + "0" * 63 + "2"):
        return {"address": self.POOL, "transactionHash": tx,
                "topics": [self.MINT_V3, self.topic(owner), tick_lo, tick_hi],
                "data": "0x" + self.word(1) + self.word(liq) + self.word(2) + self.word(3)}

    def burn_v3(self, tx, owner, liq, tick_lo="0x" + "0" * 63 + "1", tick_hi="0x" + "0" * 63 + "2"):
        return {"address": self.POOL, "transactionHash": tx,
                "topics": [cc.BURN_V3, self.topic(owner), tick_lo, tick_hi],
                "data": "0x" + self.word(liq) + self.word(2) + self.word(3)}

    def increase_log(self, token_id):
        return {"address": self.MANAGER, "topics": [self.INCREASE_V3, "0x" + self.word(token_id)]}

    def positions_resp(self, liq):
        return "0x" + self.word(0) * 7 + self.word(liq) + self.word(0) * 4

    def owner_resp(self, owner):
        return "0x" + "0" * 24 + owner[2:].lower()

    def fake_rpc(self, transfer_logs=None, mint_logs=None, burn_logs=None, total_supply=0, codes=None,
                 receipts=None, positions=None, owners=None, factory=None, fee_to=None):
        transfer_logs = transfer_logs or []
        mint_logs = mint_logs or []
        burn_logs = burn_logs or []
        codes = {k.lower(): v for k, v in (codes or {}).items()}
        receipts = receipts or {}
        positions = positions or {}
        owners = owners or {}
        factory = (factory or "").lower()
        fee_to = (fee_to or "").lower()

        class FakeRpc:
            def eth_call(self, to, data):
                to = to.lower()
                if data == "0x18160ddd":
                    return hex(total_supply)
                if data == "0xc45a0155":
                    if not factory:
                        raise RuntimeError("execution reverted")
                    return "0x" + "0" * 24 + factory[2:]
                if data == "0x017e7e58":
                    if to != factory or not fee_to:
                        raise RuntimeError("execution reverted")
                    return "0x" + "0" * 24 + fee_to[2:]
                if data == "0x6352211e":
                    raise AssertionError("ownerOf without tokenId")
                raise AssertionError((to, data))

            def logs(self, flt, frm, to, cap=None):
                addrs = flt["address"] if isinstance(flt["address"], list) else [flt["address"]]
                topics = flt.get("topics") or []
                if topics == [cc.TRANSFER]:
                    source = transfer_logs
                elif topics == [LpControl.MINT_V3]:
                    source = mint_logs
                elif topics == [cc.BURN_V3]:
                    source = burn_logs
                else:
                    raise AssertionError((topics, frm, to, cap))
                return [x for x in source if x["address"] in addrs], True

            def batch(self, calls):
                out = []
                for method, params in calls:
                    if method == "eth_getCode":
                        out.append(codes.get(params[0].lower(), "0x"))
                        continue
                    if method == "eth_getTransactionReceipt":
                        out.append(receipts.get(params[0], {"logs": []}))
                        continue
                    if method == "eth_call":
                        to, data = params[0]["to"].lower(), params[0]["data"]
                        if data.startswith("0x99fbab88"):
                            token_id = int(data[10:], 16)
                            out.append(positions[token_id])
                            continue
                        if data.startswith("0x6352211e"):
                            token_id = int(data[10:], 16)
                            out.append(owners[token_id])
                            continue
                        raise AssertionError((method, params))
                    raise AssertionError((method, params))
                return out

        return FakeRpc()

    def test_montra_pre_pull_state_flags_creator_lp_control(self):
        logs = [self.transfer(cc.ZERO, cc.ZERO, 1000), self.transfer(cc.ZERO, self.CREATOR, 10**18)]
        flags, notes = cc.lp_control(self.fake_rpc(transfer_logs=logs, total_supply=10**18 + 1000), {self.POOL}, self.CREATOR, 0, 1)
        self.assertEqual(len(flags), 1)
        self.assertIn("Liquidity can be pulled", flags[0])
        self.assertIn("(the creator)", flags[0])
        self.assertIn(self.CREATOR, flags[0])
        self.assertIn("100.0%", flags[0])
        self.assertEqual(notes, [])

    def test_dead_holding_lp_is_burned_not_flagged(self):
        logs = [self.transfer(cc.ZERO, cc.ZERO, 1000), self.transfer(cc.ZERO, self.CREATOR, 10**18),
                self.transfer(self.CREATOR, cc.DEAD, 10**18)]
        flags, notes = cc.lp_control(self.fake_rpc(transfer_logs=logs, total_supply=10**18 + 1000), {self.POOL}, self.CREATOR, 0, 1)
        self.assertEqual(flags, [])
        self.assertTrue(any("burned" in n and self.POOL in n for n in notes), notes)

    def test_contract_holding_lp_adds_locker_note(self):
        logs = [self.transfer(cc.ZERO, cc.ZERO, 1000), self.transfer(cc.ZERO, self.LOCKER, 10**18)]
        codes = {self.LOCKER: "0x6080604052"}
        flags, notes = cc.lp_control(self.fake_rpc(transfer_logs=logs, total_supply=10**18 + 1000, codes=codes), {self.POOL}, self.CREATOR, 0, 1)
        self.assertEqual(flags, [])
        self.assertTrue(any("may be a locker" in n and self.LOCKER in n for n in notes), notes)

    def test_pair_burn_on_liquidity_removal_is_not_counted_as_burned_lp(self):
        logs = [self.transfer(cc.ZERO, cc.ZERO, 1000), self.transfer(cc.ZERO, self.CREATOR, 10**18),
                self.transfer(self.CREATOR, self.POOL, 5 * 10**17), self.transfer(self.POOL, cc.ZERO, 5 * 10**17)]
        _, notes = cc.lp_control(self.fake_rpc(transfer_logs=logs, total_supply=5 * 10**17 + 1000), {self.POOL}, self.CREATOR, 0, 1)
        self.assertFalse(any("burned" in n for n in notes), notes)

    def test_pool_it_cannot_classify_is_reported_not_silently_skipped(self):
        flags, notes = cc.lp_control(self.fake_rpc(), {self.POOL}, self.CREATOR, 0, 1)
        self.assertEqual(flags, [])
        self.assertTrue(any("wasn't checked" in n and self.POOL in n for n in notes), notes)

    def test_v2_fee_to_mint_after_removal_is_excluded(self):
        fee_to = "0x9bd25e67bf390437c8faf480ac735a27bcf6168c"
        factory = "0x7777777777777777777777777777777777777777"
        logs = [self.transfer(cc.ZERO, cc.ZERO, 1000), self.transfer(cc.ZERO, self.CREATOR, 10**18),
                self.transfer(self.CREATOR, self.POOL, 10**18), self.transfer(self.POOL, cc.ZERO, 10**18),
                self.transfer(cc.ZERO, fee_to, 13360559746626339980)]
        flags, notes = cc.lp_control(self.fake_rpc(transfer_logs=logs, total_supply=13360559746626340980,
                                                   codes={fee_to: "0x6080604052"}, factory=factory, fee_to=fee_to),
                                     {self.POOL}, self.CREATOR, 0, 1)
        self.assertEqual(flags, [])
        self.assertFalse(any("locker" in n for n in notes), notes)

    def test_v3_creator_controls_most_liquidity(self):
        tx = "0x" + "a" * 64
        mints = [self.mint_v3(tx, self.MANAGER, 10**9)]
        rec = {tx: {"logs": [self.increase_log(7)]}}
        pos = {7: self.positions_resp(10**9)}
        own = {7: self.owner_resp(self.CREATOR)}
        flags, notes = cc.lp_control(self.fake_rpc(mint_logs=mints, receipts=rec, positions=pos, owners=own),
                                     {self.POOL}, self.CREATOR, 0, 1)
        self.assertEqual(len(flags), 1)
        self.assertIn("v3 positions", flags[0])
        self.assertIn(self.CREATOR, flags[0])
        self.assertIn("(the creator)", flags[0])
        self.assertEqual(notes, [])

    def test_v3_nft_in_contract_adds_locker_note(self):
        tx = "0x" + "b" * 64
        mints = [self.mint_v3(tx, self.MANAGER, 2000)]
        rec = {tx: {"logs": [self.increase_log(11)]}}
        pos = {11: self.positions_resp(2000)}
        own = {11: self.owner_resp(self.LOCKER)}
        codes = {self.LOCKER: "0x6080604052"}
        flags, notes = cc.lp_control(self.fake_rpc(mint_logs=mints, receipts=rec, positions=pos, owners=own, codes=codes),
                                     {self.POOL}, self.CREATOR, 0, 1)
        self.assertEqual(flags, [])
        self.assertTrue(any("may be a locker" in n and self.LOCKER in n for n in notes), notes)

    def test_v3_direct_position_fully_burned_has_no_flag(self):
        user = "0x9999999999999999999999999999999999999999"
        tx = "0x" + "c" * 64
        mints = [self.mint_v3(tx, user, 5000)]
        burns = [self.burn_v3(tx, user, 5000)]
        flags, notes = cc.lp_control(self.fake_rpc(mint_logs=mints, burn_logs=burns), {self.POOL}, self.CREATOR, 0, 1)
        self.assertEqual((flags, notes), ([], []))

    def test_v3_burned_nft_position_is_skipped(self):
        tx = "0x" + "d" * 64
        mints = [self.mint_v3(tx, self.MANAGER, 1000), self.mint_v3(tx, self.MANAGER, 2000, tick_lo="0x" + "0" * 63 + "3", tick_hi="0x" + "0" * 63 + "4")]
        rec = {tx: {"logs": [self.increase_log(7), self.increase_log(8)]}}
        pos = {7: self.positions_resp(1000), 8: None}
        own = {7: self.owner_resp(self.CREATOR)}
        held, total, burned = cc.lp_v3_state(self.fake_rpc(mint_logs=mints, receipts=rec, positions=pos, owners=own), self.POOL, 0, 1)
        self.assertEqual(held[self.CREATOR], 1000)
        self.assertEqual(total, 1000)
        self.assertEqual(burned, 0.0)

    def test_liquidity_pull_flag_requires_one_wallet_over_half(self):
        w1 = "0x1111111111111111111111111111111111111111"
        w2 = "0x2222222222222222222222222222222222222222"
        w3 = "0x3333333333333333333333333333333333333333"

        class FakeRpc:
            def batch(self, calls):
                return ["0x"] * len(calls)

        flags, _ = cc.lp_findings(FakeRpc(), self.POOL, self.CREATOR, {w1: 34, w2: 33, w3: 33}, 100, 0.0, "v2")
        self.assertEqual(flags, [])


class DustPools(unittest.TestCase):
    def test_checks_only_run_on_pools_over_min_liquidity(self):
        p_low = "0x" + "1" * 40
        p_hi = "0x" + "2" * 40
        v4_low = "0x" + "a" * 64
        v4_hi = "0x" + "b" * 64
        called = {}
        saved = cc.lp_control, cc.liquidity_pulls, cc.lp_control_v4, cc.gt_holders, cc.v4_manager

        class FakeRpc:
            def batch(self, calls):
                return []

        def lp_control(rpc, pools, creator, frm, to):
            called["lp"] = set(pools)
            return [], []

        def pulls(rpc, pools, token, creator, frm, to, start):
            called["pulls"] = set(pools)
            return []

        def v4(rpc, token, ids, creator, frm, to, start, manager=None, ids_control=None, pool_liquidity=None):
            called["v4"] = set(ids)
            called["v4_ctrl"] = set(ids_control or [])
            return [], []

        cc.lp_control, cc.liquidity_pulls, cc.lp_control_v4, cc.gt_holders, cc.v4_manager = lp_control, pulls, v4, (lambda c, a: None), (lambda *a: None)
        try:
            r = {"address": "0x" + "ab" * 20, "chain": "base", "notes": [], "supply": {},
                 "market": {"first_pair_ts": 1, "pairs": [p_low, p_hi], "pairs_any": [p_low, p_hi, v4_low, v4_hi],
                            "pair_liquidity": {p_low: 261.0, p_hi: 87000.0, v4_low: 261.0, v4_hi: 87000.0}}}
            cc.finish(r, FakeRpc(), r["address"], 1, 1, 0, 10, 20, [])
        finally:
            cc.lp_control, cc.liquidity_pulls, cc.lp_control_v4, cc.gt_holders, cc.v4_manager = saved
        self.assertEqual(called["lp"], {p_hi})
        self.assertEqual(called["pulls"], {p_low, p_hi})
        self.assertEqual(called["v4"], {v4_low, v4_hi})
        self.assertEqual(called["v4_ctrl"], {v4_hi})

    def test_pull_checks_run_on_all_pools_even_dust(self):
        p_low = "0x" + "1" * 40
        p_hi = "0x" + "2" * 40
        v4_low = "0x" + "a" * 64
        v4_hi = "0x" + "b" * 64
        called = {}
        saved = cc.lp_control, cc.liquidity_pulls, cc.lp_control_v4, cc.gt_holders, cc.v4_manager

        class FakeRpc:
            def batch(self, calls):
                return []

        def lp_control(rpc, pools, creator, frm, to):
            called["lp"] = set(pools)
            return ([], [])

        def pulls(rpc, pools, token, creator, frm, to, start):
            called["pulls"] = set(pools)
            return ["Liquidity pulled: creator on low pool"]

        def v4(rpc, token, ids, creator, frm, to, start, manager=None, ids_control=None, pool_liquidity=None):
            called["v4_ids"] = set(ids)
            called["v4_ctrl"] = set(ids_control or [])
            return ([], [])

        cc.lp_control, cc.liquidity_pulls, cc.lp_control_v4, cc.gt_holders, cc.v4_manager = lp_control, pulls, v4, (lambda c, a: None), (lambda *a: "0xpoolmgr")
        try:
            r = {"address": "0x" + "ab" * 20, "chain": "base", "notes": [], "supply": {},
                 "market": {"first_pair_ts": 1, "pairs": [p_low, p_hi], "pairs_any": [p_low, p_hi, v4_low, v4_hi],
                            "pair_liquidity": {p_low: 3.0, p_hi: 87000.0, v4_low: 261.0, v4_hi: 87000.0}}}
            out = cc.finish(r, FakeRpc(), r["address"], 1, 1, 0, 10, 20, [])
        finally:
            cc.lp_control, cc.liquidity_pulls, cc.lp_control_v4, cc.gt_holders, cc.v4_manager = saved
        self.assertEqual(called["lp"], {p_hi})
        self.assertEqual(called["pulls"], {p_low, p_hi})
        self.assertEqual(called["v4_ids"], {v4_low, v4_hi})
        self.assertEqual(called["v4_ctrl"], {v4_hi})
        self.assertTrue(any("Liquidity pulled" in x for x in out["flags"]), out["flags"])


class BundleTiming(unittest.TestCase):
    def test_seer_shape_flags_likely_bundle(self):
        buys = {}
        for i, pct in enumerate((4.93, 4.97, 5.02, 4.96, 5.01, 4.99, 4.95, 5.0, 4.98, 4.94)):
            w = "0x" + hex(i + 1)[2:].rjust(40, "0")
            buys[w] = (pct, 100.0 + i * 0.093)
        flags = cc.bundle_flags(100.0, buys, 100.0)
        self.assertTrue(any("Likely bundle" in f and "10 wallets" in f for f in flags), flags)

    def test_large_early_take_without_bundle_gets_3m_flag(self):
        vals = (1, 2, 3, 4, 5, 6, 2, 4, 3, 5)  # 35%
        buys = {}
        for i, pct in enumerate(vals):
            w = "0x" + hex(i + 1)[2:].rjust(40, "0")
            buys[w] = (pct, 100.0 + i * 12.0)
        flags = cc.bundle_flags(100.0, buys, 100.0)
        self.assertFalse(any("Likely bundle" in f for f in flags), flags)
        self.assertTrue(any("first 3 minutes" in f and "35.0%" in f for f in flags), flags)

    def test_small_early_buyers_have_no_timing_flag(self):
        buys = {"0x" + "1" * 40: (2.0, 101.0), "0x" + "2" * 40: (3.0, 102.0), "0x" + "3" * 40: (4.0, 103.0)}
        self.assertEqual(cc.bundle_flags(100.0, buys, 100.0), [])

    def test_bundle_wallets_sold_into_pools_note(self):
        pool = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
        ws = ["0x" + hex(i + 1)[2:].rjust(40, "0") for i in range(10)]
        logs = []
        for w in ws:
            logs.append({"topics": [cc.TRANSFER, "0x" + "0" * 24 + cc.ZERO[2:], "0x" + "0" * 24 + w[2:]], "data": hex(5)})
            logs.append({"topics": [cc.TRANSFER, "0x" + "0" * 24 + w[2:], "0x" + "0" * 24 + pool[2:]], "data": hex(5)})
        note = cc.wallet_flow_note(None, "0x" + "ab" * 20, ws, 100.0, 1, True, logs, cc.replay(logs), {pool})
        self.assertIn("hold 0.0% now", note)
        self.assertIn("sold 50.0% into the pools", note)

    def test_bundle_text_and_flow_note_join_without_dot_semicolon(self):
        saved = cc.lp_control, cc.lp_control_v4, cc.liquidity_pulls, cc.gt_holders, cc.v4_manager

        class FakeRpc:
            def batch(self, calls):
                return ["0x0"] * len(calls)

        cc.lp_control = lambda *a, **k: ([], [])
        cc.lp_control_v4 = lambda *a, **k: ([], [])
        cc.liquidity_pulls = lambda *a, **k: []
        cc.gt_holders = lambda *a, **k: None
        cc.v4_manager = lambda *a, **k: None
        try:
            r = {"address": "0x" + "ab" * 20, "chain": "base", "notes": [], "supply": {},
                 "market": {"first_pair_ts": 1, "pairs": [], "pairs_any": [], "pair_liquidity": {}},
                 "bundles": [{"wallets": ["0x" + "1" * 40],
                              "text": "Likely bundle: 10 wallets bought 50.0% of supply within 0.93 s of trading opening, in near-identical sizes (~5.0% each)."}]}
            out = cc.finish(r, FakeRpc(), r["address"], 1, 1, 0, None, 20)
        finally:
            cc.lp_control, cc.lp_control_v4, cc.liquidity_pulls, cc.gt_holders, cc.v4_manager = saved
        msg = next(x for x in out["flags"] if x.startswith("Likely bundle:"))
        self.assertIn("(~5.0% each); they hold", msg)
        self.assertNotIn(".;", msg)

    def test_bundle_wallets_moved_to_one_wallet_note(self):
        recv = "0x9999999999999999999999999999999999999999"
        ws = ["0x" + hex(i + 1)[2:].rjust(40, "0") for i in range(10)]
        logs = []
        for w in ws:
            logs.append({"topics": [cc.TRANSFER, "0x" + "0" * 24 + cc.ZERO[2:], "0x" + "0" * 24 + w[2:]], "data": hex(5)})
            logs.append({"topics": [cc.TRANSFER, "0x" + "0" * 24 + w[2:], "0x" + "0" * 24 + recv[2:]], "data": hex(4)})

        class FakeRpc:
            def batch(self, calls):
                return ["0x"] * len(calls)

        note = cc.wallet_flow_note(FakeRpc(), "0x" + "ab" * 20, ws, 100.0, 1, True, logs, cc.replay(logs), set())
        self.assertIn("hold 10.0% now", note)
        self.assertIn("40.0% moved to " + recv, note)
        self.assertIn("a plain wallet", note)
        self.assertIn("holds 40.0% now", note)


class HolderKinds(unittest.TestCase):
    def test_v4_pool_manager_is_labeled_pool_in_top_holders(self):
        token = "0x" + "ab" * 20
        mgr = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
        pool_id = "0x" + "4" * 64
        saved = cc.lp_control, cc.lp_control_v4, cc.liquidity_pulls, cc.gt_holders

        def t(frm, to, amt, txh):
            return {"transactionHash": txh,
                    "topics": [cc.TRANSFER, "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]], "data": hex(amt)}

        holder_logs = [t(cc.ZERO, mgr, 9926, "0x1"), t(cc.ZERO, "0x" + "1" * 40, 90000, "0x2")]
        token_logs = [dict(t("0x" + "2" * 40, "0x" + "3" * 40, 1, "0xaaa"), blockNumber="0x10", transactionIndex="0x0", logIndex="0x0")]

        class FakeRpc:
            def eth_call(self, to, data):
                if data.startswith("0x70a08231"):
                    return "0x0"
                raise AssertionError((to, data))

            def logs(self, flt, frm, to, cap=None):
                if flt["topics"] == [cc.TRANSFER]:
                    return token_logs, True
                raise AssertionError((flt, frm, to, cap))

            def batch(self, calls):
                out = []
                for m, p in calls:
                    if m == "eth_getCode":
                        out.append("0x6080604052")
                    elif m == "eth_getTransactionReceipt":
                        out.append({"logs": [{"address": mgr, "topics": [cc.SWAP_V4, pool_id]}]})
                    else:
                        raise AssertionError((m, p))
                return out

        cc.lp_control = lambda *a, **k: ([], [])
        cc.lp_control_v4 = lambda *a, **k: ([], [])
        cc.liquidity_pulls = lambda *a, **k: []
        cc.gt_holders = lambda *a, **k: None
        try:
            r = {"address": token, "chain": "robinhood", "notes": [], "market": {"pairs": [], "pairs_any": [pool_id],
                 "pair_liquidity": {pool_id: 87000.0}, "first_pair_ts": 1}, "supply": {}}
            out = cc.finish(r, FakeRpc(), token, 1, 100000, 0, 10, 20, holder_logs)
        finally:
            cc.lp_control, cc.lp_control_v4, cc.liquidity_pulls, cc.gt_holders = saved
        row = next(x for x in out["holders"]["top"] if x["address"] == mgr)
        self.assertEqual(row["kind"], "pool")


class V4Pools(unittest.TestCase):
    TOKEN = "0x24d18d0592180d79b563a49500c2da1480a3967a"
    CREATOR = "0xf5553fa8eb2991e9f003056e536eec629e292b24"
    POOL_ID = "0x41acccd31521fd4113f1a85f051981fdfdafaceb119c2aee91140c1363d0c685"
    POOL_MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
    POS_MGR = "0x7c5f5a4bbd8fd63184577525326123b519429bdc"
    LOCKER = "0x1111111111111111111111111111111111111111"
    HOOK = "0x2222222222222222222222222222222222222222"
    INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
    MODIFY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
    SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
    HOOK_TOPIC = "0x" + "9" * 64

    def word(self, n):
        return hex(n)[2:].zfill(64)

    def topic(self, addr):
        return "0x" + "0" * 24 + addr[2:].lower()

    def addr_word(self, addr):
        return "0" * 24 + addr[2:].lower()

    def i256(self, n):
        return n if n >= 0 else (1 << 256) + n

    def enc_str(self, s):
        b = s.encode()
        pad = ((len(b) + 31) // 32) * 64
        return "0x" + self.word(32) + self.word(len(b)) + b.hex().ljust(pad, "0")

    def transfer(self, tx):
        return {"address": self.TOKEN, "blockNumber": "0x10", "transactionIndex": "0x0", "logIndex": "0x0",
                "transactionHash": tx, "topics": [cc.TRANSFER, self.topic(self.CREATOR), self.topic(self.LOCKER)], "data": "0x1"}

    def init(self):
        data = "0x" + self.word(3000) + self.word(60) + self.addr_word(self.HOOK) + self.word(1) + self.word(0)
        return {"address": self.POOL_MANAGER, "blockNumber": "0x10", "transactionHash": "0xinit",
                "topics": [self.INITIALIZE, self.POOL_ID, self.topic(self.TOKEN), self.topic(self.LOCKER)], "data": data}

    def mod(self, tx, sender, lo, hi, delta, salt):
        data = "0x" + self.word(lo) + self.word(hi) + self.word(self.i256(delta)) + self.word(salt)
        return {"address": self.POOL_MANAGER, "blockNumber": "0x10", "transactionHash": tx,
                "topics": [self.MODIFY, self.POOL_ID, self.topic(sender)], "data": data}

    def fake_rpc(self, token_logs=None, manager_logs=None, receipts=None, tx_from=None, owners=None, names=None, codes=None):
        t = self
        token_logs = token_logs or []
        manager_logs = manager_logs or []
        receipts = receipts or {}
        tx_from = tx_from or {}
        owners = owners or {}
        names = {k.lower(): v for k, v in (names or {}).items()}
        codes = {k.lower(): v for k, v in (codes or {}).items()}

        class FakeRpc:
            def eth_call(self, to, data):
                to = to.lower()
                if data == "0x06fdde03":
                    if to not in names:
                        raise RuntimeError("execution reverted")
                    return t.enc_str(names[to])
                if data.startswith("0x6352211e"):
                    token_id = int(data[10:], 16)
                    return "0x" + "0" * 24 + owners[token_id][2:].lower()
                raise AssertionError((to, data))

            def block_ts(self, n):
                return 100 if n == 0x10 else 0

            def logs(self, flt, frm, to, cap=None):
                if flt["topics"] == [cc.TRANSFER]:
                    return token_logs, True
                if flt["topics"][0] == [V4Pools.INITIALIZE, V4Pools.MODIFY]:
                    return manager_logs, True
                raise AssertionError((flt, frm, to, cap))

            def batch(self, calls):
                out = []
                for method, params in calls:
                    if method == "eth_getTransactionReceipt":
                        out.append(receipts.get(params[0], {"logs": []}))
                    elif method == "eth_getTransactionByHash":
                        out.append(tx_from.get(params[0], {"from": V4Pools.CREATOR}))
                    elif method == "eth_getCode":
                        out.append(codes.get(params[0].lower(), "0x6080604052"))
                    elif method == "eth_call":
                        to, data = params[0]["to"].lower(), params[0]["data"]
                        if data == "0x06fdde03":
                            if to not in names:
                                raise RuntimeError("execution reverted")
                            out.append(t.enc_str(names[to]))
                        elif data.startswith("0x6352211e"):
                            token_id = int(data[10:], 16)
                            out.append("0x" + "0" * 24 + owners[token_id][2:].lower())
                        else:
                            raise AssertionError((method, params))
                    else:
                        raise AssertionError((method, params))
                return out

        return FakeRpc()

    def test_v4_contract_sender_gets_contract_and_hook_notes(self):
        tx = "0x" + "a" * 64
        receipts = {tx: {"logs": [{"address": self.POOL_MANAGER, "topics": [self.SWAP, self.POOL_ID]}]}}
        mods = [self.mod("0xmod", self.LOCKER, 1, 2, 1000, 1)]
        flags, notes = cc.lp_control_v4(self.fake_rpc(token_logs=[self.transfer(tx)], manager_logs=[self.init()] + mods, receipts=receipts),
                                        self.TOKEN, {self.POOL_ID}, self.CREATOR, 0, 20, 100)
        self.assertEqual(flags, [])
        self.assertTrue(any("may be a launchpad" in n and self.LOCKER in n for n in notes), notes)
        self.assertTrue(any("uses hook contract" in n and self.HOOK in n for n in notes), notes)

    def test_v4_manager_ignores_hook_log_before_real_manager(self):
        tx = "0x" + "e" * 64
        fake_hook = {"address": self.HOOK, "topics": [self.HOOK_TOPIC, self.POOL_ID]}
        real_mgr = {"address": self.POOL_MANAGER, "topics": [self.MODIFY, self.POOL_ID]}
        receipts = {tx: {"logs": [fake_hook, real_mgr]}}
        got = cc.v4_manager(self.fake_rpc(token_logs=[self.transfer(tx)], receipts=receipts),
                            self.TOKEN, {self.POOL_ID}, 0, 20)
        self.assertEqual(got, self.POOL_MANAGER)

    def test_v4_creator_owned_position_nft_flags_pullable(self):
        tx = "0x" + "b" * 64
        receipts = {tx: {"logs": [{"address": self.POOL_MANAGER, "topics": [self.SWAP, self.POOL_ID]}]}}
        mods = [self.mod("0xadd", self.POS_MGR, 1, 2, 5000, 9)]
        names = {self.POS_MGR: "Uniswap v4 Positions NFT"}
        owners = {9: self.CREATOR}
        txs = {"0xadd": {"from": self.CREATOR}}
        flags, notes = cc.lp_control_v4(self.fake_rpc(token_logs=[self.transfer(tx)], manager_logs=mods, receipts=receipts,
                                                      names=names, owners=owners, tx_from=txs),
                                        self.TOKEN, {self.POOL_ID}, self.CREATOR, 0, 20, 100)
        self.assertEqual(len(flags), 1)
        self.assertIn("Liquidity can be pulled", flags[0])
        self.assertIn(self.CREATOR, flags[0])
        self.assertEqual(notes, [])

    def test_v4_creator_removal_flags_liquidity_pull(self):
        tx = "0x" + "c" * 64
        receipts = {tx: {"logs": [{"address": self.POOL_MANAGER, "topics": [self.SWAP, self.POOL_ID]}]}}
        names = {self.POS_MGR: "Uniswap v4 Positions NFT"}
        owners = {7: self.CREATOR}
        mods = [self.mod("0xadd", self.POS_MGR, 1, 2, 5000, 7), self.mod("0xrm", self.POS_MGR, 1, 2, -5000, 7)]
        txs = {"0xadd": {"from": self.CREATOR}, "0xrm": {"from": self.CREATOR}}
        flags, _ = cc.lp_control_v4(self.fake_rpc(token_logs=[self.transfer(tx)], manager_logs=mods, receipts=receipts,
                                                  names=names, owners=owners, tx_from=txs),
                                    self.TOKEN, {self.POOL_ID}, self.CREATOR, 0, 20, 100)
        self.assertTrue(any("Liquidity pulled" in x and "0xrm" in x for x in flags), flags)

    def test_v4_creator_removal_high_liquidity_is_note_not_flag(self):
        tx = "0x" + "c" * 64
        receipts = {tx: {"logs": [{"address": self.POOL_MANAGER, "topics": [self.SWAP, self.POOL_ID]}]}}
        names = {self.POS_MGR: "Uniswap v4 Positions NFT"}
        owners = {7: self.CREATOR}
        mods = [self.mod("0xadd", self.POS_MGR, 1, 2, 5000, 7), self.mod("0xrm", self.POS_MGR, 1, 2, -5000, 7)]
        txs = {"0xadd": {"from": self.CREATOR}, "0xrm": {"from": self.CREATOR}}
        flags, notes = cc.lp_control_v4(self.fake_rpc(token_logs=[self.transfer(tx)], manager_logs=mods, receipts=receipts,
                                                      names=names, owners=owners, tx_from=txs),
                                        self.TOKEN, {self.POOL_ID}, self.CREATOR, 0, 20, 100,
                                        pool_liquidity={self.POOL_ID: 28000.0})
        self.assertFalse(any("Liquidity pulled" in x for x in flags), flags)
        self.assertTrue(any("but the pool still shows $28,000" in n for n in notes), notes)

    def test_v4_creator_removal_low_liquidity_stays_flag(self):
        tx = "0x" + "c" * 64
        receipts = {tx: {"logs": [{"address": self.POOL_MANAGER, "topics": [self.SWAP, self.POOL_ID]}]}}
        names = {self.POS_MGR: "Uniswap v4 Positions NFT"}
        owners = {7: self.CREATOR}
        mods = [self.mod("0xadd", self.POS_MGR, 1, 2, 5000, 7), self.mod("0xrm", self.POS_MGR, 1, 2, -5000, 7)]
        txs = {"0xadd": {"from": self.CREATOR}, "0xrm": {"from": self.CREATOR}}
        flags, notes = cc.lp_control_v4(self.fake_rpc(token_logs=[self.transfer(tx)], manager_logs=mods, receipts=receipts,
                                                      names=names, owners=owners, tx_from=txs),
                                        self.TOKEN, {self.POOL_ID}, self.CREATOR, 0, 20, 100,
                                        pool_liquidity={self.POOL_ID: 3.0})
        self.assertTrue(any("Liquidity pulled" in x for x in flags), flags)
        self.assertFalse(any("but the pool still shows" in n for n in notes), notes)

    def test_v4_rebalance_same_tx_has_no_pull_flag(self):
        tx = "0x" + "f" * 64
        trader = "0x9999999999999999999999999999999999999999"
        receipts = {tx: {"logs": [{"address": self.POOL_MANAGER, "topics": [self.SWAP, self.POOL_ID]}]}}
        mods = [self.mod("0xadd", self.LOCKER, 1, 2, 5000, 1),
                self.mod("0xreb", self.LOCKER, 1, 2, -5000, 1),
                self.mod("0xreb", self.LOCKER, 1, 2, 5000, 1)]
        txs = {"0xadd": {"from": self.CREATOR}, "0xreb": {"from": trader}}
        flags, _ = cc.lp_control_v4(self.fake_rpc(token_logs=[self.transfer(tx)], manager_logs=mods, receipts=receipts, tx_from=txs),
                                    self.TOKEN, {self.POOL_ID}, self.CREATOR, 0, 20, 100)
        self.assertFalse(any("Liquidity pulled" in x for x in flags), flags)

    def test_v4_manager_not_found_adds_note(self):
        flags, notes = cc.lp_control_v4(self.fake_rpc(token_logs=[self.transfer("0x" + "d" * 64)], manager_logs=[]),
                                        self.TOKEN, {self.POOL_ID}, self.CREATOR, 0, 20, 100)
        self.assertEqual(flags, [])
        self.assertTrue(any("v4 pools couldn't be checked" in n.lower() for n in notes), notes)

    def test_i256_negative_decoding(self):
        self.assertEqual(cc.i256("0x" + "f" * 64), -1)
        self.assertEqual(cc.i256("0x" + "0" * 63 + "a"), 10)


class KeyHelp(unittest.TestCase):
    def test_failure_names_the_free_keys_and_where(self):
        saved = cc.run

        def throttled(argv):
            raise cc.urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

        cc.run = throttled
        try:
            with self.assertRaises(SystemExit) as e:
                cc.main(["0x" + "ab" * 20])
        finally:
            cc.run = saved
        self.assertIn("allnodes.com/publicnode", str(e.exception))
        self.assertIn("helius.dev", str(e.exception))


class BadInput(unittest.TestCase):
    def exits(self, argv):
        saved = cc.check
        cc.check = lambda *a: self.fail("check() must not run on bad input")
        try:
            with self.assertRaises(SystemExit) as e:
                cc.main(argv)
        finally:
            cc.check = saved
        return str(e.exception)

    def test_free_text_is_refused_before_any_request(self):
        self.assertIn("isn't a token address", self.exits(["check this coin"]))
        self.assertIn("isn't a token address", self.exits(["0x1234; rm -rf ~"]))

    def test_chain_flag_needs_a_known_value(self):
        self.assertIn("--chain needs a value", self.exits(["0x" + "ab" * 20, "--chain"]))
        self.assertIn("Unknown chain 'polygon'", self.exits(["0x" + "ab" * 20, "--chain", "polygon"]))

    def test_address_and_chain_must_agree(self):
        self.assertIn("EVM address", self.exits(["0x" + "ab" * 20, "--chain", "solana"]))
        self.assertIn("looks like a Solana address", self.exits(["DDmGnuvroxVrb7CSyjYcezFU8gnAfhKasg4ZZezmpump", "--chain", "base"]))

    def test_unexpected_data_is_one_line_not_a_traceback(self):
        saved = cc.run

        def broken(argv):
            raise TypeError("'NoneType' object is not subscriptable")

        cc.run = broken
        try:
            with self.assertRaises(SystemExit) as e:
                cc.main(["0x" + "ab" * 20])
            with self.assertRaises(TypeError):
                cc.main(["0x" + "ab" * 20, "--debug"])
        finally:
            cc.run = saved
        self.assertIn("Nothing was found either way", str(e.exception))

    def test_rejected_key_is_not_called_rate_limiting_and_is_not_echoed(self):
        saved = cc.run

        def rejected(argv):
            raise cc.urllib.error.HTTPError("https://mainnet.helius-rpc.com/?api-key=SECRETKEY", 401, "Unauthorized", {}, None)

        cc.run = rejected
        try:
            with self.assertRaises(SystemExit) as e:
                cc.main(["DDmGnuvroxVrb7CSyjYcezFU8gnAfhKasg4ZZezmpump"])
        finally:
            cc.run = saved
        self.assertIn("rejected the request (HTTP 401)", str(e.exception))
        self.assertNotIn("SECRETKEY", str(e.exception))
        self.assertNotIn("rate limiting", str(e.exception))


class NetworkRetries(unittest.TestCase):
    def test_errors_while_reading_the_body_are_retried(self):
        import socket

        class Reply:
            def __init__(self, err):
                self.err = err

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                if self.err:
                    raise self.err
                return b'{"ok": 1}'

        queue = [socket.timeout("timed out"), ConnectionResetError(104, "reset"), cc.http.client.IncompleteRead(b""), None]
        saved, saved_sleep = cc.urllib.request.urlopen, cc.time.sleep
        cc.urllib.request.urlopen = lambda *a, **k: Reply(queue.pop(0))
        cc.time.sleep = lambda s: None
        try:
            self.assertEqual(cc.http_json("https://example.invalid/"), {"ok": 1})
        finally:
            cc.urllib.request.urlopen, cc.time.sleep = saved, saved_sleep
        self.assertEqual(queue, [])


class MarketData(unittest.TestCase):
    TOKEN = "0x" + "ab" * 20
    V4 = "0x" + "cd" * 32

    def market(self, pairs):
        saved = cc.http_json
        cc.http_json = lambda url, body=None, tries=7: {"pairs": pairs}
        try:
            return cc.market(self.TOKEN, "base")[1]
        finally:
            cc.http_json = saved

    def pair(self, address, liquidity):
        p = {"chainId": "base", "pairAddress": address, "baseToken": {"address": self.TOKEN, "symbol": "T"},
             "priceUsd": "1", "fdv": 1000, "pairCreatedAt": 1794375412000}
        if liquidity is not None:
            p["liquidity"] = {"usd": liquidity}
        return p

    def test_missing_liquidity_figure_is_unknown_not_zero(self):
        m = self.market([self.pair(self.V4, None)])
        self.assertIsNone(m["liquidity"])
        self.assertNotIn(self.V4, m["pair_liquidity"])
        self.assertIn("liquidity unknown", cc.fmt({"symbol": "T", "chain": "base", "address": self.TOKEN, "market": m,
                                                  "supply": {"total": 1.0, "burned": 0.0}, "flags": [], "holders": {},
                                                  "notes": [], "cant_check": []}))

    def test_known_liquidity_is_summed(self):
        m = self.market([self.pair(self.V4, 500.0), self.pair("0x" + "11" * 20, 2500.0)])
        self.assertEqual(m["liquidity"], 3000.0)
        self.assertEqual(m["first_pair_ts"], 1794375412)

    def test_solana_pool_addresses_keep_their_case(self):
        mint, pool = "DDmGnuvroxVrb7CSyjYcezFU8gnAfhKasg4ZZezmpump", "CJyaPwhfcRHL57Nrmr4yTV2egpWoGx9ZeKQjScDfi3Hd"
        saved = cc.http_json
        cc.http_json = lambda url, body=None, tries=7: {"pairs": [{
            "chainId": "solana", "pairAddress": pool, "baseToken": {"address": mint, "symbol": "T"}, "priceUsd": "1"}]}
        try:
            m = cc.market(mint, "solana")[1]
        finally:
            cc.http_json = saved
        self.assertEqual(m["pairs_any"], [pool])


class ReportWording(unittest.TestCase):
    def fdv_flag(self, dex, real):
        r = {"supply": {"real_fdv": real}, "market": {"dex_fdv": dex}, "holders": {}}
        return [f for f in cc.flags(r) if "FDV" in f]

    def test_fdv_gap_is_explained_in_the_right_direction(self):
        self.assertIn("burned", self.fdv_flag(17_500_000, 4_400_000)[0])
        low = self.fdv_flag(4_400_000, 17_500_000)[0]
        self.assertNotIn("burned", low)
        self.assertIn("smaller supply", low)


if __name__ == "__main__":
    unittest.main()
