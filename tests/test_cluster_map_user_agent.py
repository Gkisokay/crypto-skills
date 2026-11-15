import tempfile
import unittest

from test_cluster_map import CM, addr


class UserAgentTests(unittest.TestCase):
    def test_requests_name_the_tool_not_curl(self):
        seen = []

        def fetch(url, data, headers, timeout):
            seen.append(headers["User-Agent"])
            return {"jsonrpc": "2.0", "id": 1, "result": "0x1", "status": "1", "message": "OK"}

        with tempfile.TemporaryDirectory() as tmp:
            cache, stats = CM.DiskCache(tmp), CM.RequestStats()
            CM.RpcClient("https://x", cache, stats, fetch_json=fetch).call("eth_getCode", [addr(1), "0x10"])
            CM.BlockscoutClient("fake-key", cache, stats, fetch_json=fetch, limiter=CM.RateLimiter(1000)).get_history(addr(1), "txlist")
        self.assertEqual(len(seen), 2)
        for agent in seen:
            self.assertTrue(agent.startswith("cluster-map/"), agent)


if __name__ == "__main__":
    unittest.main()
