"""Creator history and early-exit checks for launch-check.

All logic here is side-effect free except network reads through helpers passed from launch_check.py / solana_check.py.
"""
import collections
import time


MAX_HISTORY_CALLS = 25
MAX_HISTORY_SECONDS = 15
MAX_HISTORY_CHECKED = 10
MAX_HISTORY_LAUNCHES = 20  # enough to call a wallet a serial launcher; stop reading there
SERIAL_LAUNCHER_MIN = 3  # fewer other launches is a note, not a red flag
LOW_FDV_USD = 5_000
HISTORY_WINDOW_DAYS = 30
MAX_EXIT_RECEIPTS = 5
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _norm(chain, addr):
    if not addr:
        return ""
    return addr if chain == "solana" else addr.lower()


def _to_float(x):
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _tx_sig(tx):
    if tx.get("signature"):
        return tx["signature"]
    msg = (tx.get("transaction") or {})
    sigs = msg.get("signatures") or []
    return sigs[0] if sigs else ""


def _owner_deltas(tx, mint):
    d = collections.Counter()
    meta = (tx or {}).get("meta") or {}
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(key) or []:
            if b.get("mint") == mint and b.get("owner"):
                d[b["owner"]] += sign * float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    return d


def _token_amount(row):
    ui = (row or {}).get("uiTokenAmount") or {}
    if ui.get("uiAmount") is not None:
        return float(ui.get("uiAmount") or 0)
    amt = ui.get("amount")
    dec = ui.get("decimals")
    if amt is None or dec is None:
        return 0.0
    try:
        return int(amt) / (10 ** int(dec))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _extract_created_mints(tx):
    out = set()
    if tx.get("createdMint"):
        out.add(tx["createdMint"])

    pump_candidates = set()

    def parse_instructions(items):
        for ins in items or []:
            if not isinstance(ins, dict):
                continue
            pid = (
                ins.get("programId")
                or (ins.get("programIdIndex") if isinstance(ins.get("programIdIndex"), str) else None)
                or ((ins.get("programId") or {}).get("pubkey") if isinstance(ins.get("programId"), dict) else None)
            )
            if pid == PUMP_PROGRAM:
                accs = ins.get("accounts") or []
                first = None
                if accs:
                    a0 = accs[0]
                    if isinstance(a0, dict):
                        first = a0.get("pubkey")
                    elif isinstance(a0, str):
                        first = a0
                if first:
                    pump_candidates.add(first)
            parsed = ins.get("parsed") or {}
            if not isinstance(parsed, dict):
                continue
            typ = (parsed.get("type") or "").lower()
            info = parsed.get("info") or {}
            if typ.startswith("initializemint") and isinstance(info, dict) and info.get("mint"):
                out.add(info["mint"])

    parse_instructions((tx.get("transaction") or {}).get("message", {}).get("instructions"))
    parse_instructions(tx.get("instructions"))
    for row in ((tx.get("meta") or {}).get("innerInstructions") or []):
        parse_instructions(row.get("instructions"))

    for tr in tx.get("tokenTransfers") or []:
        if not isinstance(tr, dict):
            continue
        if tr.get("mint") and not tr.get("fromUserAccount") and tr.get("toUserAccount"):
            out.add(tr["mint"])

    # Pump.fun create tx shape: top-level call to pump program, mint initialized in inner instructions.
    # If parsing missed the mint, use the pump instruction account[0] candidate and confirm from balances.
    if pump_candidates:
        pre = {(b.get("mint"), b.get("owner")): _token_amount(b) for b in ((tx.get("meta") or {}).get("preTokenBalances") or [])}
        post_rows = (tx.get("meta") or {}).get("postTokenBalances") or []
        for cand in pump_candidates:
            if any((b.get("mint") == cand) for b in post_rows):
                out.add(cand)

        # Fallback: post-only mint whose first holder looks like a bonding curve.
        by_mint = collections.defaultdict(list)
        for row in post_rows:
            m = row.get("mint")
            if m:
                by_mint[m].append(row)
        pre_mints = {b.get("mint") for b in ((tx.get("meta") or {}).get("preTokenBalances") or [])}
        for m, rows in by_mint.items():
            if m in pre_mints:
                continue
            rows = sorted(rows, key=lambda x: _token_amount(x), reverse=True)
            if not rows:
                continue
            owner = (rows[0].get("owner") or "").lower()
            if "curve" in owner:
                out.add(m)
    return out


def fetch_dex_metrics(cc, chain, addresses):
    if not addresses:
        return {}
    net = "solana" if chain == "solana" else (cc.CHAINS.get(chain, {}).get("gt") or chain)
    want = {_norm(chain, a): a for a in addresses}
    out = {}
    for i in range(0, len(addresses), 30):
        part = addresses[i:i + 30]
        url = f"https://api.dexscreener.com/tokens/v1/{net}/" + ",".join(part)
        page = cc.http_json(url)
        pairs = []
        if isinstance(page, list):
            pairs = page
        elif isinstance(page, dict):
            pairs = page.get("pairs") or []
        for p in pairs:
            base = _norm(chain, ((p.get("baseToken") or {}).get("address")))
            if base not in want:
                continue
            fdv = _to_float(p.get("fdv"))
            liq = _to_float(((p.get("liquidity") or {}).get("usd"))
                            if isinstance(p.get("liquidity"), dict) else None)
            row = out.setdefault(base, {"fdv": None, "liquidity": None})
            if fdv is not None:
                row["fdv"] = fdv if row["fdv"] is None else max(row["fdv"], fdv)
            if liq is not None:
                row["liquidity"] = liq if row["liquidity"] is None else max(row["liquidity"], liq)
    return out


def summarize_creator_history(creator, launch_ts, launches, metrics, chain, partial=False):
    if not launches:
        return None, None
    launches = sorted(launches, key=lambda x: x.get("ts") or 0, reverse=True)
    low = min((x.get("ts") or launch_ts) for x in launches)
    high = max((x.get("ts") or launch_ts) for x in launches)
    lo_txt = time.strftime("%Y-%m-%d", time.gmtime(low or launch_ts))
    hi_txt = time.strftime("%Y-%m-%d", time.gmtime(high or launch_ts))
    checked = launches[:MAX_HISTORY_CHECKED]
    under = unlisted = 0
    rows = []
    for x in checked:
        a = _norm(chain, x.get("address"))
        m = metrics.get(a) or {}
        fdv = m.get("fdv")
        liq = m.get("liquidity")
        if fdv is None:
            unlisted += 1
        elif fdv < LOW_FDV_USD:
            under += 1
        fdv_txt = "FDV n/a" if fdv is None else f"FDV ${fdv:,.0f}"
        liq_txt = "liq n/a" if liq is None else f"liq ${liq:,.0f}"
        rows.append(f"{x.get('address')} ({fdv_txt}, {liq_txt})")
    if len(launches) >= MAX_HISTORY_LAUNCHES:
        n = f"{MAX_HISTORY_LAUNCHES}+"
    elif partial:
        n = f"at least {len(launches)}"
    else:
        n = str(len(launches))
    coins = "coin" if len(launches) == 1 else "coins"
    span = lo_txt if lo_txt == hi_txt else f"{lo_txt} to {hi_txt}"
    text = (f"This creator wallet {creator} launched {n} other {coins} ({span}); of the {len(checked)} checked, "
            f"{under} are under $5K FDV now and {unlisted} have no DexScreener market.")
    note = "Creator history sample (newest first): " + "; ".join(rows)
    if len(launches) < SERIAL_LAUNCHER_MIN:
        return None, text + " " + note
    return text, note


def creator_exit_solana(cc, txs, mint, creator, launch_total, launch_ts, pool_owners):
    if not txs or not creator or launch_total <= 0:
        return None
    pool_owners = set(pool_owners or [])
    txs = sorted(txs, key=lambda x: (x.get("slot") or 0, x.get("blockTime") or launch_ts))
    create_tx = txs[0]
    create_delta = max(_owner_deltas(create_tx, mint).get(creator, 0.0), 0.0)
    if create_delta <= 0:
        return None
    sold = 0.0
    t0 = create_tx.get("blockTime") or launch_ts
    for tx in txs[1:]:
        deltas = _owner_deltas(tx, mint)
        d = deltas.get(creator, 0.0)
        if d >= 0:
            continue
        sold += -d
        if sold + 1e-9 < create_delta * 0.95:
            continue
        span = max(0, (tx.get("blockTime") or t0) - t0)
        pct = 100.0 * create_delta / launch_total
        sig = _tx_sig(tx)
        recv = sorted(((w, a) for w, a in deltas.items() if w != creator and a > 0), key=lambda x: x[1], reverse=True)
        dest = recv[0][0] if recv else ""
        if dest in pool_owners:
            return (f"The creator wallet {creator} sold {pct:.2f}% back to the curve/pool {dest} "
                    f"{cc.human_span(span)} later (tx {sig}).")
        return (f"The creator wallet {creator} moved {pct:.2f}% to {dest or 'another wallet'} "
                f"{cc.human_span(span)} later (tx {sig}).")
    return None


def _hex_int(x):
    if isinstance(x, int):
        return x
    if isinstance(x, str) and x.startswith("0x"):
        return int(x, 16)
    return int(x or 0)


def _is_lp_add_like_receipt(cc, receipt, creator, pool, token):
    logs = (receipt or {}).get("logs") or []
    creator_to_pool = 0
    for l in logs:
        topics = l.get("topics") or []
        if topics and topics[0] in (cc.MINT_V2, cc.MINT_V3) and (l.get("address") or "").lower() == pool:
            return True
        if len(topics) >= 3 and topics[0] == cc.TRANSFER:
            frm = "0x" + topics[1][26:].lower()
            to = "0x" + topics[2][26:].lower()
            if frm == creator and to == pool:
                creator_to_pool += 1
                if (l.get("address") or "").lower() != token:
                    return True
    return creator_to_pool >= 2


def creator_exit_evm(cc, rpc, logs, creator, create_tx_hash, total_supply, unit, launch_ts, token, pools):
    if not logs or not creator or not create_tx_hash or total_supply <= 0 or unit <= 0:
        return None, None, None
    creator = creator.lower()
    create_tx_hash = create_tx_hash.lower()
    token = token.lower()
    pools = {p.lower() for p in (pools or [])}
    rows = sorted(
        logs,
        key=lambda l: (
            _hex_int(l.get("blockNumber", 0)),
            _hex_int(l.get("transactionIndex", 0)),
            _hex_int(l.get("logIndex", 0)),
        ),
    )
    create_recv = 0.0
    for l in rows:
        topics = l.get("topics") or []
        if len(topics) < 3:
            continue
        txh = (l.get("transactionHash") or "").lower()
        if txh != create_tx_hash:
            continue
        to = "0x" + topics[2][26:].lower()
        if to == creator:
            create_recv += int(l.get("data", "0x0"), 16) / unit
    if create_recv <= 0:
        return None, None, None

    pool_tx_order = []
    pool_by_tx = {}
    seen_pool_txs = set()
    for l in rows:
        topics = l.get("topics") or []
        if len(topics) < 3 or topics[0] != cc.TRANSFER:
            continue
        frm = "0x" + topics[1][26:].lower()
        to = "0x" + topics[2][26:].lower()
        if frm != creator or to not in pools:
            continue
        txh = (l.get("transactionHash") or "").lower()
        if txh and txh not in seen_pool_txs:
            seen_pool_txs.add(txh)
            pool_tx_order.append(txh)
            pool_by_tx[txh] = to

    lp_add_txs = set()
    unknown_pool_txs = set(pool_tx_order)
    for txh in pool_tx_order[:MAX_EXIT_RECEIPTS]:
        rc = rpc.call("eth_getTransactionReceipt", [txh])
        if _is_lp_add_like_receipt(cc, rc, creator, pool_by_tx.get(txh, ""), token):
            lp_add_txs.add(txh)
        unknown_pool_txs.discard(txh)
    cant = None
    if len(pool_tx_order) > MAX_EXIT_RECEIPTS:
        cant = (
            f"Creator exit check reviewed {MAX_EXIT_RECEIPTS} pool transfer receipts; "
            f"{len(pool_tx_order) - MAX_EXIT_RECEIPTS} more pool transfers were not classified."
        )

    sold = 0.0
    moves = collections.Counter()
    move_tx = {}
    for l in rows:
        topics = l.get("topics") or []
        if len(topics) < 3 or topics[0] != cc.TRANSFER:
            continue
        frm = "0x" + topics[1][26:].lower()
        if frm != creator:
            continue
        to = "0x" + topics[2][26:].lower()
        amt = int(l.get("data", "0x0"), 16) / unit
        txh = (l.get("transactionHash") or "").lower()
        if to in pools:
            if txh in lp_add_txs or txh in unknown_pool_txs:
                continue
            sold += amt
            if sold + 1e-12 >= create_recv * 0.95:
                blk = _hex_int(l.get("blockNumber", 0))
                span = max(0, rpc.block_ts(blk) - launch_ts)
                pct = 100.0 * sold / total_supply
                sold_line = (
                    f"The creator wallet {creator} sold {pct:.2f}% back to the curve/pool {to} "
                    f"{cc.human_span(span)} later (tx {l.get('transactionHash')})."
                )
                move_line = None
                if moves:
                    to2, amt2 = max(moves.items(), key=lambda x: x[1])
                    blk2 = _hex_int((move_tx.get(to2) or {}).get("blockNumber", 0))
                    span2 = max(0, rpc.block_ts(blk2) - launch_ts)
                    pct2 = 100.0 * amt2 / total_supply
                    move_line = (
                        f"The creator wallet {creator} moved {pct2:.2f}% to {to2} "
                        f"{cc.human_span(span2)} later (tx {(move_tx.get(to2) or {}).get('transactionHash')})."
                    )
                return sold_line, move_line, cant
            continue
        moves[to] += amt
        move_tx[to] = l
    if moves:
        to2, amt2 = max(moves.items(), key=lambda x: x[1])
        blk2 = _hex_int((move_tx.get(to2) or {}).get("blockNumber", 0))
        span2 = max(0, rpc.block_ts(blk2) - launch_ts)
        pct2 = 100.0 * amt2 / total_supply
        move_line = (
            f"The creator wallet {creator} moved {pct2:.2f}% to {to2} "
            f"{cc.human_span(span2)} later (tx {(move_tx.get(to2) or {}).get('transactionHash')})."
        )
        return None, move_line, cant
    return None, None, cant


def _solana_history_launches(call, creator, current_mint, launch_ts, stop):
    launches = {}
    token = None
    calls = 0
    partial = None
    lo_ts = launch_ts - HISTORY_WINDOW_DAYS * 86400
    hi_ts = launch_ts + 86400  # newest first from a day after launch: the launches before it matter most
    while calls < MAX_HISTORY_CALLS and time.monotonic() < stop:
        opts = {
            "transactionDetails": "full",
            "sortOrder": "desc",
            "limit": 100,
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 1,
            "filters": {"blockTime": {"gte": lo_ts, "lte": hi_ts}},
        }
        if token:
            opts["paginationToken"] = token
        page = call("getTransactionsForAddress", [creator, opts]) or {}
        calls += 1
        data = page.get("data") or []
        if not data:
            token = None
            break
        old_reached = False
        for tx in data:
            ts = tx.get("blockTime") or 0
            if not ts:
                continue
            if ts < lo_ts:
                old_reached = True
                continue
            if ts > hi_ts:
                continue
            for mint in _extract_created_mints(tx):
                if mint != current_mint and mint not in launches:
                    launches[mint] = ts
        token = page.get("paginationToken")
        if old_reached or len(launches) >= MAX_HISTORY_LAUNCHES:
            token = None
        if not token:
            break
    if token:
        partial = f"Creator history reached the time/call budget after {calls} calls; checked {len(launches)} launches so far."
    return [{"address": a, "ts": ts} for a, ts in launches.items()], partial


def _evm_history_launches(cc, rpc, creator, current_token, launch_block, head, launch_ts, block_time_s, stop):
    if time.monotonic() >= stop:
        return [], "Creator history hit the time budget before EVM trace lookup."
    span_blocks = int(HISTORY_WINDOW_DAYS * 86400 / max(block_time_s or 1.0, 1.0))
    frm = max(0, launch_block - span_blocks)
    to = min(head, launch_block + span_blocks)
    params = {
        "fromBlock": hex(frm),
        "toBlock": hex(to),
        "fromAddress": [creator],
        "after": 0,
        "count": 300,
    }
    traces = rpc.call("trace_filter", [params])  # some public RPCs don't support this method
    addrs = []
    by_addr = {}
    for tr in traces or []:
        if (tr.get("type") or "").lower() != "create":
            continue
        res = tr.get("result") or {}
        addr = (res.get("address") or "").lower()
        if not addr or addr == current_token:
            continue
        if addr in by_addr:
            continue
        blk = _hex_int(tr.get("blockNumber", 0))
        by_addr[addr] = {"address": addr, "ts": rpc.block_ts(blk) if blk else launch_ts}
        addrs.append(addr)
    if not addrs:
        return [], None
    calls = [("eth_call", [{"to": a, "data": "0x18160ddd"}, "latest"]) for a in addrs]
    total = rpc.batch(calls)
    out = []
    for a, x in zip(addrs, total):
        if x in (None, "0x", "0x0"):
            continue
        out.append(by_addr[a])
    return out, None


def add_solana_creator_checks(cc, call, rpc_url, r, mint, creator, launch_ts, launch_total, txs, pool_owners=None):
    cant = []
    try:
        line = creator_exit_solana(cc, txs, mint, creator, launch_total, launch_ts, pool_owners or set())
        if line:
            r.setdefault("extra_flags", []).append(line)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        cant.append(f"Creator exit check couldn't finish: {type(e).__name__}: {str(e)[:120]}")

    deadline = time.monotonic() + MAX_HISTORY_SECONDS
    launches = []
    partial = None
    try:
        launches, partial = _solana_history_launches(call, creator, mint, launch_ts, deadline)
    except RuntimeError as e:
        if "helius" not in (rpc_url or "").lower():
            cant.append("Creator history on Solana wasn't checked with the public RPC; set HELIUS_API_KEY (free at helius.dev).")
        else:
            cant.append(f"Creator history check couldn't finish: {type(e).__name__}: {str(e)[:120]}")
        return cant

    if launches:
        checked = sorted(launches, key=lambda x: x["ts"], reverse=True)[:MAX_HISTORY_CHECKED]
        metrics = {}
        try:
            metrics = fetch_dex_metrics(cc, "solana", [x["address"] for x in checked])
        except (OSError, RuntimeError, ValueError, TypeError):
            r.setdefault("notes", []).append("Creator history found launches, but DexScreener metrics were unavailable this run.")
        flag, note = summarize_creator_history(creator, launch_ts, launches, metrics, "solana", bool(partial))
        if flag:
            r.setdefault("extra_flags", []).append(flag)
        if note:
            r.setdefault("notes", []).append(note)
    if partial:
        r.setdefault("notes", []).append(partial)
    return cant


def add_evm_creator_checks(cc, rpc, r, token, mint_tx_hash, t0, t0_ts, head, unit, total, logs):
    cant = []
    creator = (r.get("creator") or "").lower()
    if not creator or t0 is None:
        return cant
    try:
        use_logs = logs
        if use_logs is None:
            use_logs, _ = rpc.logs({"address": token, "topics": [cc.TRANSFER]}, t0, min(head, t0 + 10_000), cap=5_000)
        market = r.get("market") or {}
        pools = set((market.get("pairs") or []))
        sold_line, move_line, receipt_cant = creator_exit_evm(
            cc, rpc, use_logs, creator, mint_tx_hash, total, unit, t0_ts, token, pools
        )
        if sold_line:
            r.setdefault("extra_flags", []).append(sold_line)
        if move_line:
            r.setdefault("extra_flags", []).append(move_line)
        if receipt_cant:
            cant.append(receipt_cant)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        cant.append(f"Creator exit check couldn't finish: {type(e).__name__}: {str(e)[:120]}")

    deadline = time.monotonic() + MAX_HISTORY_SECONDS
    launches, partial = [], None
    try:
        launches, partial = _evm_history_launches(
            cc,
            rpc,
            creator,
            token,
            t0,
            head,
            t0_ts,
            (r.get("launch") or {}).get("block_time_s") or 1.0,
            deadline,
        )
    except RuntimeError as e:
        if "trace_filter" in str(e) and ("-32601" in str(e) or "not available" in str(e) or "does not exist" in str(e)):
            cant.append("Creator history on this EVM chain wasn't checked because this RPC has no trace API; "
                        "set RPC_<CHAIN> to a tracing endpoint.")
        else:
            cant.append(f"Creator history check couldn't finish: {type(e).__name__}: {str(e)[:120]}")
    if launches:
        checked = sorted(launches, key=lambda x: x["ts"], reverse=True)[:MAX_HISTORY_CHECKED]
        metrics = {}
        try:
            metrics = fetch_dex_metrics(cc, r.get("chain"), [x["address"] for x in checked])
        except (OSError, RuntimeError, ValueError, TypeError):
            r.setdefault("notes", []).append("Creator history found launches, but DexScreener metrics were unavailable this run.")
        flag, note = summarize_creator_history(creator, t0_ts, launches, metrics, r.get("chain"))
        if flag:
            r.setdefault("extra_flags", []).append(flag)
        if note:
            r.setdefault("notes", []).append(note)
    if partial:
        r.setdefault("notes", []).append(partial)
    return cant
