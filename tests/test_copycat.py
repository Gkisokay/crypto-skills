import json
import sys
import urllib.error
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import copycat  # noqa: E402


class Copycat(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures" / "copycat_search_ai.json"

    @classmethod
    def setUpClass(cls):
        cls.search = json.loads(cls.FIX.read_text())

    def test_affair_inu_ai_is_flagged_against_older_bigger_ai(self):
        market = {"symbol": "AI", "dex_fdv": 185_000, "first_pair_ts": 1762444800}
        flag = copycat.find_copycat_from_search(
            "2T56GBrvcyeWWyhUAzsTFW4AcFag1nr7xyzrtZn7pump",
            "solana",
            market,
            self.search,
        )
        self.assertIsNotNone(flag)
        self.assertIn("Same ticker as AI on Robinhood Chain", flag)
        self.assertIn("0x2e8c31162b855a2ffa90f6f8634643ad6f111e18", flag)
        self.assertIn("0xce8cfbd3776d8f6920757d3bb0d56a89bdf01e18", flag)

    def test_no_flag_when_current_fdv_is_already_larger(self):
        market = {"symbol": "AI", "dex_fdv": 90_000_000, "first_pair_ts": 1762444800}
        flag = copycat.find_copycat_from_search(
            "2T56GBrvcyeWWyhUAzsTFW4AcFag1nr7xyzrtZn7pump",
            "solana",
            market,
            self.search,
        )
        self.assertIsNone(flag)

    def test_missing_age_prints_age_unknown(self):
        market = {"symbol": "AI", "dex_fdv": 185_000}
        flag = copycat.find_copycat_from_search(
            "2T56GBrvcyeWWyhUAzsTFW4AcFag1nr7xyzrtZn7pump",
            "solana",
            market,
            self.search,
        )
        self.assertIsNotNone(flag)
        self.assertIn("age unknown", flag)
        self.assertNotIn("0 days older", flag)

    def test_copycat_http_errors_return_cant_check_with_error_class(self):
        market = {"symbol": "AI"}
        flag, cant = copycat.find_copycat_flag(
            "2T56GBrvcyeWWyhUAzsTFW4AcFag1nr7xyzrtZn7pump",
            "solana",
            market,
            lambda url: (_ for _ in ()).throw(urllib.error.URLError("down")),
        )
        self.assertIsNone(flag)
        self.assertIn("URLError", cant)

    def test_copycat_unexpected_error_type_is_not_swallowed(self):
        market = {"symbol": "AI"}
        with self.assertRaises(ValueError):
            copycat.find_copycat_flag(
                "2T56GBrvcyeWWyhUAzsTFW4AcFag1nr7xyzrtZn7pump",
                "solana",
                market,
                lambda url: (_ for _ in ()).throw(ValueError("bad")),
            )


if __name__ == "__main__":
    unittest.main()
