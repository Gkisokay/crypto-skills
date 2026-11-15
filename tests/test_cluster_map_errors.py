import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from test_cluster_map import CM, FakeBlockscoutClient, FakeRpcClient, addr, holder, make_histories, run_map, token_info, token_row


class CliErrorTests(unittest.TestCase):
    def test_runtime_error_becomes_one_line_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(CM, "run_cluster_map", side_effect=RuntimeError("Blockscout request failed: HTTP 429")):
                with self.assertRaises(SystemExit) as ctx:
                    CM.main([addr(5000), "--out", tmp, "--cache", os.path.join(tmp, "cache")])
        self.assertEqual(str(ctx.exception), "cluster-map: Blockscout request failed: HTTP 429")

    def run_record(self, path, out, save_error=None):
        report = {"token": {"address": addr(5000)}, "header": {"generated_at_utc": "2026-01-01T00:00:00Z"}}
        with mock.patch.object(CM, "run_cluster_map", return_value=(report, "SUMMARY", None)):
            with mock.patch("sys.stdout", new=io.StringIO()) as stdout:
                patch = mock.patch.object(CM, "save_fixture", side_effect=save_error) if save_error else contextlib.nullcontext()
                with patch:
                    with self.assertRaises(SystemExit) as ctx:
                        CM.main([addr(5000), "--out", out, "--cache", os.path.join(out, "cache"), "--record", path])
        return str(ctx.exception), stdout.getvalue()

    def test_record_into_missing_folder_is_one_line_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            message, stdout = self.run_record(os.path.join(tmp, "nope", "f.json.gz"), tmp)
        self.assertTrue(message.startswith("cluster-map: could not save the fixture: "), message)
        self.assertIn("SUMMARY", stdout)

    def test_record_with_secret_is_one_line_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            message, _ = self.run_record(os.path.join(tmp, "f.json.gz"), tmp, RuntimeError("refusing to save a fixture that contains a key"))
        self.assertEqual(message, "cluster-map: could not save the fixture: refusing to save a fixture that contains a key")


class NonJsonBodyTests(unittest.TestCase):
    def test_html_body_becomes_runtime_error_with_host_only(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        body = b"<html><body>Bad gateway apikey=SECRETKEY123</body></html>"
        with mock.patch.object(CM.urllib.request, "urlopen", return_value=Resp(body)):
            with self.assertRaises(RuntimeError) as ctx:
                CM.default_fetch_json("https://api.example.com/path?apikey=SECRETKEY123")
        message = str(ctx.exception)
        self.assertIn("api.example.com", message)
        self.assertIn("<html>", message)
        self.assertNotIn("SECRETKEY123", message)
        self.assertNotIn("/path", message)


class RpcSanitizeTests(unittest.TestCase):
    def make_client(self, tmp, fetch_json, batch_size=1):
        return CM.RpcClient("https://x/?api-key=SECRETKEY123", CM.DiskCache(tmp), CM.RequestStats(),
                            fetch_json=fetch_json, batch_size=batch_size, max_batch=batch_size, secret="SECRETKEY123")

    def assert_redacted(self, exc):
        self.assertNotIn("SECRETKEY123", str(exc))
        self.assertIn("[redacted]", str(exc))

    def test_http_error_body_is_redacted(self):
        def fetch(url, data, headers, timeout):
            raise CM.urllib.error.HTTPError(url, 401, "x", {}, io.BytesIO(b"bad token SECRETKEY123 for https://x/?api-key=SECRETKEY123"))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                self.make_client(tmp, fetch).call("eth_blockNumber", [])
        self.assert_redacted(ctx.exception)

    def test_jsonrpc_error_is_redacted(self):
        def fetch(url, data, headers, timeout):
            return {"error": {"message": "invalid key SECRETKEY123"}}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                self.make_client(tmp, fetch).call("eth_blockNumber", [])
        self.assert_redacted(ctx.exception)

    def test_batch_row_error_is_redacted(self):
        def fetch(url, data, headers, timeout):
            return [{"id": 0, "error": {"message": "invalid key SECRETKEY123"}}]

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                self.make_client(tmp, fetch, batch_size=10).get_code_many([addr(1)])
        self.assert_redacted(ctx.exception)

    def test_helius_api_key_param_is_redacted(self):
        self.assertEqual(CM.sanitize_error_text("https://h/?api-key=abc123", ""), "https://h/?api-key=[redacted]")


class FailingHistoryClient(FakeBlockscoutClient):
    def __init__(self, *args, failing, **kwargs):
        super().__init__(*args, **kwargs)
        self.failing = failing

    def get_history(self, address, action):
        if address == self.failing:
            raise RuntimeError("Blockscout request failed with HTTP 500: upstream")
        return super().get_history(address, action)


class HistoryFailureTests(unittest.TestCase):
    def test_one_failed_holder_does_not_abort_run(self):
        p1, p2, p3 = addr(40), addr(41), addr(42)
        holders = [holder(p1, 100), holder(p2, 100), holder(p3, 100)]
        client = FailingHistoryClient(token_info(), holders, make_histories([p1, p2, p3], []), failing=p2)
        report, _, _ = run_map(client, FakeRpcClient())
        self.assertEqual(report["header"]["truncated_histories"], [p2])
        self.assertIn(f"{p2}: history not read (Blockscout request failed with HTTP 500: upstream).", report["notes"])
        self.assertNotIn(f"{p2}: history truncated at {CM.MAX_HISTORY_PAGES} pages of {CM.HISTORY_OFFSET} rows.",
                         report["notes"])

    def test_failed_holder_token_sends_are_not_called_spoofs(self):
        a, b = addr(60), addr(61)
        holders = [holder(a, 500), holder(b, 500)]
        histories = make_histories([a, b], [("tokentx", token_row(a, b, 1000, 120, token_address=addr(984)))])
        report, _, _ = run_map(FailingHistoryClient(token_info(), holders, histories, failing=a), FakeRpcClient())
        self.assertFalse([n for n in report["notes"] if "not counted as link" in n])
        self.assertTrue(report["direct_links"])


if __name__ == "__main__":
    unittest.main()
