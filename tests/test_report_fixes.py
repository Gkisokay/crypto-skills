import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import launch_check as cc  # noqa: E402


class ReportFixes(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures" / "report_fix_cases.json"

    def load(self):
        return json.loads(self.FIX.read_text())

    def test_human_span_rejects_negative_and_absurd_values(self):
        d = self.load()["span_seconds"]
        self.assertEqual(cc.human_span(d["negative"]), "time unknown")
        self.assertEqual(cc.human_span(d["absurd_large"]), "time unknown")
        self.assertEqual(cc.human_span(d["valid_short"]), "90 s")
        self.assertEqual(cc.human_span(d["valid_long"]), "65 min")

    def test_fmt_includes_main_risk_line_and_unknown_launch_gap(self):
        d = self.load()
        launch = dict(d["launch_row"])
        launch["first_buy_ts"] = launch["ts"] - 20
        r = {
            "symbol": "T",
            "chain": "base",
            "address": "0x" + "ab" * 20,
            "market": None,
            "supply": {"total": 1000.0, "burned": 0.0, "real_fdv": None},
            "flags": d["flags"],
            "flag_codes": [{"code": "LP_CAN_PULL", "text": d["flags"][0]}, {"code": "TOP10_PEOPLE", "text": d["flags"][1]}],
            "holders": {},
            "notes": [],
            "cant_check": [],
            "launch": launch,
            "creator_share": 0.10,
        }
        out = cc.fmt(r).splitlines()
        self.assertTrue(out[1].startswith("Main risk: "), out[:3])
        self.assertTrue(any("time unknown after the mint" in line for line in out), out)

    def test_removal_time_uses_time_unknown_for_invalid_gap(self):
        class FakeRpc:
            def block_ts(self, n):
                return 2000 if n == 20 else 5000

        last = {"blockNumber": "0x14"}
        got = cc.removal_time(FakeRpc(), last, 10, 1000)
        self.assertEqual(got, "time unknown after liquidity was first added to that pool")

    def test_finish_adds_flag_codes_without_changing_text(self):
        d = self.load()
        saved = cc.flags
        cc.flags = lambda r: list(d["flags"])

        class FakeRpc:
            def batch(self, calls):
                return []

        try:
            r = {
                "address": "0x" + "ab" * 20,
                "chain": "base",
                "market": {"pairs": [], "pairs_any": [], "pair_liquidity": {}},
                "notes": [],
                "supply": {"total": 1000.0, "burned": 0.0, "real_fdv": None},
            }
            out = cc.finish(r, FakeRpc(), r["address"], 1, 1000, 0, None, 200, logs=[])
        finally:
            cc.flags = saved
        self.assertEqual(out["flags"], d["flags"])
        self.assertEqual([x["code"] for x in out["flag_codes"]], ["LP_CAN_PULL", "TOP10_PEOPLE"])
        self.assertEqual([x["text"] for x in out["flag_codes"]], d["flags"])


if __name__ == "__main__":
    unittest.main()
