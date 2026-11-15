import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cluster_map import (
    CM,
    FakeBlockscoutClient,
    FakeRpcClient,
    addr,
    holder,
    make_histories,
    native_row,
    run_map,
    token_info,
    token_row,
)

SPOOF_NOTE = ("3 token transfers were not counted as links: the holder shown as sender did not send the transaction "
              "(look-alike spam, or sent through a relayer).")


class SpoofedTokenTransferTests(unittest.TestCase):
    def three_persons(self):
        p1, p2, p3 = addr(40), addr(41), addr(42)
        return [p1, p2, p3], [holder(p1, 100), holder(p2, 100), holder(p3, 100)]

    def test_spoofed_transfers_to_lookalike_do_not_link(self):
        persons, holders = self.three_persons()
        lookalike, fake_token = addr(980), addr(981)
        events = [("tokentx", token_row(p, lookalike, 1000 + i, 80 + i, fake_token)) for i, p in enumerate(persons)]
        report, _, _ = run_map(
            FakeBlockscoutClient(token_info(), holders, make_histories(persons, events, sent=False)),
            FakeRpcClient(nonces={lookalike: 5}),
        )
        self.assertEqual(report["clusters"], [])
        self.assertEqual(report["magic_nodes"], [])
        self.assertIn(SPOOF_NOTE, report["notes"])

    def test_transfers_the_holders_sent_still_link(self):
        persons, holders = self.three_persons()
        collector, tok = addr(982), addr(983)
        events = []
        for i, p in enumerate(persons):
            events.append(("tokentx", token_row(p, collector, 1000 + i, 90 + i, tok)))
            events.append(("txlist", native_row(p, tok, 1000 + i, 90 + i, value=0)))
        report, _, _ = run_map(
            FakeBlockscoutClient(token_info(), holders, make_histories(persons, events, sent=False)),
            FakeRpcClient(nonces={collector: 5}),
        )
        self.assertEqual(len(report["clusters"]), 1)
        self.assertEqual(set(report["clusters"][0]["wallets"]), set(persons))
        self.assertFalse(any("not counted as" in n for n in report["notes"]))

    def test_spoofed_direct_transfer_does_not_link(self):
        a, b = addr(60), addr(61)
        holders = [holder(a, 500), holder(b, 500)]
        events = [("tokentx", token_row(a, b, 1000, 120, token_address=addr(984)))]
        histories = make_histories([a, b], events, sent=False)
        self.assertEqual(len(histories[a]["tokentx"]["rows"]), 1)
        self.assertEqual(len(histories[b]["tokentx"]["rows"]), 1)
        report, _, _ = run_map(FakeBlockscoutClient(token_info(), holders, histories), FakeRpcClient())
        self.assertEqual(report["direct_links"], [])
        self.assertEqual(report["clusters"], [])

    def test_sends_after_a_cut_tx_list_still_link(self):
        persons, holders = self.three_persons()
        collector, lookalike, tok = addr(986), addr(987), addr(988)
        events = []
        for i, p in enumerate(persons):
            events.append(("txlist", native_row(p, tok, 500, 200 + 2 * i, value=0)))
            events.append(("txlist", native_row(p, tok, 501, 201 + 2 * i, value=0)))
            events.append(("tokentx", token_row(p, lookalike, 400, 220 + i, tok)))
            events.append(("tokentx", token_row(p, collector, 1000 + i, 230 + i, tok)))
        with mock.patch.object(CM, "HISTORY_OFFSET", 2):
            report, _, _ = run_map(
                FakeBlockscoutClient(token_info(), holders, make_histories(persons, events, sent=False)),
                FakeRpcClient(nonces={collector: 5, lookalike: 5}),
            )
        self.assertEqual([m["address"] for m in report["magic_nodes"]], [collector])
        self.assertIn(SPOOF_NOTE, report["notes"])



class CompactFixtureProofTests(unittest.TestCase):
    def test_zero_value_txlist_row_proving_a_kept_token_transfer_is_kept(self):
        token, person, buyer = addr(7300), addr(70), addr(71)
        sold = token_row(person, buyer, 1000, 130, token_address=token, value=5)
        proof = native_row(person, token, 1000, 130, value=0)
        unrelated = native_row(person, addr(985), 1100, 131, value=0)
        data = {
            "token": token_info(),
            "token_address": token,
            "holders": [holder(person, 100)],
            "transfers": [],
            "history": {
                person: {
                    "txlist": [[proof, unrelated], False, ""],
                    "txlistinternal": [[], False, ""],
                    "tokentx": [[sold], False, ""],
                }
            },
            "tx_internal": {},
        }
        out = CM.compact_source_fixture(data)
        self.assertEqual(out["history"][person]["tokentx"][0], [CM.trim_row(sold)])
        self.assertEqual(out["history"][person]["txlist"][0], [CM.trim_row(proof)])

    def test_address_read_for_one_list_only_is_kept_as_is(self):
        token, follower = addr(7300), addr(72)
        data = {
            "token": token_info(),
            "token_address": token,
            "holders": [],
            "transfers": [],
            "history": {follower: {"txlist": [[], False, ""]}},
            "tx_internal": {},
        }
        self.assertEqual(CM.compact_source_fixture(data)["history"][follower], {"txlist": [[], False, ""]})


if __name__ == "__main__":
    unittest.main()
