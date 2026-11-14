import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import launch_check as cc  # noqa: E402
import snipers  # noqa: E402


class Snipers(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures"

    def load(self, name):
        return json.loads((self.FIX / name).read_text())

    def make_wallets(self, cfg):
        open_ts = cfg["open_ts"]
        count = cfg["wallet_count"]
        total = cfg["total_supply"]
        took = total * cfg["taken_share"]
        per = took / count
        wallets = []
        for i in range(count):
            addr = f"wallet-{i:03d}"
            wallets.append((addr, per, open_ts + int(i * cfg["duration_s"] / max(count - 1, 1))))
        return wallets

    def test_affair_shape_reads_as_mostly_snipers(self):
        cfg = self.load("snipers_affair.json")
        wallets = self.make_wallets(cfg)
        buys = {w: (amt, ts) for w, amt, ts in wallets}
        profiles = {}
        for i, (w, _, _) in enumerate(wallets):
            profiles[w] = {"tx_count": cfg["sniper_tx_count"] if i < cfg["sniper_count"] else cfg["other_tx_count"]}
        text = snipers.early_take_flag(cfg["open_ts"], buys, cfg["total_supply"], [w for w, _, _ in wallets], profiles)
        self.assertIn("59 wallets took 42.2% in the first 172 s", text)
        self.assertIn("49 look like sniper bots (1,000+ txs)", text)
        self.assertIn("0 look like fresh wallets made for this launch", text)

    def test_gta_shape_reads_as_fresh_wallet_bundle(self):
        cfg = self.load("snipers_gta.json")
        wallets = self.make_wallets(cfg)
        buys = {w: (amt, ts) for w, amt, ts in wallets}
        profiles = {}
        for i, (w, _, _) in enumerate(wallets):
            if i < cfg["fresh_count"]:
                profiles[w] = {"tx_count": 2, "first_seen_ts": cfg["open_ts"] - cfg["fresh_wallet_age_s"], "funded_for_launch": True}
            else:
                profiles[w] = {"tx_count": 20, "first_seen_ts": cfg["open_ts"] - 86400}
        text = snipers.early_take_flag(cfg["open_ts"], buys, cfg["total_supply"], [w for w, _, _ in wallets], profiles)
        self.assertIn("23 wallets took 88.0% in the first 1 s", text)
        self.assertIn("0 look like sniper bots (1,000+ txs)", text)
        self.assertIn("22 look like fresh wallets made for this launch", text)

    def test_relabel_bundle_groups_keeps_legacy_when_profiles_missing(self):
        buys = {"w1": (6.0, 100.0), "w2": (6.0, 100.4), "w3": (6.0, 100.8), "w4": (6.0, 101.2)}
        groups = cc.bundle_groups(100.0, buys, 100.0)
        relabeled = snipers.relabel_bundle_groups(groups, 100.0, buys, 100.0, {})
        self.assertEqual(relabeled[0]["text"], groups[0]["text"])

    def test_evm_bundle_flags_accept_nonce_profiles(self):
        buys = {}
        for i in range(10):
            buys[f"0x{i + 1:040x}"] = (3.5, 100.0 + i * 18.0)  # 35% in 3 minutes
        profiles = {w: {"tx_count": 1200 if i < 8 else 20} for i, w in enumerate(buys)}
        out = cc.bundle_flags(100.0, buys, 100.0, wallet_profiles=profiles)
        self.assertTrue(out and "wallets took 35.0%" in out[0], out)
        self.assertIn("8 look like sniper bots (1,000+ txs)", out[0])

    def test_same_block_flag_is_reworded_to_sniper_shape(self):
        wallets = [f"w{i}" for i in range(5)]
        buys = {w: (20.0, 100.0) for w in wallets}
        profiles = {w: {"tx_count": 1400} for w in wallets}
        text = snipers.same_block_take_flag(wallets, 100.0, buys, 100.0, profiles)
        self.assertIn("5 wallets took 100.0% in the first 0 s", text)
        self.assertIn("5 look like sniper bots (1,000+ txs)", text)

    def test_same_block_flag_is_reworded_to_fresh_shape(self):
        wallets = [f"w{i}" for i in range(4)]
        buys = {w: (10.0, 100.0) for w in wallets}
        profiles = {w: {"tx_count": 2, "first_seen_ts": 80.0, "funded_for_launch": True} for w in wallets}
        text = snipers.same_block_take_flag(wallets, 100.0, buys, 100.0, profiles)
        self.assertIn("4 wallets took 40.0% in the first 0 s", text)
        self.assertIn("4 look like fresh wallets made for this launch", text)

    def test_wallet_profiling_is_capped_to_top_60(self):
        took_by_wallet = {f"w{i:03d}": float(i) for i in range(65)}
        selected, capped = snipers.profile_wallets_by_take(took_by_wallet, max_wallets=60)
        self.assertEqual(len(selected), 60)
        self.assertTrue(capped)
        self.assertEqual(selected[0], "w064")


if __name__ == "__main__":
    unittest.main()
