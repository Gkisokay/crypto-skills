import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import solana_traps as st  # noqa: E402


class SolanaTraps(unittest.TestCase):
    FIX = Path(__file__).parent / "fixtures" / "solana_traps_accounts.json"

    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(cls.FIX.read_text())

    def findings_for(self, name):
        flags, notes, cant_check = st.mint_trap_findings(self.fixtures[name]["value"])
        self.assertEqual(cant_check, [])
        return flags, notes

    def flags_for(self, name):
        flags, _ = self.findings_for(name)
        return flags

    def notes_for(self, name):
        _, notes = self.findings_for(name)
        return notes

    def test_clean_pump_fun_shape_has_no_trap_flags(self):
        self.assertEqual(self.flags_for("clean_pump"), [])

    def test_mint_authority_flag(self):
        flags = self.flags_for("mint_authority")
        self.assertEqual(len(flags), 1)
        self.assertIn("Mint authority is still set to DDmGnuvroxVrb7CSyjYcezFU8gnAfhKasg4ZZezmpump", flags[0])

    def test_freeze_authority_flag(self):
        flags = self.flags_for("freeze_authority")
        self.assertEqual(len(flags), 1)
        self.assertIn("Freeze authority is still set to 7cW45tcSDSiog8poB5LFu7CD1g4TX6JYJ8s4Ur29cb5H", flags[0])

    def test_transfer_fee_config_flag(self):
        flags = self.flags_for("transfer_fee_config")
        self.assertEqual(len(flags), 1)
        self.assertIn("can change the fee", flags[0])
        self.assertIn("FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM", flags[0])
        self.assertIn("4.5%", flags[0])
        self.assertIn("3.5%", flags[0])
        self.assertIn("25", flags[0])
        self.assertIn("40", flags[0])

    def test_transfer_fee_config_revoked_authority_does_not_borrow_other_addresses(self):
        flags = self.flags_for("transfer_fee_revoked_authority")
        fee_line = next(f for f in flags if "fixed 2.5% fee" in f)
        self.assertIn("fee authority is revoked", fee_line)
        self.assertNotIn("Hkm9m53BVZsV3SRf23zzq6BVEpcsfz2M6Ucsa837Nzk7", fee_line)
        self.assertNotIn("DDmGnuvroxVrb7CSyjYcezFU8gnAfhKasg4ZZezmpump", fee_line)

    def test_transfer_fee_zero_and_revoked_authority_has_no_flag(self):
        flags = self.flags_for("transfer_fee_zero_revoked")
        self.assertFalse(any("Transfer fee authority" in f or "fixed " in f for f in flags), flags)

    def test_transfer_hook_flag(self):
        flags = self.flags_for("transfer_hook")
        self.assertEqual(len(flags), 1)
        self.assertIn("Transfer hook is enabled", flags[0])
        self.assertIn("2tHSj5CRX9uXRuDw5PJoSuWvw4JT9TFxFc8zZYFSVnyP", flags[0])
        self.assertIn("noopb9bkMVfRPU8AsbpTUg8AQkHtKwMYZiFUjNRtMmV", flags[0])

    def test_transfer_hook_with_null_program_id_has_no_flag(self):
        flags = self.flags_for("transfer_hook_no_program")
        self.assertEqual(flags, [])

    def test_permanent_delegate_flag(self):
        flags = self.flags_for("permanent_delegate")
        self.assertEqual(len(flags), 1)
        self.assertIn("Permanent delegate is set to HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC", flags[0])

    def test_permanent_delegate_null_has_no_flag(self):
        flags = self.flags_for("permanent_delegate_null")
        self.assertEqual(flags, [])

    def test_default_account_state_frozen_flag(self):
        flags = self.flags_for("default_account_state_frozen")
        self.assertEqual(len(flags), 2)
        self.assertTrue(any("Default account state is frozen" in f for f in flags), flags)
        self.assertTrue(any("7cW45tcSDSiog8poB5LFu7CD1g4TX6JYJ8s4Ur29cb5H" in f for f in flags), flags)

    def test_non_transferable_flag(self):
        flags = self.flags_for("non_transferable")
        self.assertEqual(len(flags), 2)
        nt_line = next(f for f in flags if "Non-transferable is enabled" in f)
        self.assertIn("transfers are disabled for this token", nt_line)
        self.assertNotIn("CJyaPwhfcRHL57Nrmr4yTV2egpWoGx9ZeKQjScDfi3Hd", nt_line)

    def test_confidential_transfer_is_note_not_flag(self):
        flags = self.flags_for("confidential_transfer")
        notes = self.notes_for("confidential_transfer")
        self.assertEqual(flags, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("Confidential transfers are on", notes[0])
        self.assertIn("hidden from checks like this one", notes[0])

    def test_pausable_flag(self):
        flags = self.flags_for("pausable_config")
        self.assertEqual(len(flags), 1)
        self.assertIn("Pausable extension is enabled", flags[0])
        self.assertIn("currently paused", flags[0])
        self.assertIn("2tHSj5CRX9uXRuDw5PJoSuWvw4JT9TFxFc8zZYFSVnyP", flags[0])

    def test_new_flags_do_not_use_banned_words(self):
        banned = ("buy", "sell", "safe", "scam")
        for name in self.fixtures:
            flags, notes = self.findings_for(name)
            for line in flags + notes:
                low = line.lower()
                self.assertTrue(all(word not in low for word in banned), line)


if __name__ == "__main__":
    unittest.main()
