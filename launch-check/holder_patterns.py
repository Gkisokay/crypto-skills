"""Holder and flow pattern checks for launch-check v2.

This module adds:
- wallet-farm detection (equal-slice person holders + one-hop funder sample),
- FDV/liquidity mismatch checks,
- concentrated back-and-forth volume checks.
"""

import collections
import concurrent.futures
import statistics
import time
import urllib.error


WALLET_FARM_MIN_WALLETS = 20
WALLET_FARM_SLICE_TOLERANCE = 0.15
WALLET_FARM_MIN_GROUP_SHARE = 0.25
WALLET_FARM_MIN_SLICE_SHARE = 0.0075
WALLET_FARM_SAMPLE = 20
WALLET_FARM_TX_DETAIL_SAMPLE = 20
WALLET_FARM_MAX_TXS = 4
WALLET_FARM_LOW_TX_RATIO = 0.70
ONE_HOP_LOOKBACK_S = 24 * 3600
ONE_HOP_BUDGET_S = 8.0
SOLANA_HOLDER_READ_BUDGET_S = 20.0

EXTREME_FDV_TO_LIQUIDITY_RATIO = 120.0
EXTREME_FDV_MIN_USD = 50_000

FAKE_VOLUME_TOP_WALLETS = 4
FAKE_VOLUME_MIN_TOTAL_SWAPS = 80
FAKE_VOLUME_MIN_WALLET_SWAPS = 20
FAKE_VOLUME_MIN_SWAP_SHARE = 0.60
FAKE_VOLUME_MIN_VOLUME_SHARE = 0.55
FAKE_VOLUME_MIN_SYMMETRY = 0.65

SOLANA_SYSTEM = "11111111111111111111111111111111"
SOLANA_BURN = "1nc1nerator11111111111111111111111111111111"
SOLANA_TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


def _median(values):
    if not values:
        return 0.0
    return statistics.median(values)


def _format_pct(value, digits=1):
    return f"{100.0 * value:.{digits}f}%"


def fake_market_cap_flag(market):
    if not market:
        return None
    fdv = float(market.get("dex_fdv") or 0.0)
    liq = market.get("liquidity")
    if liq is None:
        return None
    liq = float(liq or 0.0)
    if fdv < EXTREME_FDV_MIN_USD or liq <= 0:
        return None
    ratio = fdv / liq
    if ratio < EXTREME_FDV_TO_LIQUIDITY_RATIO:
        return None
    return (f"FDV is ${fdv:,.0f} while listed liquidity is ${liq:,.0f} ({ratio:.0f}x); "
            "that gap is far larger than this liquidity can support.")


def _equal_slice_group(people_rows):
    """people_rows: list[(wallet, share)]"""
    rows = [(w, float(s)) for w, s in people_rows if s > 0]
    if len(rows) < WALLET_FARM_MIN_WALLETS:
        return None
    best = None
    for _, anchor in rows:
        if anchor < WALLET_FARM_MIN_SLICE_SHARE:
            continue
        lo = anchor * (1.0 - WALLET_FARM_SLICE_TOLERANCE)
        hi = anchor * (1.0 + WALLET_FARM_SLICE_TOLERANCE)
        group = [(w, s) for w, s in rows if lo <= s <= hi]
        if len(group) < WALLET_FARM_MIN_WALLETS:
            continue
        med = _median([s for _, s in group])
        lo2 = med * (1.0 - WALLET_FARM_SLICE_TOLERANCE)
        hi2 = med * (1.0 + WALLET_FARM_SLICE_TOLERANCE)
        group = [(w, s) for w, s in group if lo2 <= s <= hi2]
        share = sum(s for _, s in group)
        if len(group) < WALLET_FARM_MIN_WALLETS or share < WALLET_FARM_MIN_GROUP_SHARE:
            continue
        score = (share, len(group), med)
        if best is None or score > best["score"]:
            best = {
                "wallets": [w for w, _ in sorted(group, key=lambda x: x[1], reverse=True)],
                "share": share,
                "slice_share": med,
                "score": score,
            }
    if not best:
        return None
    best.pop("score", None)
    return best


def _is_low_tx_group(tx_counts):
    if not tx_counts:
        return False
    small = sum(1 for x in tx_counts if x <= WALLET_FARM_MAX_TXS)
    return (small / len(tx_counts)) >= WALLET_FARM_LOW_TX_RATIO


def _tx_activity_text(sampled_wallets, tx_count_by_wallet):
    counts = [tx_count_by_wallet[w] for w in sampled_wallets if w in tx_count_by_wallet]
    if not counts:
        return "tx activity couldn't be sampled"
    if _is_low_tx_group(counts):
        return f"most have <={WALLET_FARM_MAX_TXS} txs (made for this)"
    med = int(round(_median(counts)))
    return f"they trade actively (median {med} txs)"


def _summarize_funders(sampled_wallets, funders):
    known = [(w, funders.get(w)) for w in sampled_wallets if funders.get(w)]
    if not sampled_wallets:
        return {"suffix": "one-hop funding couldn't be sampled.", "top_count": 0}
    if len(known) < 2:
        return {"suffix": (f"one-hop funding was visible for {len(known)} of the sampled {len(sampled_wallets)} wallets, "
                           "which is too little to judge shared funding."),
                "top_count": 0}
    counts = collections.Counter(f for _, f in known)
    top_funder, top_count = max(counts.items(), key=lambda x: (x[1], x[0]))
    if top_count >= 2:
        return {"suffix": (f"{top_count} of the sampled {len(sampled_wallets)} were funded by one wallet "
                           f"{top_funder} one hop up."),
                "top_count": top_count}
    return {"suffix": f"one-hop funders were distinct across the sampled {len(sampled_wallets)} wallets.", "top_count": 1}


def wallet_farm_flag(group, sampled_wallets, funders, tx_count_by_wallet):
    summary = _summarize_funders(sampled_wallets, funders)
    activity = _tx_activity_text(sampled_wallets, tx_count_by_wallet)
    return (f"{len(group['wallets'])} wallets hold {_format_pct(group['share'])} in near-equal slices of "
            f"~{_format_pct(group['slice_share'])} each; {activity}; {summary['suffix']}")


def wallet_farm_from_rows(people_rows, tx_count_by_wallet, funder_by_wallet, sample_size=WALLET_FARM_SAMPLE):
    group = _equal_slice_group(people_rows)
    if not group:
        return None
    sampled = [w for w in group["wallets"][:sample_size]]
    return wallet_farm_flag(group, sampled, funder_by_wallet, tx_count_by_wallet)


def _scan_evm_funders_one_hop(rpc, first_receive_block, sampled_wallets, block_time_s):
    stop = time.monotonic() + ONE_HOP_BUDGET_S
    block_time_s = max(float(block_time_s or 2.0), 0.1)
    lookback = max(200, int(ONE_HOP_LOOKBACK_S / block_time_s))
    lo = {}
    hi = {}
    for w in sampled_wallets:
        b = first_receive_block.get(w)
        if b is None:
            continue
        lo[w] = max(1, b - lookback)
        hi[w] = b
    if not hi:
        return {}

    while any(lo[w] < hi[w] for w in hi):
        if time.monotonic() >= stop:
            break
        todo = [w for w in hi if lo[w] < hi[w]]
        mids = {w: (lo[w] + hi[w]) // 2 for w in todo}
        res = rpc.batch([("eth_getBalance", [w, hex(mids[w])]) for w in todo])
        for w, bal in zip(todo, res):
            if int(bal or "0x0", 16) > 0:
                hi[w] = mids[w]
            else:
                lo[w] = mids[w] + 1

    done = {w: hi[w] for w in hi if lo[w] == hi[w]}
    if not done:
        return {}
    blocks = sorted(set(done.values()))
    got_blocks = rpc.batch([("eth_getBlockByNumber", [hex(b), True]) for b in blocks])
    by_block = dict(zip(blocks, got_blocks))
    funders = {}
    for w, b in done.items():
        block = by_block.get(b) or {}
        sender = None
        for tx in block.get("transactions", []):
            if (tx.get("to") or "").lower() == w and int(tx.get("value") or "0x0", 16) > 0:
                sender = (tx.get("from") or "").lower()
                if sender:
                    break
        if sender:
            funders[w] = sender
    return funders


def _evm_pool_flow_stats(logs, pools, unit):
    stats = collections.defaultdict(lambda: {"swaps": 0, "inflow": 0.0, "outflow": 0.0})
    if not pools:
        return stats
    pool_set = {p.lower() for p in pools}
    for l in logs or []:
        ts = l.get("topics") or []
        if len(ts) < 3:
            continue
        frm = "0x" + ts[1][26:]
        to = "0x" + ts[2][26:]
        amt = int(l.get("data") or "0x0", 16) / unit
        if frm in pool_set and to not in pool_set:
            s = stats[to]
            s["swaps"] += 1
            s["inflow"] += amt
        elif to in pool_set and frm not in pool_set:
            s = stats[frm]
            s["swaps"] += 1
            s["outflow"] += amt
    return stats


def fake_volume_flag_from_stats(stats):
    rows = [(w, s) for w, s in stats.items() if s["swaps"] > 0]
    if not rows:
        return None
    total_swaps = sum(s["swaps"] for _, s in rows)
    total_vol = sum(s["inflow"] + s["outflow"] for _, s in rows)
    if total_swaps < FAKE_VOLUME_MIN_TOTAL_SWAPS or total_vol <= 0:
        return None
    rows.sort(key=lambda x: x[1]["swaps"], reverse=True)
    top = rows[:FAKE_VOLUME_TOP_WALLETS]
    if len(top) < 3 or min(s["swaps"] for _, s in top) < FAKE_VOLUME_MIN_WALLET_SWAPS:
        return None
    top_swaps = sum(s["swaps"] for _, s in top)
    top_vol = sum(s["inflow"] + s["outflow"] for _, s in top)
    swap_share = top_swaps / total_swaps
    vol_share = top_vol / total_vol
    symmetry = []
    for _, s in top:
        hi = max(s["inflow"], s["outflow"])
        lo = min(s["inflow"], s["outflow"])
        symmetry.append((lo / hi) if hi > 0 else 0.0)
    med_sym = _median(symmetry)
    if swap_share < FAKE_VOLUME_MIN_SWAP_SHARE or vol_share < FAKE_VOLUME_MIN_VOLUME_SHARE or med_sym < FAKE_VOLUME_MIN_SYMMETRY:
        return None
    return (f"Recent pool-facing flow is concentrated: top {len(top)} wallets account for {_format_pct(swap_share)} of swaps "
            f"and {_format_pct(vol_share)} of volume, with near-symmetric in/out flow (median {med_sym:.2f}).")


def evm_holder_pattern_flags(rpc, token, total_supply, unit, market, logs, replay_bal, pools, launch_block_time_s):
    flags = []
    cant = []

    mc = fake_market_cap_flag(market)
    if mc:
        flags.append(mc)

    if logs and replay_bal and total_supply > 0:
        excluded = {token.lower(), "0x" + "0" * 40, "0x000000000000000000000000000000000000dead"}
        excluded.update(p.lower() for p in (pools or []))
        holders = [(a, (b / unit) / total_supply) for a, b in replay_bal.items() if b > 0 and a not in excluded]
        holders = sorted(holders, key=lambda x: x[1], reverse=True)
        holders = holders[:400]
        scan = [(a, s) for a, s in holders if s >= (WALLET_FARM_MIN_SLICE_SHARE * 0.5)]
        if scan:
            codes = rpc.batch([("eth_getCode", [a, "latest"]) for a, _ in scan])
            people = [(a, s) for (a, s), c in zip(scan, codes) if c in (None, "0x") or str(c).startswith("0xef0100")]
            group = _equal_slice_group(people)
            if group:
                tx_probe = group["wallets"][:WALLET_FARM_TX_DETAIL_SAMPLE]
                tx_counts_hex = rpc.batch([("eth_getTransactionCount", [w, "latest"]) for w in tx_probe])
                tx_count_by_wallet = {w: int(x or "0x0", 16) for w, x in zip(tx_probe, tx_counts_hex)}
                targets = set(group["wallets"][:WALLET_FARM_SAMPLE])
                first_receive = {}
                for l in logs:
                    ts = l.get("topics") or []
                    if len(ts) < 3:
                        continue
                    to = "0x" + ts[2][26:]
                    if to in targets and to not in first_receive:
                        first_receive[to] = int(l["blockNumber"], 16)
                        if len(first_receive) == len(targets):
                            break
                sampled = [w for w in group["wallets"][:WALLET_FARM_SAMPLE] if w in first_receive]
                funders = _scan_evm_funders_one_hop(rpc, first_receive, sampled, launch_block_time_s or 2.0)
                flags.append(wallet_farm_flag(group, sampled, funders, tx_count_by_wallet))

        stats = _evm_pool_flow_stats(logs, pools, unit)
        if stats:
            active = sorted(stats.items(), key=lambda x: x[1]["swaps"], reverse=True)[:80]
            if active:
                codes = rpc.batch([("eth_getCode", [w, "latest"]) for w, _ in active])
                person_stats = {w: s for (w, s), c in zip(active, codes) if c in (None, "0x") or str(c).startswith("0xef0100")}
                vol_flag = fake_volume_flag_from_stats(person_stats)
                if vol_flag:
                    flags.append(vol_flag)
    else:
        cant.append("Wallet-farm and fake-volume checks need a full holder replay; this coin exceeded the transfer replay cap on this RPC. "
                    "Set PUBLICNODE_TOKEN (free at allnodes.com/publicnode) for deeper history on supported EVM chains.")
    return flags, cant


def _sol_owner_deltas(tx, mint):
    out = collections.Counter()
    meta = (tx or {}).get("meta") or {}
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(key) or []:
            if b.get("mint") == mint and b.get("owner"):
                out[b["owner"]] += sign * float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    return out


def _solana_holders(call, mint):
    owner_bal = collections.Counter()
    t0 = time.monotonic()
    for program in SOLANA_TOKEN_PROGRAMS:
        if time.monotonic() - t0 > SOLANA_HOLDER_READ_BUDGET_S:
            raise RuntimeError("solana holder scan exceeded time budget")
        # Token-2022 accounts carry extensions (170+ bytes), so only classic SPL accounts have a fixed size
        filters = [{"memcmp": {"offset": 0, "bytes": mint}}]
        if program == SOLANA_TOKEN_PROGRAMS[0]:
            filters.insert(0, {"dataSize": 165})
        rows = call("getProgramAccounts", [program, {"encoding": "jsonParsed", "filters": filters}])
        if time.monotonic() - t0 > SOLANA_HOLDER_READ_BUDGET_S:
            raise RuntimeError("solana holder scan exceeded time budget")
        for row in rows or []:
            info = (((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            owner = info.get("owner")
            amt = float((((info.get("tokenAmount") or {}).get("uiAmount") or 0.0)))
            if owner and amt > 0:
                owner_bal[owner] += amt
    return owner_bal


def _solana_wallet_pages(call, wallets):
    pages = {}
    for w in wallets:
        pages[w] = call("getSignaturesForAddress", [w, {"limit": 1000}]) or []
    return pages


def _sol_fee_payer(tx):
    keys = ((tx or {}).get("transaction") or {}).get("message", {}).get("accountKeys") or []
    if not keys:
        return ""
    first = keys[0]
    return first["pubkey"] if isinstance(first, dict) else first


def _solana_one_hop_funders_from_pages(call, pages):
    out = {}
    for w, page in pages.items():
        if not page or len(page) >= 1000:
            continue
        oldest = page[-1].get("signature")
        if not oldest:
            continue
        tx = call("getTransaction", [oldest, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        payer = _sol_fee_payer(tx)
        if payer and payer != w:
            out[w] = payer
    return out


def _solana_fake_volume_flag(call, mint, kind_lookup):
    sigs = call("getSignaturesForAddress", [mint, {"limit": 220}]) or []
    sigs = [s["signature"] for s in sigs if not s.get("err")]
    if len(sigs) < FAKE_VOLUME_MIN_TOTAL_SWAPS:
        return None
    sigs = sigs[:140]
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        txs = list(ex.map(lambda s: call("getTransaction", [s, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]), sigs))
    owners = sorted({o for tx in txs for o in _sol_owner_deltas(tx, mint)})
    kinds = kind_lookup(owners) if owners else {}
    stats = collections.defaultdict(lambda: {"swaps": 0, "inflow": 0.0, "outflow": 0.0})
    for tx in txs:
        for owner, delta in _sol_owner_deltas(tx, mint).items():
            if kinds.get(owner) != "person" or delta == 0:
                continue
            s = stats[owner]
            s["swaps"] += 1
            if delta > 0:
                s["inflow"] += delta
            else:
                s["outflow"] += -delta
    return fake_volume_flag_from_stats(stats)


def solana_holder_pattern_flags(call, mint, total_supply, market, excluded_wallets, kind_lookup):
    flags = []
    cant = []

    mc = fake_market_cap_flag(market)
    if mc:
        flags.append(mc)

    rpc_errors = (RuntimeError, OSError, urllib.error.HTTPError, urllib.error.URLError)
    try:
        balances = _solana_holders(call, mint)
    except rpc_errors as e:
        cant.append("Wallet-farm check on Solana couldn't read the full holder list from RPC "
                    f"({type(e).__name__}); set HELIUS_API_KEY (free at helius.dev).")
        balances = None
    if balances and total_supply > 0:
        excluded = set(excluded_wallets or [])
        excluded.update({SOLANA_SYSTEM, SOLANA_BURN})
        rows = [(w, amt / total_supply) for w, amt in balances.items() if w not in excluded and amt > 0]
        rows = sorted(rows, key=lambda x: x[1], reverse=True)
        rows = rows[:500]
        rows = [(w, s) for w, s in rows if s >= (WALLET_FARM_MIN_SLICE_SHARE * 0.5)]
        if rows:
            kinds = kind_lookup([w for w, _ in rows])
            people = [(w, s) for w, s in rows if kinds.get(w) == "person"]
            group = _equal_slice_group(people)
            if group:
                sampled = group["wallets"][:WALLET_FARM_SAMPLE]
                try:
                    pages = _solana_wallet_pages(call, sampled)
                    tx_counts = {w: len(p) for w, p in pages.items()}
                    funders = _solana_one_hop_funders_from_pages(call, pages)
                except rpc_errors as e:
                    tx_counts = {}
                    funders = {}
                    cant.append("Wallet-farm one-hop sampling on Solana hit RPC limits "
                                f"({type(e).__name__}); set HELIUS_API_KEY (free at helius.dev).")
                flags.append(wallet_farm_flag(group, sampled, funders, tx_counts))

    try:
        vflag = _solana_fake_volume_flag(call, mint, kind_lookup)
        if vflag:
            flags.append(vflag)
    except rpc_errors as e:
        cant.append(f"Fake-volume check on Solana hit RPC limits ({type(e).__name__}); set HELIUS_API_KEY (free at helius.dev).")
    return flags, cant
