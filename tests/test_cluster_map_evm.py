"""Step 2-3 tests: real-chain fixtures (SEER on Robinhood, MONTRA on Base) replayed offline, plus the new link rules."""

import gzip
import importlib.util
import json
import os
import tempfile
import unittest


def load_cluster_map_module():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    path = os.path.join(root, "cluster-map", "cluster_map.py")
    spec = importlib.util.spec_from_file_location("cluster_map_evm_mod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CM = load_cluster_map_module()
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

SEER = "0x9b0e4762b4cc7c05331b561032f1cb4c37607543"
SEER_BUNDLE = {
    "0x40d9653c885107d7dcc1515642edd22f109b654a",
    "0xb0829ec385bb8f83dbae98283de23d6630198adc",
    "0x7b9b006eef6647a9123c74ab854c117acfc9fd09",
    "0x8bf3a047ab64781257c14d0655687ae70cb36e91",
    "0xa1b89aaaf3edda13ae66b1db354167335405cf65",
    "0x5dac3d51b1fd1c068c6bfb7219dcdb944c55c625",
    "0x9e8c5c29b473ad2e5d85ef34f283bcaf58d8acfe",
    "0xc04045936e38a06c31ae1610debde124197344ff",
    "0x784fb74b2569c57799d35cb8aae6bc31f6935fc2",
    "0x52106ab882be393926311867ce2f24c3754fd5cf",
}

MONTRA = "0xeebaa9bcf270e227b52b6206bda794a74ebc43fe"
FUNDER_15ETH = "0x9bf4ef3459e2d376518ebb36bb992c0b38fc2c11"
MONTRA_CREATOR = "0xf5553fa8eb2991e9f003056e536eec629e292b24"
MONTRA_FUNDING_TX = "0xca09f942bf91bc04f5eda5bf28e9e6812091eacb6a2abdd8f2eb51fa1b396a1d"
MONTRA_PROCEEDS_TX = "0x36db11fd0060b81d30a0e7c765c3ecefc749ecb85394c37dbd7b452d1ba7417d"
MONTRA_MULTISEND_TX = "0xa258be5c8af92886d1c1c127d4b13c7826c31e0f622614cf463a27a714902983"
MONTRA_BOT_BUYERS = {
    "0x78ced5485303fe5ed7100b04425202b427d53452",
    "0x90f6fe80c4d04211b5c56eaf88703996480fe530",
    "0x6c200f31cdc5bea9e439f31877c321ae9ce6c81d",
    "0x8cbc66a0996e43c65acf11157113d166ff941f44",
    "0x33866ad12bd0dec673ee31ee1727c6e8e8327ff7",
    "0x9b8c6acde15a5c598b173f41e76cfbd0bc99d52c",
    "0x4d7f5dccbc20a9c8c1a2b956941e154c63a79839",
    "0xdbdfad89b743c71a4002d1c839183a930e1a0c24",
    "0x8b81e2245ef02089a45c8afecc4fdc76745f7c77",
    "0xf51f2f0d4a06e447f6e1ff6009b5f0819606df79",
}


SHORT_ADDR = r"[0-9A-Za-z]{4,}(…|\.\.\.)[0-9A-Za-z]{4,}"
JARGON = ("follow-forward", "magic node", "converge", "start wallets")


def load_fixture(name):
    with gzip.open(os.path.join(FIXTURES, name), "rt", encoding="utf-8") as handle:
        return json.load(handle)


def addr(num):
    return "0x" + "%040x" % (0x2000000000 + num)


def txh(num):
    return "0x%064x" % num


class SeerFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = load_fixture("seer-robinhood.json.gz")
        with tempfile.TemporaryDirectory() as tmp:
            cache = CM.DiskCache(os.path.join(tmp, "cache"))
            stats = CM.RequestStats()
            rpc = CM.RpcClient(CM.CHAINS["robinhood"]["rpc_url"], cache, stats, fetch_json=CM.ReplayFetch(fixture["rpc"]))
            cls.report, cls.summary, _ = CM.run_cluster_map(
                SEER, None, fixture["meta"]["top"], os.path.join(tmp, "cache"),
                blockscout_client=CM.ReplaySource(fixture["source"]), rpc_client=rpc,
                generated_at="2026-11-14T00:00:00Z",
            )

    def test_bundle_cluster_says_why_in_one_sentence(self):
        why = self.report["clusters"][0]["why"]
        self.assertTrue(why.startswith("10 of these wallets bought"), why)
        self.assertIn("within 1 s", why)
        self.assertIn("near-equal", why)
        self.assertEqual(why.count(". "), 0)
        self.assertIn(why, self.summary)

    def test_sale_payouts_are_one_line_in_the_answer(self):
        after = self.report["clusters"][0]["after"]
        self.assertEqual(after.count("paid from their sells"), 1, after)
        self.assertRegex(after, r"ETH went to \d+ new wallets · 19\.7\d ETH \(paid from their sells\)")

    def test_bundle_is_one_cluster(self):
        top = self.report["clusters"][0]
        self.assertTrue(SEER_BUNDLE <= set(top["wallets"]))
        self.assertAlmostEqual(top["bought_at_launch"], 0.5004, places=3)
        others = [c for c in self.report["clusters"][1:] if set(c["wallets"]) & SEER_BUNDLE]
        self.assertEqual(others, [])

    def test_launch_burst_matches_known_blocks(self):
        bursts = [t for t in self.report["time_nodes"] if t["subtype"] == "launch-burst"]
        self.assertEqual(len(bursts), 1)
        self.assertEqual(set(bursts[0]["wallets"]), SEER_BUNDLE)
        self.assertEqual(bursts[0]["block_first"], 105_977_303)
        self.assertLessEqual(bursts[0]["block_last"] - bursts[0]["block_first"], 10)
        self.assertLessEqual(bursts[0]["window_end"] - bursts[0]["window_start"], 1)
        self.assertEqual(bursts[0]["funder_addresses"], ["0x8366a39cc670b4001a1121b8f6a443a643e40951"])

    def test_funding_wave_alone_also_finds_all_ten(self):
        waves = [t for t in self.report["time_nodes"] if t["subtype"] == "funding-wave"]
        self.assertEqual(len(waves), 1)
        self.assertEqual(set(waves[0]["wallets"]), SEER_BUNDLE)
        self.assertIn("0xb22204b8cb966ffcedcfdf50fdd5c83d144d679a", waves[0]["funder_addresses"])

    def test_bundle_wallets_paid_current_holders(self):
        pairs = {(d["a"], d["b"]) for d in self.report["direct_links"]}
        self.assertIn(("0x15e6343db946e2d1f0b56bc54414daddb32300e6", "0x7b9b006eef6647a9123c74ab854c117acfc9fd09"), pairs)
        self.assertIn(("0x8bf3a047ab64781257c14d0655687ae70cb36e91", "0xea1cbe0e0869868fd3ea3f8945b8c7c1d77e1f50"), pairs)


    def test_follow_forward_finds_sale_payouts_to_fresh_wallets(self):
        follow = self.report["follow_forward"]
        flows = [f for f in follow["flows"] if f["hops"]]
        self.assertGreaterEqual(len(flows), 90)
        self.assertTrue(all(f["hops"][0]["kind"] == "sale proceeds" for f in flows))
        self.assertEqual({f["hops"][0]["paid_by"] for f in flows}, {"0x8366a39cc670b4001a1121b8f6a443a643e40951"})
        self.assertTrue({f["start"] for f in flows} <= SEER_BUNDLE | {"0x15e6343db946e2d1f0b56bc54414daddb32300e6", "0xea1cbe0e0869868fd3ea3f8945b8c7c1d77e1f50"})
        self.assertAlmostEqual(sum(f["total_eth"] for f in flows), 19.73, delta=0.05)
        self.assertFalse(any(f["returns_home"] for f in flows))

    def test_summary_has_no_shortened_address(self):
        self.assertNotRegex(self.summary, SHORT_ADDR)

    def test_headline_says_bought_and_held_when_cluster_sold(self):
        self.assertIn("\n12 wallets that are probably one person or team bought 50.0% of the supply at launch and hold 1.3% now. "
                      "2 more groups hold 2.0%.\n", self.summary)

    def test_money_after_launch_in_plain_words(self):
        self.assertIn("\nFollowed 98 ETH payments out of 13 wallets; 0 went back to a wallet that funded them or is linked to them.\n",
                      self.summary)
        self.assertIn("  0.3100 ETH 0x7b9b006eef6647a9123c74ab854c117acfc9fd09 -> 0xc1f06f5ecd2ba5313d9dfcf466b3e5913402c40f "
                      "[sent no ETH on] tx 0x7b24fe03ed129cb18334e5b4fecd2a76157a2d75e86412d9ebda1289d88051c1", self.summary)
        for word in JARGON:
            self.assertNotIn(word, self.summary)

    def test_lists_every_cluster_wallet(self):
        for cluster in self.report["clusters"]:
            for wallet in cluster["wallets"]:
                self.assertIn(f"  {wallet} · holds", self.summary)

    def test_repeated_notes_fold_into_one_line(self):
        self.assertIn("- 18 wallets: txlistinternal included unprocessed-range warning and rows were used. (list: --out <folder>)",
                      self.summary)
        self.assertNotIn("0x15e6343db946e2d1f0b56bc54414daddb32300e6: txlistinternal", self.summary)
        self.assertIn("- 0x1affa14b4a972adfd3f6eebc14c51686ae3081c0: history truncated at 5 pages of 1000 rows.", self.summary)
        self.assertIn("0x15e6343db946e2d1f0b56bc54414daddb32300e6: txlistinternal included unprocessed-range warning and rows were used.",
                      self.report["notes"])

    def test_header_lines_read_plainly(self):
        self.assertTrue(self.summary.startswith("SEER · Robinhood (4663) · "))
        self.assertIn("generated 2026-11-14T00:00:00Z · holders 25 · people 30 · launch buyers 14 · 0 requests, 0 from cache · "
                      "2 histories cut short", self.summary)

    def test_buys_are_not_proceeds(self):
        router = "0x06afba43fd06227fa663b0daecf536f6eaa6bf99"
        for flow in self.report["follow_forward"]["flows"]:
            for hop in flow["hops"]:
                self.assertNotEqual(hop["to"], router)


class MontraFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = load_fixture("montra-base.json.gz")
        old = os.environ.get("PUBLICNODE_TOKEN")
        os.environ["PUBLICNODE_TOKEN"] = "fixture-token"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cls.report, cls.summary, json_path = CM.run_cluster_map(
                    MONTRA, os.path.join(tmp, "out"), fixture["meta"]["top"], os.path.join(tmp, "cache"),
                    generated_at="2026-11-14T00:00:00Z", chain_key="base",
                    fetch_json=CM.ReplayFetch(fixture["rpc"], secrets=["fixture-token"]),
                )
                with open(json_path, encoding="utf-8") as handle:
                    cls.json_text = handle.read()
        finally:
            if old is None:
                os.environ.pop("PUBLICNODE_TOKEN", None)
            else:
                os.environ["PUBLICNODE_TOKEN"] = old

    def test_text_leads_with_plain_answer_then_clusters_then_how_read(self):
        answer = "11 wallets that are probably one person or team hold 98.9% of the supply."
        cluster = CM.tile_title(self.report["clusters"][0])
        a, card, how = (self.summary.index(x) for x in (answer, cluster, "How this was read"))
        self.assertLess(a, card)
        self.assertLess(card, how)
        self.assertLess(how, self.summary.index("read: native"))

    def test_text_names_every_cluster_wallet_in_full(self):
        for wallet in MONTRA_BOT_BUYERS | {MONTRA_CREATOR}:
            self.assertIn(wallet, self.summary)

    def funder_node(self):
        nodes = [m for m in self.report["magic_nodes"] if m["address"] == FUNDER_15ETH]
        self.assertEqual(len(nodes), 1)
        return nodes[0]

    def test_15eth_funder_is_magic_node(self):
        node = self.funder_node()
        self.assertEqual(node["role"], "both")
        self.assertEqual(set(node["funded"]), MONTRA_BOT_BUYERS | {MONTRA_CREATOR})
        self.assertEqual(node["loop_wallets"], [MONTRA_CREATOR])
        self.assertTrue(node["busy"])
        for tx in (MONTRA_FUNDING_TX, MONTRA_PROCEEDS_TX, MONTRA_MULTISEND_TX):
            self.assertIn(tx, node["hashes"])

    def test_cluster_is_creator_plus_bot_buyers(self):
        self.assertEqual(len(self.report["clusters"]), 1)
        self.assertEqual(set(self.report["clusters"][0]["wallets"]), MONTRA_BOT_BUYERS | {MONTRA_CREATOR})
        self.assertEqual(self.report["header"]["creator"], MONTRA_CREATOR)

    def test_follow_forward_proceeds_go_back_to_funder(self):
        flows = [f for f in self.report["follow_forward"]["flows"] if f["start"] == MONTRA_CREATOR and f["hops"]]
        self.assertEqual(len(flows), 1)
        hop = flows[0]["hops"][0]
        self.assertEqual(hop["to"], FUNDER_15ETH)
        self.assertEqual(hop["hash"], MONTRA_PROCEEDS_TX)
        self.assertAlmostEqual(hop["value_eth"], 17.7397, places=3)
        self.assertTrue(flows[0]["returns_home"])
        self.assertEqual(flows[0]["stop"], "back to the start's first funder")
        self.assertIn("back to the start's first funder", self.summary)

    def test_cluster_says_why(self):
        why = self.report["clusters"][0]["why"]
        self.assertTrue(why.startswith(f"All 11 wallets got ETH from the same wallet {FUNDER_15ETH}"), why)
        self.assertIn("which got ETH back from them", why)

    def test_proceeds_home_in_the_after_line(self):
        self.assertIn(f"17.74 ETH went back to {FUNDER_15ETH}, the wallet that funded them", self.report["clusters"][0]["after"])

    def test_summary_has_no_shortened_address(self):
        self.assertNotRegex(self.summary, SHORT_ADDR)
        self.assertIn(f"All 11 wallets got ETH from the same wallet {FUNDER_15ETH}", self.summary)
        self.assertIn(f"After launch: 17.74 ETH went back to {FUNDER_15ETH}, the wallet that funded them.", self.summary)

    def test_headline_unchanged_when_cluster_holds_more_than_it_bought(self):
        self.assertIn("\n11 wallets that are probably one person or team hold 98.9% of the supply.\n", self.summary)

    def test_money_after_launch_in_plain_words(self):
        self.assertIn("\nFollowed 1 ETH payment out of 11 wallets; 1 went back to a wallet that funded them or is linked to them.\n",
                      self.summary)
        for word in JARGON:
            self.assertNotIn(word, self.summary)

    def test_header_lines_read_plainly(self):
        self.assertTrue(self.summary.startswith("MONTRA · Base (8453) · "))
        self.assertRegex(self.summary, r"· people 12 · launch buyers 3 · \d+ requests, \d+ from cache · 0 histories cut short\n")

    def test_funder_not_duplicated_as_time_node(self):
        for node in self.report["time_nodes"]:
            self.assertNotIn(FUNDER_15ETH, node["funder_addresses"])

    def test_header_says_what_was_read(self):
        coverage = " ".join(self.report["header"]["coverage"])
        self.assertIn("Transfer logs", coverage)
        self.assertIn("trace_filter", coverage)
        self.assertIn("other tokens not read", coverage)
        self.assertIn("read: native", self.summary)
        self.assertEqual(self.report["header"]["chain_name"], "Base")

    def test_no_key_in_outputs(self):
        for text in (self.json_text, self.summary):
            self.assertNotIn("fixture-token", text)
            self.assertNotIn("publicnode.com/", text)


class FakeRpc:
    def __init__(self, codes=None, txs=None):
        self.codes = codes or {}
        self.txs = txs or {}

    def get_code_many(self, addresses):
        return [self.codes.get(a, "0x") for a in addresses]

    def get_tx_many(self, hashes):
        return [self.txs.get(h) for h in hashes]


def internal(frm, to, idx, value=10**18):
    return {"from": frm, "to": to, "value": str(value), "hash": txh(idx), "blockNumber": str(idx), "timeStamp": str(1000 + idx)}


class OriginResolutionTests(unittest.TestCase):
    def test_multisend_credited_to_sender(self):
        person, multisend, sender = addr(1), addr(2), addr(3)
        histories = {person: {"txlist": [], "txlistinternal": [internal(multisend, person, 5)], "tokentx": []}}
        rpc = FakeRpc(codes={multisend: "0x6080"}, txs={txh(5): {"from": sender}})
        self.assertEqual(CM.resolve_internal_origins(histories, [person], rpc), 1)
        self.assertEqual(histories[person]["txlistinternal"], [])
        self.assertEqual(histories[person]["txlist"][0]["from"], sender)
        self.assertEqual(histories[person]["txlist"][0]["via"], multisend)

    def test_swap_payout_to_self_left_alone(self):
        person, router = addr(1), addr(2)
        histories = {person: {"txlist": [], "txlistinternal": [internal(router, person, 6)], "tokentx": []}}
        rpc = FakeRpc(codes={router: "0x6080"}, txs={txh(6): {"from": person}})
        self.assertEqual(CM.resolve_internal_origins(histories, [person], rpc), 0)
        self.assertEqual(len(histories[person]["txlistinternal"]), 1)


def counterparty(sent_to=(), received_from=()):
    persons = set(sent_to) | set(received_from)
    return {
        "persons": persons,
        "sent_to_persons": set(sent_to),
        "received_from_persons": set(received_from),
        "native_sent_to_persons": set(sent_to),
        "native_received_from_persons": set(received_from),
        "hashes": [txh(1)],
        "first_timestamp": 1,
        "last_timestamp": 2,
    }


class BusyLoopRuleTests(unittest.TestCase):
    def label(self, nonce, contract=False):
        return {"nonce": nonce, "is_contract": contract, "is_busy": contract or nonce >= CM.BUSY_NONCE,
                "is_busy_eoa": (not contract) and nonce >= CM.BUSY_NONCE}

    def test_busy_funder_with_loop_is_magic(self):
        a, b, f = addr(10), addr(11), addr(12)
        nodes = CM.classify_magic_nodes({f: counterparty(sent_to=[a, b], received_from=[a])}, {f: self.label(2917)})
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["loop_wallets"], [a])

    def test_busy_funder_without_loop_hidden(self):
        a, b, f = addr(10), addr(11), addr(12)
        nodes = CM.classify_magic_nodes({f: counterparty(sent_to=[a, b])}, {f: self.label(2917)})
        self.assertEqual(nodes, [])

    def test_loop_paid_by_other_holder_is_not_a_loop(self):
        a, b, c, f = addr(10), addr(11), addr(13), addr(12)
        nodes = CM.classify_magic_nodes({f: counterparty(sent_to=[a, b], received_from=[c])}, {f: self.label(5000)})
        self.assertEqual(nodes, [])

    def test_supernode_hidden_even_with_loop(self):
        a, b, f = addr(10), addr(11), addr(12)
        nodes = CM.classify_magic_nodes({f: counterparty(sent_to=[a, b], received_from=[a])}, {f: self.label(CM.SUPERNODE_NONCE)})
        self.assertEqual(nodes, [])


def buy(person, ts, block, share, src=None):
    return person, {"person": person, "from": src or addr(99), "timestamp": ts, "block": block, "hash": txh(block), "share": share}


class LaunchBurstTests(unittest.TestCase):
    def test_equal_buys_within_window(self):
        buys = dict(buy(addr(i), 100 + (i // 5), 500 + i, 0.05) for i in range(6))
        nodes = CM.classify_launch_bursts(buys)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(len(nodes[0]["wallets"]), 6)

    def test_unequal_snipers_do_not_burst(self):
        shares = [0.05, 0.02, 0.011, 0.031, 0.008]
        buys = dict(buy(addr(i), 100, 500 + i, s) for i, s in enumerate(shares))
        self.assertEqual(CM.classify_launch_bursts(buys), [])

    def test_spread_out_buys_do_not_burst(self):
        buys = dict(buy(addr(i), 100 + i * 10, 500 + i, 0.05) for i in range(5))
        self.assertEqual(CM.classify_launch_bursts(buys), [])

    def test_dust_buys_ignored(self):
        buys = dict(buy(addr(i), 100, 500 + i, 0.001) for i in range(5))
        self.assertEqual(CM.classify_launch_bursts(buys), [])


class FundingWaveTests(unittest.TestCase):
    def test_one_off_funder_joins_wave(self):
        people = [addr(20 + i) for i in range(3)]
        busy1, busy2, quiet = addr(30), addr(31), addr(32)
        first = {
            people[0]: {"person": people[0], "from": busy1, "hash": txh(1), "block": 1, "timestamp": 1000},
            people[1]: {"person": people[1], "from": busy2, "hash": txh(2), "block": 2, "timestamp": 1060},
            people[2]: {"person": people[2], "from": quiet, "hash": txh(3), "block": 3, "timestamp": 1200},
        }
        labels = {
            busy1: {"is_busy_eoa": True, "is_contract": False},
            busy2: {"is_busy_eoa": True, "is_contract": False},
            quiet: {"is_busy_eoa": False, "is_contract": False},
        }
        waves = [n for n in CM.classify_time_nodes(first, labels) if n["subtype"] == "funding-wave"]
        self.assertEqual(len(waves), 1)
        self.assertEqual(set(waves[0]["wallets"]), set(people))

    def test_wave_after_launch_is_ignored(self):
        people = [addr(60 + i) for i in range(3)]
        funders = [addr(70 + i) for i in range(3)]
        first = {p: {"person": p, "from": f, "hash": txh(i), "block": i, "timestamp": 5000 + 30 * i}
                 for i, (p, f) in enumerate(zip(people, funders))}
        labels = {f: {"is_busy_eoa": False, "is_contract": False} for f in funders}
        self.assertEqual(len(CM.classify_time_nodes(first, labels, launch_ts=6000)), 1)
        self.assertEqual(CM.classify_time_nodes(first, labels, launch_ts=4000), [])

    def test_magic_funder_excluded_from_time_nodes(self):
        people = [addr(40 + i) for i in range(3)]
        busy = addr(50)
        first = {p: {"person": p, "from": busy, "hash": txh(i), "block": i, "timestamp": 1000 + i} for i, p in enumerate(people)}
        labels = {busy: {"is_busy_eoa": True, "is_contract": False}}
        self.assertEqual(CM.classify_time_nodes(first, labels, {busy}), [])


class BisectionFakeRpc:
    def __init__(self, change_at, batch_size=10):
        self.change_at = change_at
        self.batch_size = batch_size
        self.batches = 0

    def call_many(self, method, params_list):
        self.batches += 1
        return [hex(1 if int(p[1], 16) >= self.change_at else 0) for p in params_list]


class BisectionTests(unittest.TestCase):
    def test_finds_exact_block_in_few_batches(self):
        for change_at in (1, 2, 17, 55_925_083, 57_000_000):
            rpc = BisectionFakeRpc(change_at)
            src = CM.RpcChainSource.__new__(CM.RpcChainSource)
            src.rpc = rpc
            got = src.first_block_where("eth_getBalance", addr(1), lambda v: v > 0, -1, 57_145_000)
            self.assertEqual(got, change_at)
            self.assertLessEqual(rpc.batches, 9)


class PrunedFetch:
    """A load-balanced RPC where some backends answer 'state is pruned' for old blocks."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def __call__(self, url, data, headers, timeout):
        self.calls += 1
        reqs = json.loads(data)
        if self.calls <= self.failures:
            return [{"jsonrpc": "2.0", "id": r["id"], "error": {"code": -32603, "message": "state at block #47130319 is pruned"}} for r in reqs]
        return [{"jsonrpc": "2.0", "id": r["id"], "result": "0x1"} for r in reqs]


class PrunedStateTests(unittest.TestCase):
    def client(self, fetch, tmp):
        return CM.RpcClient("https://x", CM.DiskCache(tmp), CM.RequestStats(), fetch_json=fetch, batch_size=5, max_batch=5)

    def test_pruned_answer_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = CM.BACKOFF_BASE_S
            CM.BACKOFF_BASE_S = 0
            try:
                fetch = PrunedFetch(failures=1)
                self.assertEqual(self.client(fetch, tmp).call_many("eth_getBalance", [[addr(1), "0x10"]]), ["0x1"])
                self.assertEqual(fetch.calls, 2)
            finally:
                CM.BACKOFF_BASE_S = old

    def test_one_wallet_failing_does_not_stop_the_run(self):
        src = CM.RpcChainSource.__new__(CM.RpcChainSource)
        src.chain = {"trace": True, "link_tokens": {}, "log_range": 1000}
        src.coverage, src.head = [], 100
        good, bad = addr(1), addr(2)

        def native_history(address, head):
            if address == bad:
                raise CM.RpcStateError("RPC eth_getTransactionCount failed: state at block #5 is pruned")
            return address, {"txlist": [send(addr(9), good, 5)], "txlistinternal": [], "tokentx": []}, 1, []

        src._native_history = native_history
        src.token_links = lambda persons: ([], "token links: none")
        rows, truncated, notes = src.fetch_histories([good, bad])
        self.assertEqual(len(rows[good]["txlist"]), 1)
        self.assertEqual(truncated, [bad])
        self.assertTrue(any(bad in n and "pruned" in n for n in notes))


class WalkSendsTests(unittest.TestCase):
    def test_walk_reuses_earlier_probes(self):
        sends_at = [1_000, 2_000_000, 2_000_500, 30_000_000, 30_000_001]

        class NonceRpc:
            batch_size = 10
            batches = 0

            def call_many(self, method, params_list):
                NonceRpc.batches += 1
                return [hex(sum(1 for b in sends_at if int(p[1], 16) >= b)) for p in params_list]

        src = CM.RpcChainSource.__new__(CM.RpcChainSource)
        src.rpc = NonceRpc()
        seen_blocks = []
        src._trace_rows = lambda block, address, direction: (seen_blocks.append(block) or [], [], 1)
        src._walk_sends(addr(1), 1, -1, len(sends_at), 40_000_000)
        self.assertEqual(seen_blocks, sends_at)
        self.assertLessEqual(NonceRpc.batches, 30)


class FollowSource:
    def __init__(self, sends):
        self.sends = sends

    def outgoing_native(self, address, after_block):
        return [dict(r) for r in self.sends.get(address, []) if int(r["blockNumber"]) > after_block]


class FollowRpc:
    def __init__(self, codes=None, nonces=None):
        self.codes, self.nonces = codes or {}, nonces or {}

    def get_code_many(self, addresses):
        return [self.codes.get(a, "0x") for a in addresses]

    def get_nonce_many(self, addresses):
        return [self.nonces.get(a, 1) for a in addresses]


def send(frm, to, block, eth=1.0):
    return {"from": frm, "to": to, "value": str(int(eth * 10**18)), "hash": txh(block), "blockNumber": str(block), "timeStamp": str(block)}


class FollowForwardStopTests(unittest.TestCase):
    def test_stops(self):
        start, a, b, c, d, cex, router = (addr(100 + i) for i in range(7))
        sends = {
            start: [send(start, a, 10), send(start, router, 11), send(start, cex, 12), send(start, d, 5)],
            a: [send(a, b, 20)],
            b: [send(b, c, 30)],
        }
        result = CM.follow_forward([start], {}, {}, [], FollowSource(sends), FollowRpc(codes={router: "0x60"}, nonces={cex: 50_000}), 9)
        stops = {f["ends_at"]: f["stop"] for f in result["flows"]}
        self.assertEqual(stops[router], "contract")
        self.assertEqual(stops[cex], "busy wallet (50,000 txs)")
        self.assertEqual(stops[c], "hop limit")
        self.assertNotIn(d, stops)

    def test_tiny_sends_ignored(self):
        start, a = addr(200), addr(201)
        result = CM.follow_forward([start], {}, {}, [], FollowSource({start: [send(start, a, 10, eth=0.01)]}), FollowRpc(), 0)
        self.assertEqual([f for f in result["flows"] if f["hops"]], [])


class FixtureSafetyTests(unittest.TestCase):
    def test_save_refuses_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                CM.save_fixture(os.path.join(tmp, "x.json.gz"), {}, {"https://x/abc-secret\n{}": {"result": "abc-secret"}}, None, ["abc-secret"])

    def test_recorded_key_is_redacted(self):
        rec = CM.RecordingFetch(lambda url, data, headers, timeout: {"result": "0x1"}, ["sekret"])
        rec("https://base-rpc.publicnode.com/sekret", b'{"method":"eth_blockNumber"}')
        self.assertTrue(all("sekret" not in k for k in rec.dump()))

    def test_missing_publicnode_token_is_clear(self):
        old = os.environ.pop("PUBLICNODE_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit) as ctx:
                    CM.run_cluster_map(MONTRA, tmp, 10, os.path.join(tmp, "c"), chain_key="base")
            self.assertIn("PUBLICNODE_TOKEN", str(ctx.exception))
        finally:
            if old is not None:
                os.environ["PUBLICNODE_TOKEN"] = old


class MissingKeyTests(unittest.TestCase):
    def missing_key_message(self, chain, env):
        old = os.environ.pop(env, None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit) as ctx:
                    CM.build_clients(chain, CM.DiskCache(tmp), CM.RequestStats())
            return str(ctx.exception)
        finally:
            if old is not None:
                os.environ[env] = old

    def test_base_names_the_free_publicnode_token(self):
        self.assertIn("allnodes.com/publicnode", self.missing_key_message("base", "PUBLICNODE_TOKEN"))

    def test_robinhood_names_the_free_blockscout_key(self):
        self.assertIn("dev.blockscout.com", self.missing_key_message("robinhood", "BLOCKSCOUT_API_KEY"))


class PlainAnswerTests(unittest.TestCase):
    def test_no_clusters(self):
        self.assertEqual(CM.plain_answer([]), "No group of top holders looks linked in the data read.")

    def test_more_groups_are_counted(self):
        clusters = [{"wallet_count": 85, "share_now": 0.12}, {"wallet_count": 2, "share_now": 0.03}, {"wallet_count": 4, "share_now": 0.01}]
        self.assertEqual(
            CM.plain_answer(clusters),
            "85 wallets that are probably one person or team hold 12.0% of the supply. 2 more groups hold 4.0%.",
        )

    def test_sold_clusters_say_bought_and_held(self):
        clusters = [{"wallet_count": 94, "share_now": 0.013, "bought_at_launch": 0.083},
                    {"wallet_count": 94, "share_now": 0.014, "bought_at_launch": 0.055},
                    {"wallet_count": 2, "share_now": 0.0, "bought_at_launch": 0.0}]
        self.assertEqual(
            CM.plain_answer(clusters),
            "94 wallets that are probably one person or team bought 8.3% of the supply at launch and hold 1.3% now. "
            "2 more groups bought 5.5% at launch and hold 1.4% now.",
        )


class SummaryNotesTests(unittest.TestCase):
    def test_three_repeats_stay_as_they_are(self):
        notes = [f"0x{i}: same thing." for i in range(3)] + ["other note."]
        self.assertEqual(CM.summary_note_lines(notes), [f"- 0x{i}: same thing." for i in range(3)] + ["- other note."])

    def test_four_repeats_fold(self):
        notes = ["0xa: other."] + [f"0x{i}: same thing." for i in range(4)]
        self.assertEqual(CM.summary_note_lines(notes), ["- 0xa: other.", "- 4 wallets: same thing. (list: --out <folder>)"])


if __name__ == "__main__":
    unittest.main()
