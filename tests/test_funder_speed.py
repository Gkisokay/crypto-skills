import io
import sys
import urllib.error
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import launch_check as cc  # noqa: E402


class RpcPrunedState(unittest.TestCase):
    def setUp(self):
        self._http = cc.http_json
        self._sleep = cc.time.sleep
        self._open = cc.urllib.request.urlopen
        self._mono = cc.MONO

    def tearDown(self):
        cc.http_json = self._http
        cc.time.sleep = self._sleep
        cc.urllib.request.urlopen = self._open
        cc.MONO = self._mono

    def test_pruned_state_fast_fail_falls_back_and_sets_cache(self):
        calls = {"balance_batches": 0, "block_batches": 0}
        sleeps = []
        wallets = ["0x" + hex(i)[2:].rjust(40, "0") for i in range(1, 5)]

        def pruned_then_blocks(url, body=None, tries=7, deadline=None):
            if not isinstance(body, list):
                raise AssertionError(body)
            method = body[0]["method"] if body else None
            if method == "eth_getBalance":
                calls["balance_batches"] += 1
                return [{"jsonrpc": "2.0", "id": item["id"],
                         "error": {"code": -32000, "message": "historical state 0xabc is not available"}}
                        for item in body]
            if method == "eth_getBlockByNumber":
                calls["block_batches"] += 1
                return [{"jsonrpc": "2.0", "id": item["id"], "result": {"transactions": []}} for item in body]
            raise AssertionError(method)

        cc.http_json = pruned_then_blocks
        cc.time.sleep = lambda s: sleeps.append(s)
        rpc = cc.Rpc("u", 1000, 1, batch_size=20)
        r1 = {"launch": {}, "notes": []}
        s1, u1 = cc.first_funders(rpc, wallets, 50_000_000, 50_000_120, 600.0, r1)
        self.assertEqual((s1, u1), ([], 4))
        self.assertTrue(getattr(rpc, "_funding_balance_pruned", False))
        self.assertEqual(calls["balance_batches"], 1)
        self.assertEqual(calls["block_batches"], 1)
        self.assertEqual(sleeps, [])
        self.assertIn("block scan of the 7 blocks", r1["launch"]["funding_method"])
        self.assertEqual(r1["launch"]["funding_timing"]["calls"], 2)

        r2 = {"launch": {}, "notes": []}
        s2, u2 = cc.first_funders(rpc, wallets, 50_000_000, 50_000_120, 600.0, r2)
        self.assertEqual((s2, u2), ([], 4))
        self.assertEqual(calls["balance_batches"], 1)  # cached skip of balance search
        self.assertEqual(calls["block_batches"], 2)
        self.assertEqual(r2["launch"]["funding_timing"]["calls"], 1)

    def test_rpc_retries_429_then_success(self):
        attempts = []

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return b'{"jsonrpc":"2.0","id":1,"result":"0x2a"}'

        def open_then_ok(req, timeout=None):
            attempts.append(timeout)
            if len(attempts) == 1:
                raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
            return Reply()

        cc.urllib.request.urlopen = open_then_ok
        cc.time.sleep = lambda s: None
        got = cc.Rpc("https://rpc.example", 1000, 1).call("eth_blockNumber", [])
        self.assertEqual(got, "0x2a")
        self.assertEqual(len(attempts), 2)

    def test_deadline_timeout_becomes_note_not_crash(self):
        seq = [0.0, 59.0, 61.0]
        cc.MONO = lambda: seq.pop(0) if seq else 61.0
        touched = {"urlopen": 0}

        def should_not_open(*a, **kw):
            touched["urlopen"] += 1
            raise AssertionError("urlopen should not run after deadline expiry")

        cc.urllib.request.urlopen = should_not_open
        rpc = cc.Rpc("u", 1000, 1, batch_size=20)
        r = {"launch": {}, "notes": []}
        sends, unresolved = cc.first_funders(rpc, ["0x" + "1" * 40], 1_000_000, 1_000_100, 2.0, r)
        self.assertEqual((sends, unresolved), ([], 1))
        self.assertTrue(any("Funding lookups stopped after 60 s" in n for n in r["notes"]), r["notes"])
        self.assertEqual(touched["urlopen"], 0)


if __name__ == "__main__":
    unittest.main()
