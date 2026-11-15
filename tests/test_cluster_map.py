import importlib.util
import io
import math
import os
import tempfile
import unittest


def load_cluster_map_module():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    path = os.path.join(root, "cluster-map", "cluster_map.py")
    spec = importlib.util.spec_from_file_location("cluster_map_mod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CM = load_cluster_map_module()


def addr(num):
    body = "%040x" % num
    return "0x" + "1" + body[1:]


def txh(num):
    return "0x%064x" % num


def token_info(symbol="TKN", decimals=18, total_supply_units=1000):
    raw = total_supply_units * (10 ** decimals)
    return {"symbol": symbol, "decimals": str(decimals), "totalSupply": str(raw)}


def holder(address, amount_units, decimals=18):
    return {"address": address, "value": str(amount_units * (10 ** decimals))}


def native_row(frm, to, ts, idx, value=1):
    return {
        "from": frm,
        "to": to,
        "value": str(value),
        "hash": txh(idx),
        "blockNumber": str(idx),
        "timeStamp": str(ts),
    }


def token_row(frm, to, ts, idx, token_address, value=1):
    return {
        "from": frm,
        "to": to,
        "value": str(value),
        "hash": txh(idx),
        "blockNumber": str(idx),
        "timeStamp": str(ts),
        "contractAddress": token_address,
    }


def launch_transfer_row(frm, to, ts, idx, value_raw=1):
    return {
        "from": frm,
        "to": to,
        "value": str(value_raw),
        "hash": txh(idx),
        "blockNumber": str(idx),
        "timeStamp": str(ts),
    }


def empty_history_row():
    return {"txlist": {"rows": []}, "txlistinternal": {"rows": []}, "tokentx": {"rows": []}}


def make_histories(persons, events, sent=True):
    out = {p: empty_history_row() for p in persons}
    for action, row in events:
        frm = row["from"].lower()
        to = row["to"].lower()
        if frm in out:
            out[frm][action]["rows"].append(row)
            if sent and action == "tokentx":
                out[frm]["txlist"]["rows"].append(dict(row, to=row["contractAddress"], value="0"))
        if to in out and to != frm:
            out[to][action]["rows"].append(row)
    return out


class FakeBlockscoutClient:
    def __init__(self, token_meta, holders, histories, token_transfers=None):
        self.token_meta = token_meta
        self.holders = holders
        self.histories = histories
        self.token_transfers = token_transfers or []

    def get_token(self, token):
        return dict(self.token_meta)

    def get_token_holders(self, token, top_n):
        return list(self.holders[:top_n])

    def get_history(self, address, action):
        row = self.histories.get(address, {}).get(action, {"rows": [], "message": ""})
        rows = list(row.get("rows", []))
        message = str(row.get("message", ""))
        note = ""
        if action == "txlistinternal" and rows and "not yet been processed" in message.lower():
            note = f"{address}: txlistinternal included unprocessed-range warning and rows were used."
        truncated = len(rows) >= CM.HISTORY_OFFSET
        return rows, truncated, note

    def get_token_transfers(self, token):
        return list(self.token_transfers)


class FakeRpcClient:
    def __init__(self, codes=None, nonces=None):
        self.codes = codes or {}
        self.nonces = nonces or {}

    def get_code_many(self, addresses):
        return [self.codes.get(addr, "0x") for addr in addresses]

    def get_nonce_many(self, addresses):
        return [self.nonces.get(addr, 0) for addr in addresses]


def run_map(blockscout, rpc, token=None):
    token = token or addr(5000)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = os.path.join(tmp, "out")
        cache_dir = os.path.join(tmp, "cache")
        report, summary, json_path = CM.run_cluster_map(
            token_address=token,
            out_dir=out_dir,
            top=250,
            cache_dir=cache_dir,
            blockscout_client=blockscout,
            rpc_client=rpc,
            generated_at="2026-11-13T00:00:00Z",
        )
        with open(json_path, "r", encoding="utf-8") as handle:
            json_text = handle.read()
    return report, summary, json_text


class OutputTests(unittest.TestCase):
    def run_once(self, out_dir):
        p1, p2 = addr(30), addr(31)
        return CM.run_cluster_map(
            token_address=addr(5000), out_dir=out_dir, top=250, cache_dir=os.path.join(self.tmp, "cache"),
            blockscout_client=FakeBlockscoutClient(token_info(), [holder(p1, 100), holder(p2, 100)], make_histories([p1, p2], [])),
            rpc_client=FakeRpcClient(), generated_at="2026-11-13T00:00:00Z",
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_out_dir_writes_no_files(self):
        _, text, json_path = self.run_once(None)
        self.assertIsNone(json_path)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["cache"])
        self.assertIn("No group of top holders looks linked in the data read.", text)

    def test_out_dir_gets_the_json_only(self):
        out_dir = os.path.join(self.tmp, "out")
        _, _, json_path = self.run_once(out_dir)
        self.assertEqual(os.listdir(out_dir), [os.path.basename(json_path)])
        self.assertTrue(json_path.endswith("-map.json"))


class ClusterMapTests(unittest.TestCase):
    def test_seer_shape_launch_buyers_sold_out_still_cluster(self):
        token = addr(7000)
        creator = addr(90)
        pool_contract = addr(91)
        people = [addr(100 + i) for i in range(9)]
        funders = [addr(300 + i) for i in range(6)]
        holders = [holder(addr(600), 200), holder(addr(601), 180), holder(addr(602), 170)]
        info = token_info(symbol="SEER")
        launch_transfers = [launch_transfer_row(CM.ZERO_ADDRESS, creator, 900, 1, value_raw=1000 * 10**18)]
        for i, p in enumerate(people, start=2):
            launch_transfers.append(launch_transfer_row(pool_contract, p, 2000 + (i - 2) * 15, i, value_raw=50 * 10**18))

        assignments = [
            (funders[0], people[0], 1000, 1),
            (funders[0], people[1], 1018, 2),
            (funders[1], people[2], 1030, 3),
            (funders[1], people[3], 1045, 4),
            (funders[2], people[4], 1060, 5),
            (funders[2], people[5], 1080, 6),
            (funders[3], people[6], 1095, 7),
            (funders[4], people[7], 1110, 8),
            (funders[5], people[8], 1138, 9),
        ]
        events = [("txlist", native_row(f, p, ts, idx)) for f, p, ts, idx in assignments]
        histories = make_histories(people, events)
        nonces = {
            funders[0]: 1300,
            funders[1]: 2300,
            funders[2]: 5300,
            funders[3]: 8200,
            funders[4]: 15500,
            funders[5]: 2354511,
        }
        codes = {pool_contract: "0x6001"}
        fake_bs = FakeBlockscoutClient(info, holders, histories, token_transfers=launch_transfers)
        fake_rpc = FakeRpcClient(codes=codes, nonces=nonces)

        report, _, json_text = run_map(fake_bs, fake_rpc, token=token)
        wave_nodes = [x for x in report["time_nodes"] if x["subtype"] == "funding-wave"]
        self.assertEqual(len(wave_nodes), 1)
        self.assertEqual(set(wave_nodes[0]["wallets"]), set(people))
        self.assertEqual(len(report["clusters"]), 1)
        self.assertEqual(report["clusters"][0]["wallet_count"], 9)
        self.assertAlmostEqual(report["clusters"][0]["bought_at_launch"], 0.45, places=6)
        self.assertAlmostEqual(report["clusters"][0]["share_now"], 0.0, places=8)
        self.assertEqual(report["header"]["launch_buyer_count"], 9)
        sold_flags = [p for p in report["persons"] if p["is_launch_buyer"] and p["sold_out"]]
        self.assertEqual(len(sold_flags), 9)
        self.assertNotIn('"events"', json_text)

    def test_launcher_contract_as_creator_does_not_cluster_everyone(self):
        # LuckyCoin shape (Base, Nov 14): supply minted to a launcher contract that sprays dust to hundreds of wallets
        token = addr(7010)
        launcher, pool = addr(95), addr(96)
        people = [addr(400 + i) for i in range(30)]
        holders = [holder(pool, 800), holder(launcher, 199)] + [holder(p, 0) for p in people[:5]]
        transfers = [launch_transfer_row(CM.ZERO_ADDRESS, launcher, 900, 1, value_raw=1000 * 10**18)]
        events = []
        for i, p in enumerate(people):
            row = launch_transfer_row(launcher, p, 1000, 2, value_raw=9)
            transfers.append(row)
            events.append(("tokentx", token_row(launcher, p, 1000, 2, token_address=token, value=9)))
        histories = make_histories(people, events)
        codes = {launcher: "0x6001", pool: "0x6001"}
        report, _, _ = run_map(
            FakeBlockscoutClient(token_info(symbol="LUCKY"), holders, histories, token_transfers=transfers),
            FakeRpcClient(codes=codes),
            token=token,
        )
        self.assertEqual(report["header"]["creator"], "")
        self.assertNotIn(launcher, {p["address"] for p in report["persons"]})
        self.assertEqual(report["clusters"], [])

    def test_exchange_hot_wallet_holding_dust_is_not_a_person(self):
        # LuckyCoin (Base, Nov 14): 0x20fe…3b7a (1.94M txs) held dust and had paid 46 holders over two years
        token = addr(7011)
        hot = addr(97)
        people = [addr(500 + i) for i in range(6)]
        holders = [holder(p, 10) for p in people] + [holder(hot, 1)]
        events = [("tokentx", token_row(hot, p, 1000 + i, 10 + i, token_address=addr(98))) for i, p in enumerate(people)]
        report, _, _ = run_map(
            FakeBlockscoutClient(token_info(symbol="DUST"), holders, make_histories(people + [hot], events)),
            FakeRpcClient(nonces={hot: 1_942_876}),
            token=token,
        )
        self.assertNotIn(hot, {p["address"] for p in report["persons"]})
        self.assertEqual(report["clusters"], [])

    def test_magic_node_role_both(self):
        token = addr(7001)
        p1, p2, p3 = addr(10), addr(11), addr(12)
        funder = addr(900)
        holders = [holder(p1, 100), holder(p2, 100), holder(p3, 100)]
        info = token_info(symbol="BASE15")
        events = [
            ("txlist", native_row(funder, p1, 1000, 1)),
            ("txlist", native_row(funder, p2, 1010, 2)),
            ("txlist", native_row(funder, p3, 1020, 3)),
            ("txlist", native_row(p1, funder, 2000, 4)),
            ("txlist", native_row(p2, funder, 2010, 5)),
            ("txlist", native_row(p3, funder, 2020, 6)),
        ]
        histories = make_histories([p1, p2, p3], events)
        fake_bs = FakeBlockscoutClient(info, holders, histories)
        fake_rpc = FakeRpcClient(nonces={funder: 20})

        report, _, _ = run_map(fake_bs, fake_rpc, token=token)
        self.assertEqual(len(report["magic_nodes"]), 1)
        self.assertEqual(report["magic_nodes"][0]["role"], "both")
        self.assertEqual(set(report["magic_nodes"][0]["wallets"]), {p1, p2, p3})
        self.assertEqual(len(report["clusters"]), 1)
        self.assertEqual(report["clusters"][0]["wallet_count"], 3)

    def test_direct_link_dashed_then_solid(self):
        token = addr(7002)
        a, b = addr(50), addr(51)
        holders = [holder(a, 500), holder(b, 500)]
        info = token_info(symbol="DIR")

        one_way = [("tokentx", token_row(a, b, 1000, 1, token_address=token))]
        report, _, _ = run_map(
            FakeBlockscoutClient(info, holders, make_histories([a, b], one_way)),
            FakeRpcClient(),
            token=token,
        )
        direct_edges = [x for x in report["graph"]["edges"] if x["kind"] == "direct"]
        self.assertEqual(len(direct_edges), 1)
        self.assertFalse(direct_edges[0]["both_ways"])
        self.assertEqual(set(report["clusters"][0]["wallets"]), {a, b})

        two_way = [
            ("tokentx", token_row(a, b, 1000, 1, token_address=token)),
            ("tokentx", token_row(b, a, 1010, 2, token_address=token)),
        ]
        report2, _, _ = run_map(
            FakeBlockscoutClient(info, holders, make_histories([a, b], two_way)),
            FakeRpcClient(),
            token=token,
        )
        direct_edges2 = [x for x in report2["graph"]["edges"] if x["kind"] == "direct"]
        self.assertTrue(direct_edges2[0]["both_ways"])

    def test_contract_counterparty_is_not_magic_or_time_node(self):
        token = addr(7003)
        people = [addr(1000 + i) for i in range(10)]
        router = addr(2000)
        holders = [holder(p, 10) for p in people]
        info = token_info(symbol="ROUTER")
        events = []
        for i, p in enumerate(people, start=1):
            events.append(("tokentx", token_row(p, router, 1000 + i, i, token_address=token)))
        histories = make_histories(people, events)
        fake_bs = FakeBlockscoutClient(info, holders, histories)
        fake_rpc = FakeRpcClient(codes={router: "0x6001"}, nonces={router: 5000})

        report, _, _ = run_map(fake_bs, fake_rpc, token=token)
        self.assertEqual(report["magic_nodes"], [])
        self.assertEqual(report["time_nodes"], [])
        self.assertEqual(report["clusters"], [])

    def test_busy_funder_far_apart_no_time_node(self):
        token = addr(7004)
        p1, p2 = addr(3001), addr(3002)
        funder = addr(4001)
        holders = [holder(p1, 100), holder(p2, 100)]
        info = token_info(symbol="TIME")
        events = [
            ("txlist", native_row(funder, p1, 1_000, 1)),
            ("txlist", native_row(funder, p2, 1_000 + 7200, 2)),
        ]
        histories = make_histories([p1, p2], events)
        fake_bs = FakeBlockscoutClient(info, holders, histories)
        fake_rpc = FakeRpcClient(nonces={funder: 12000})

        report, _, _ = run_map(fake_bs, fake_rpc, token=token)
        self.assertEqual(report["time_nodes"], [])
        self.assertEqual(report["clusters"], [])

    def busy_funder_pair_report(self, fund_ts):
        token = addr(7012)
        p1, p2 = addr(3011), addr(3012)
        funder, pool, buyer = addr(4011), addr(4012), addr(4013)
        holders = [holder(p1, 100), holder(p2, 100)]
        launch_transfers = [launch_transfer_row(pool, buyer, 5_000, 1)]
        events = [
            ("txlist", native_row(funder, p1, fund_ts, 2)),
            ("txlist", native_row(funder, p2, fund_ts + 60, 3)),
        ]
        report, _, _ = run_map(
            FakeBlockscoutClient(token_info(symbol="WIN"), holders, make_histories([p1, p2], events),
                                 token_transfers=launch_transfers),
            FakeRpcClient(codes={pool: "0x6001"}, nonces={funder: 12000}),
            token=token,
        )
        self.assertEqual(report["header"]["first_trade_timestamp"], 5_000)
        return report, {p1, p2}

    def test_busy_funder_after_launch_no_time_node(self):
        # Unrelated buyers withdraw from the same exchange hot wallet minutes apart to buy a hot coin.
        report, _ = self.busy_funder_pair_report(6_000)
        self.assertEqual([x for x in report["time_nodes"] if x["subtype"] == "busy-funder-window"], [])
        self.assertEqual(report["clusters"], [])

    def test_busy_funder_before_launch_is_time_node(self):
        report, pair = self.busy_funder_pair_report(1_000)
        nodes = [x for x in report["time_nodes"] if x["subtype"] == "busy-funder-window"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(set(nodes[0]["wallets"]), pair)
        self.assertEqual(len(report["clusters"]), 1)
        self.assertEqual(set(report["clusters"][0]["wallets"]), pair)

    def test_burn_and_contract_holders_excluded_and_shares_use_decimals(self):
        token = addr(7005)
        burn = CM.DEAD_ADDRESS
        contract_holder = addr(5010)
        p1, p2 = addr(5011), addr(5012)
        decimals = 2
        holders = [
            holder(burn, 300, decimals=decimals),
            holder(contract_holder, 200, decimals=decimals),
            holder(p1, 100, decimals=decimals),
            holder(p2, 400, decimals=decimals),
        ]
        info = token_info(symbol="MATH", decimals=decimals, total_supply_units=1000)
        histories = make_histories([p1, p2], [])
        fake_bs = FakeBlockscoutClient(info, holders, histories)
        fake_rpc = FakeRpcClient(codes={contract_holder: "0x60016001"})

        report, _, _ = run_map(fake_bs, fake_rpc, token=token)
        self.assertEqual(report["header"]["persons"], 2)
        kinds = {x["address"]: x["kind"] for x in report["holders"]}
        self.assertEqual(kinds[burn], "burn")
        self.assertEqual(kinds[contract_holder], "contract")
        shares = {x["address"]: x["share_now"] for x in report["persons"]}
        self.assertAlmostEqual(shares[p1], 0.1, places=8)
        self.assertAlmostEqual(shares[p2], 0.4, places=8)

    def test_txlistinternal_warning_keeps_rows_and_note(self):
        token = addr(7006)
        a, b = addr(6101), addr(6102)
        holders = [holder(a, 500), holder(b, 500)]
        info = token_info(symbol="INTERNAL")
        row = native_row(a, b, 1000, 1)
        histories = {
            a: {
                "txlist": {"rows": []},
                "txlistinternal": {
                    "rows": [row],
                    "message": "Some internal transactions within this block range have not yet been processed",
                },
                "tokentx": {"rows": []},
            },
            b: empty_history_row(),
        }
        fake_bs = FakeBlockscoutClient(info, holders, histories)
        fake_rpc = FakeRpcClient()

        report, _, _ = run_map(fake_bs, fake_rpc, token=token)
        self.assertEqual(len(report["clusters"]), 1)
        note_text = "\n".join(report["notes"]).lower()
        self.assertIn("unprocessed-range warning", note_text)

    def test_key_not_in_json_or_summary(self):
        token = addr(7007)
        a, b = addr(7101), addr(7102)
        holders = [holder(a, 500), holder(b, 500)]
        info = token_info(symbol="KEY")
        histories = make_histories([a, b], [("tokentx", token_row(a, b, 1000, 1, token_address=token))])
        fake_bs = FakeBlockscoutClient(info, holders, histories)
        fake_rpc = FakeRpcClient()

        fake_key = "fake-secret-blockscout-key"
        old = os.environ.get("BLOCKSCOUT_API_KEY")
        os.environ["BLOCKSCOUT_API_KEY"] = fake_key
        try:
            _, summary, json_text = run_map(fake_bs, fake_rpc, token=token)
        finally:
            if old is None:
                del os.environ["BLOCKSCOUT_API_KEY"]
            else:
                os.environ["BLOCKSCOUT_API_KEY"] = old
        self.assertNotIn(fake_key, summary)
        self.assertNotIn(fake_key, json_text)

    def test_chat_text_smoke(self):
        token = addr(7008)
        p1, p2, p3 = addr(8001), addr(8002), addr(8003)
        funder = addr(9001)
        holders = [holder(p1, 120), holder(p2, 140), holder(p3, 160)]
        info = token_info(symbol="HTML")
        events = [
            ("txlist", native_row(funder, p1, 1000, 1)),
            ("txlist", native_row(funder, p2, 1010, 2)),
            ("txlist", native_row(funder, p3, 1020, 3)),
            ("txlist", native_row(p1, funder, 2000, 4)),
            ("txlist", native_row(p2, funder, 2010, 5)),
            ("txlist", native_row(p3, funder, 2020, 6)),
        ]
        histories = make_histories([p1, p2, p3], events)
        report, summary, _ = run_map(
            FakeBlockscoutClient(info, holders, histories),
            FakeRpcClient(nonces={funder: 8}),
            token=token,
        )
        self.assertNotIn("<", summary)
        self.assertIn(CM.plain_answer(report["clusters"]), summary)
        for cluster in report["clusters"]:
            self.assertIn(cluster["why"], summary)
            for row in cluster["wallet_rows"]:
                self.assertIn(row["address"], summary)


if __name__ == "__main__":
    unittest.main()


class BlockscoutStatusZeroTests(unittest.TestCase):
    def make_client(self, responses, cache_dir):
        calls = []

        def fake_fetch(url, data, headers, timeout):
            calls.append(url)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        client = CM.BlockscoutClient("fake-key", CM.DiskCache(cache_dir), CM.RequestStats(), fetch_json=fake_fetch,
                                     limiter=CM.RateLimiter(1000))
        return client, calls

    def test_rate_limit_body_is_retried_and_not_cached(self):
        rows = [native_row(addr(1), addr(2), 100, 1)]
        limited = {"status": "0", "message": "NOTOK", "result": "Max calls per sec rate limit reached (5/sec)"}
        with tempfile.TemporaryDirectory() as tmp:
            client, calls = self.make_client([limited, {"status": "1", "message": "OK", "result": rows}], tmp)
            got, _, _ = client.get_history(addr(2), "txlist")
            self.assertEqual(got, rows)
            self.assertEqual(len(calls), 2)
            again, _, _ = client.get_history(addr(2), "txlist")
            self.assertEqual(again, rows)
            self.assertEqual(len(calls), 2)

    def test_persistent_error_body_raises_instead_of_empty_history(self):
        limited = {"status": "0", "message": "NOTOK", "result": "Max calls per sec rate limit reached (5/sec)"}
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self.make_client([limited], tmp)
            with self.assertRaises(RuntimeError):
                client.get_history(addr(2), "txlist")

    def test_no_transactions_found_is_empty_not_error(self):
        empty = {"status": "0", "message": "No transactions found", "result": []}
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self.make_client([empty], tmp)
            got, _, _ = client.get_history(addr(2), "txlist")
            self.assertEqual(got, [])

    def test_history_pages_until_short_page(self):
        rows_a = [native_row(addr(1), addr(2), 100 + i, i + 1) for i in range(CM.HISTORY_OFFSET)]
        rows_b = [native_row(addr(1), addr(2), 4000 + i, 5000 + i) for i in range(CM.HISTORY_OFFSET)]
        rows_c = [native_row(addr(1), addr(2), 9000 + i, 9000 + i) for i in range(10)]
        calls = []

        def fake_fetch(url, data, headers, timeout):
            calls.append(url)
            page = int(url.split("page=")[1].split("&")[0])
            if page == 1:
                return {"status": "1", "message": "OK", "result": rows_a}
            if page == 2:
                return {"status": "1", "message": "OK", "result": rows_b}
            return {"status": "1", "message": "OK", "result": rows_c}

        with tempfile.TemporaryDirectory() as tmp:
            client = CM.BlockscoutClient("fake-key", CM.DiskCache(tmp), CM.RequestStats(), fetch_json=fake_fetch,
                                         limiter=CM.RateLimiter(1000))
            rows, truncated, _ = client.get_history(addr(2), "txlist")
            self.assertFalse(truncated)
            self.assertEqual(len(rows), CM.HISTORY_OFFSET * 2 + 10)
            self.assertEqual(len(calls), 3)


class FalseLinkTests(unittest.TestCase):
    def three_persons(self):
        p1, p2, p3 = addr(20), addr(21), addr(22)
        return [p1, p2, p3], [holder(p1, 100), holder(p2, 100), holder(p3, 100)]

    def test_token_airdrop_from_stranger_is_not_a_link(self):
        persons, holders = self.three_persons()
        sprayer, spam = addr(950), addr(951)
        events = [("tokentx", token_row(sprayer, p, 1000 + i, 30 + i, spam)) for i, p in enumerate(persons)]
        report, _, _ = run_map(FakeBlockscoutClient(token_info(), holders, make_histories(persons, events)),
                                  FakeRpcClient(nonces={sprayer: 5}))
        self.assertEqual(report["magic_nodes"], [])
        self.assertEqual(report["clusters"], [])

    def test_zero_address_mints_are_not_a_link(self):
        persons, holders = self.three_persons()
        other = addr(952)
        events = [("tokentx", token_row(CM.ZERO_ADDRESS, p, 1000 + i, 40 + i, other)) for i, p in enumerate(persons)]
        events += [("txlistinternal", native_row(CM.ZERO_ADDRESS, p, 2000 + i, 50 + i)) for i, p in enumerate(persons)]
        report, _, _ = run_map(FakeBlockscoutClient(token_info(), holders, make_histories(persons, events)),
                                  FakeRpcClient())
        self.assertEqual(report["magic_nodes"], [])
        self.assertEqual(report["clusters"], [])

    def test_tokens_sent_to_one_collector_still_link(self):
        persons, holders = self.three_persons()
        collector, tok = addr(953), addr(954)
        events = [("tokentx", token_row(p, collector, 1000 + i, 60 + i, tok)) for i, p in enumerate(persons)]
        report, _, _ = run_map(FakeBlockscoutClient(token_info(), holders, make_histories(persons, events)),
                                  FakeRpcClient(nonces={collector: 5}))
        self.assertEqual(len(report["magic_nodes"]), 1)
        self.assertEqual(report["magic_nodes"][0]["role"], "collector")
        self.assertEqual(report["clusters"][0]["wallet_count"], 3)


class DeepPageFailureTests(unittest.TestCase):
    def test_failing_deep_page_marks_truncated_and_keeps_rows(self):
        full = {"status": "1", "message": "OK", "result": [native_row(addr(1), addr(2), 100, i + 1) for i in range(CM.HISTORY_OFFSET)]}

        def fake_fetch(url, data, headers, timeout):
            if "page=1&" in url:
                return full
            raise CM.urllib.error.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(b'{"error":"Internal server error"}'))

        with tempfile.TemporaryDirectory() as tmp:
            client = CM.BlockscoutClient("fake-key", CM.DiskCache(tmp), CM.RequestStats(), fetch_json=fake_fetch,
                                         limiter=CM.RateLimiter(1000))
            rows, truncated, note = client.get_history(addr(2), "tokentx")
        self.assertEqual(len(rows), CM.HISTORY_OFFSET)
        self.assertTrue(truncated)
        self.assertIn("page 2", note)


class OutputSizeTests(unittest.TestCase):
    def test_contract_bytecode_not_written_to_json(self):
        p1, p2 = addr(30), addr(31)
        router = addr(960)
        holders = [holder(p1, 100), holder(p2, 100)]
        events = [("txlist", native_row(router, p1, 1000, 70)), ("txlist", native_row(router, p2, 1001, 71))]
        bytecode = "0x" + "60" * 5000
        report, _, json_text = run_map(FakeBlockscoutClient(token_info(), holders, make_histories([p1, p2], events)),
                                          FakeRpcClient(codes={router: bytecode}))
        self.assertNotIn("60" * 100, json_text)


class RankingTests(unittest.TestCase):
    def test_launch_bundle_outranks_smaller_holding(self):
        rows = [{"share_now": 0.03, "bought_at_launch": 0.0, "wallets": ["0xa"]},
                {"share_now": 0.0, "bought_at_launch": 0.45, "wallets": ["0xb"]}]
        rows.sort(key=CM.cluster_sort_key)
        self.assertEqual(rows[0]["wallets"], ["0xb"])


def layout_fixture_clients():
    token = addr(8800)
    creator = addr(8810)
    pool = addr(8811)
    buyers = [addr(8820 + i) for i in range(9)]
    funders = [addr(8830 + i) for i in range(6)]
    direct_a, direct_b = addr(8840), addr(8841)
    g1, g2, g3 = addr(8850), addr(8851), addr(8852)
    collector = addr(8860)
    u1, u2, u3 = addr(8870), addr(8871), addr(8872)

    holders = [
        holder(direct_a, 90),
        holder(direct_b, 85),
        holder(g1, 60),
        holder(g2, 58),
        holder(g3, 55),
        holder(u1, 30),
        holder(u2, 25),
        holder(u3, 22),
    ]
    info = token_info(symbol="LAY", total_supply_units=1000)
    launch_transfers = [launch_transfer_row(CM.ZERO_ADDRESS, creator, 900, 1, value_raw=1000 * 10**18)]
    for i, p in enumerate(buyers, start=2):
        launch_transfers.append(launch_transfer_row(pool, p, 2000 + (i - 2) * 15, i, value_raw=50 * 10**18))

    events = []
    assignments = [
        (funders[0], buyers[0], 1000, 1),
        (funders[0], buyers[1], 1018, 2),
        (funders[1], buyers[2], 1030, 3),
        (funders[1], buyers[3], 1045, 4),
        (funders[2], buyers[4], 1060, 5),
        (funders[2], buyers[5], 1080, 6),
        (funders[3], buyers[6], 1095, 7),
        (funders[4], buyers[7], 1110, 8),
        (funders[5], buyers[8], 1138, 9),
    ]
    events.extend([("txlist", native_row(f, p, ts, idx)) for f, p, ts, idx in assignments])
    events.extend(
        [
            ("tokentx", token_row(direct_a, direct_b, 3000, 100, token_address=token)),
            ("tokentx", token_row(direct_b, direct_a, 3010, 101, token_address=token)),
            ("tokentx", token_row(g1, collector, 3200, 110, token_address=token)),
            ("tokentx", token_row(g2, collector, 3210, 111, token_address=token)),
            ("tokentx", token_row(g3, collector, 3220, 112, token_address=token)),
        ]
    )
    persons = buyers + [direct_a, direct_b, g1, g2, g3, u1, u2, u3]
    histories = make_histories(persons, events)
    nonces = {
        funders[0]: 1300,
        funders[1]: 2300,
        funders[2]: 5300,
        funders[3]: 8200,
        funders[4]: 15500,
        funders[5]: 2354511,
        collector: 15,
    }
    codes = {pool: "0x6001"}
    fake_bs = FakeBlockscoutClient(info, holders, histories, token_transfers=launch_transfers)
    fake_rpc = FakeRpcClient(codes=codes, nonces=nonces)
    return token, fake_bs, fake_rpc


class UnlinkedTests(unittest.TestCase):

    def test_unlinked_holders_counted_and_json_has_shares(self):
        token, fake_bs, fake_rpc = layout_fixture_clients()
        report, summary, json_text = run_map(fake_bs, fake_rpc, token=token)
        self.assertIn(f"Not linked to anyone: {len(report['unclustered_wallets'])} wallet", summary)
        self.assertIn("share_now_pct", json_text)
        self.assertIn("bought_at_launch_pct", json_text)
