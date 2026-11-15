import json
import os
import tempfile
import time
import unittest

from test_cluster_map import CM, FakeBlockscoutClient, FakeRpcClient, addr, holder, make_histories, run_map, token_info, token_row


def age_cache(root, seconds):
    t = time.time() - seconds
    for folder, _, files in os.walk(root):
        for name in files:
            os.utime(os.path.join(folder, name), (t, t))


class CountingRpcFetch:
    def __init__(self):
        self.calls = 0

    def __call__(self, url, data, headers, timeout):
        self.calls += 1
        reqs = json.loads(data)
        if isinstance(reqs, list):
            return [{"jsonrpc": "2.0", "id": r["id"], "result": "0x%x" % self.calls} for r in reqs]
        return {"jsonrpc": "2.0", "id": reqs["id"], "result": "0x%x" % self.calls}


class BlockscoutExpiryTests(unittest.TestCase):
    def client(self, tmp, answers):
        calls = []

        def fake_fetch(url, data, headers, timeout):
            calls.append(url)
            return answers[min(len(calls) - 1, len(answers) - 1)]

        client = CM.BlockscoutClient("fake-key", CM.DiskCache(tmp), CM.RequestStats(), fetch_json=fake_fetch,
                                     limiter=CM.RateLimiter(1000))
        return client, calls

    def test_holders_are_reread_after_an_hour(self):
        a = {"status": "1", "message": "OK", "result": [holder(addr(1), 10)]}
        b = {"status": "1", "message": "OK", "result": [holder(addr(2), 20)]}
        with tempfile.TemporaryDirectory() as tmp:
            client, calls = self.client(tmp, [a, b])
            self.assertEqual(client.get_token_holders(addr(9), 5), a["result"])
            self.assertEqual(client.get_token_holders(addr(9), 5), a["result"])
            self.assertEqual(len(calls), 1)
            age_cache(tmp, 2 * 3600)
            self.assertEqual(client.get_token_holders(addr(9), 5), b["result"])
            self.assertEqual(len(calls), 2)

    def test_internal_transfers_of_one_tx_never_expire(self):
        row = {"from": addr(1), "to": addr(2), "value": "5", "isError": "0"}
        with tempfile.TemporaryDirectory() as tmp:
            client, calls = self.client(tmp, [{"status": "1", "message": "OK", "result": [row]}])
            client.tx_internal_native("0x" + "ab" * 32)
            age_cache(tmp, 30 * 86400)
            self.assertEqual(len(client.tx_internal_native("0x" + "ab" * 32)), 1)
            self.assertEqual(len(calls), 1)


class RpcExpiryTests(unittest.TestCase):
    def client(self, tmp, fetch):
        return CM.RpcClient("https://x", CM.DiskCache(tmp), CM.RequestStats(), fetch_json=fetch, batch_size=5, max_batch=5)

    def test_latest_nonce_and_code_are_reread_after_an_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetch = CountingRpcFetch()
            rpc = self.client(tmp, fetch)
            self.assertEqual(rpc.get_nonce_many([addr(1)]), [1])
            rpc.get_code_many([addr(1)])
            rpc.get_nonce_many([addr(1)])
            self.assertEqual(fetch.calls, 2)
            age_cache(tmp, 2 * 3600)
            self.assertEqual(rpc.get_nonce_many([addr(1)]), [3])
            self.assertEqual(rpc.get_code_many([addr(1)]), ["0x4"])
            self.assertEqual(fetch.calls, 4)

    def test_answers_at_a_fixed_block_never_expire(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetch = CountingRpcFetch()
            rpc = self.client(tmp, fetch)
            trace = [{"fromBlock": "0x10", "toBlock": "0x10", "toAddress": [addr(1)]}]
            rpc.call("eth_getCode", [addr(1), "0x10"])
            rpc.call("trace_filter", trace)
            age_cache(tmp, 30 * 86400)
            self.assertEqual(rpc.call("eth_getCode", [addr(1), "0x10"]), "0x1")
            self.assertEqual(rpc.call("trace_filter", trace), "0x2")
            self.assertEqual(fetch.calls, 2)

    def test_non_evm_methods_are_reread_after_an_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetch = CountingRpcFetch()
            rpc = self.client(tmp, fetch)
            rpc.call("getAsset", {"id": "SomeMint"})
            age_cache(tmp, 2 * 3600)
            self.assertEqual(rpc.call("getAsset", {"id": "SomeMint"}), "0x2")
            self.assertEqual(fetch.calls, 2)


class SolanaExpiryTests(unittest.TestCase):
    def test_solana_answers_are_reread_after_an_hour(self):
        class StubRpc:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            stub = StubRpc()
            stub.cache = CM.DiskCache(tmp)
            source = CM.SolanaSource(stub)
            runs = []

            def fetch():
                runs.append(1)
                return {"n": len(runs)}

            self.assertEqual(source._fetch(["holders", "Mint"], fetch), {"n": 1})
            self.assertEqual(source._fetch(["holders", "Mint"], fetch), {"n": 1})
            age_cache(tmp, 2 * 3600)
            self.assertEqual(source._fetch(["holders", "Mint"], fetch), {"n": 2})
            self.assertEqual(len(runs), 2)


class OldestAgeTests(unittest.TestCase):
    def test_oldest_age_counts_only_expiring_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CM.DiskCache(tmp)
            self.assertIsNone(cache.oldest_age_s)
            cache.set("ns", ["fixed"], 1)
            cache.set("ns", ["moving"], 2)
            age_cache(tmp, 1800)
            self.assertEqual(cache.get("ns", ["fixed"]), 1)
            self.assertIsNone(cache.oldest_age_s)
            self.assertIsNone(cache.get("ns", ["missing"], max_age_s=3600))
            self.assertIsNone(cache.oldest_age_s)
            self.assertEqual(cache.get("ns", ["moving"], max_age_s=3600), 2)
            self.assertAlmostEqual(cache.oldest_age_s, 1800, delta=60)


class CacheAgeLineTests(unittest.TestCase):
    def reports(self):
        p1, p2, p3 = addr(20), addr(21), addr(22)
        persons, holders = [p1, p2, p3], [holder(p1, 100), holder(p2, 100), holder(p3, 100)]
        collector, tok = addr(953), addr(954)
        events = [("tokentx", token_row(p, collector, 1000 + i, 60 + i, tok)) for i, p in enumerate(persons)]
        clustered, _, _ = run_map(FakeBlockscoutClient(token_info(), holders, make_histories(persons, events)),
                                     FakeRpcClient(nonces={collector: 5}))
        empty, _, _ = run_map(FakeBlockscoutClient(token_info(), holders, make_histories(persons, [])), FakeRpcClient())
        self.assertTrue(clustered["clusters"])
        self.assertFalse(empty["clusters"])
        return [clustered, empty]

    def test_summary_says_cache_age(self):
        for report in self.reports():
            report["header"]["oldest_cache_age_s"] = 1800
            self.assertIn("cache: oldest cached answer used was 30 min old (holders, balances and histories are re-read after 60 min)",
                          CM.build_summary(report))

    def test_no_cache_line_without_an_age(self):
        for report in self.reports():
            report["header"]["oldest_cache_age_s"] = None
            self.assertNotIn("cache:", CM.build_summary(report))
            del report["header"]["oldest_cache_age_s"]
            self.assertNotIn("cache:", CM.build_summary(report))


if __name__ == "__main__":
    unittest.main()
