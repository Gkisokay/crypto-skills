"""Solana liquidity-pull and insider-exit helpers for launch-check."""

import collections


SOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_FUN_CURVE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
METEORA_DAMM_V2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
METEORA_DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


def _sig(tx):
    sigs = ((tx or {}).get("transaction") or {}).get("signatures") or []
    return sigs[0] if sigs else ""


def _payer(tx):
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not keys:
        return ""
    k = keys[0]
    return k.get("pubkey") if isinstance(k, dict) else k


def _time(tx):
    return (tx or {}).get("blockTime") or 0


def _token_balances(tx, owner, mint):
    meta = (tx or {}).get("meta") or {}
    pre = 0.0
    post = 0.0
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") == owner and b.get("mint") == mint:
            pre += float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == owner and b.get("mint") == mint:
            post += float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
    return pre, post


def _account_index(tx, addr):
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    for i, k in enumerate(keys):
        pub = k.get("pubkey") if isinstance(k, dict) else k
        if pub == addr:
            return i
    return None


def _account_keys(tx):
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    out = []
    for k in keys:
        out.append(k.get("pubkey") if isinstance(k, dict) else k)
    return out


def _program_id(ix, keys):
    if "programId" in ix:
        return ix["programId"]
    idx = ix.get("programIdIndex")
    if idx is not None and 0 <= idx < len(keys):
        return keys[idx]
    return None


def _program_ids(tx):
    keys = _account_keys(tx)
    out = set()
    msg = (((tx or {}).get("transaction") or {}).get("message") or {})
    for ix in msg.get("instructions") or []:
        pid = _program_id(ix, keys)
        if pid:
            out.add(pid)
    meta = (tx or {}).get("meta") or {}
    for inner in meta.get("innerInstructions") or []:
        for ix in inner.get("instructions") or []:
            pid = _program_id(ix, keys)
            if pid:
                out.add(pid)
    return out


def detect_amm(txs):
    for tx in txs or []:
        pids = _program_ids(tx)
        if METEORA_DAMM_V2 in pids or METEORA_DLMM in pids:
            return "meteora"
        if PUMPSWAP_AMM in pids:
            return "pumpswap"
        if RAYDIUM_CPMM in pids or RAYDIUM_V4 in pids:
            return "raydium"
        if PUMP_FUN_CURVE in pids:
            return "pumpfun"
    return "unknown"


def _owner_deltas(tx, mint):
    d = collections.Counter()
    meta = (tx or {}).get("meta") or {}
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(key) or []:
            if b.get("owner") and b.get("mint") == mint:
                d[b["owner"]] += sign * float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
    return d


def _best_quote_mint(mint, pool, txs):
    peaks = {}
    for tx in txs:
        meta = (tx or {}).get("meta") or {}
        for key in ("preTokenBalances", "postTokenBalances"):
            for b in meta.get(key) or []:
                if b.get("owner") != pool:
                    continue
                m = b.get("mint")
                if not m or m == mint:
                    continue
                amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
                peaks[m] = max(peaks.get(m, 0.0), amt)
    if not peaks:
        return None
    return max(peaks, key=lambda m: peaks[m])


def _current_quote_balance(call, pool, quote_mint):
    if quote_mint == SOL_MINT:
        return float((call("getBalance", [pool]).get("value") or 0) / 1_000_000_000.0)
    owned = call("getTokenAccountsByOwner", [pool, {"mint": quote_mint}]).get("value") or []
    if not owned:
        return 0.0
    total = 0.0
    for row in owned:
        pub = row.get("pubkey")
        if not pub:
            continue
        bal = call("getTokenAccountBalance", [pub]).get("value") or {}
        total += float(bal.get("uiAmount") or 0.0)
    return total


def _is_remove_instruction(ix):
    parsed = ix.get("parsed")
    typ = (parsed.get("type") if isinstance(parsed, dict) else "") or ""
    low = typ.lower()
    return any(k in low for k in ("remove", "withdraw", "decrease", "burn"))


def _is_removal_tx(tx, amm, mint, quote_mint, pool):
    keys = _account_keys(tx)
    d_base = _owner_deltas(tx, mint)
    d_quote = _owner_deltas(tx, quote_mint)
    if d_base.get(pool, 0.0) >= 0 or d_quote.get(pool, 0.0) >= 0:
        receiver = ""
    else:
        receivers = []
        for w in set(d_base) | set(d_quote):
            if w == pool:
                continue
            if d_base.get(w, 0.0) > 0 and d_quote.get(w, 0.0) > 0:
                receivers.append((min(d_base[w], d_quote[w]), w))
        receiver = ""
        if receivers:
            receivers.sort(reverse=True)
            receiver = receivers[0][1]
            return True, receiver
    amm_pids = {
        "pumpswap": {PUMPSWAP_AMM},
        "raydium": {RAYDIUM_CPMM, RAYDIUM_V4},
        "meteora": {METEORA_DAMM_V2, METEORA_DLMM},
    }.get(amm, set())
    msg = (((tx or {}).get("transaction") or {}).get("message") or {})
    for ix in msg.get("instructions") or []:
        pid = _program_id(ix, keys)
        if pid in amm_pids and _is_remove_instruction(ix):
            return True, receiver or _payer(tx)
    meta = (tx or {}).get("meta") or {}
    for inner in meta.get("innerInstructions") or []:
        for ix in inner.get("instructions") or []:
            pid = _program_id(ix, keys)
            if pid in amm_pids and _is_remove_instruction(ix):
                return True, receiver or _payer(tx)
    return False, ""


def pool_pull_flag(call, mint, pool, launch_txs, max_recent=10):
    sigs = call("getSignaturesForAddress", [pool, {"limit": 100}]) or []
    chosen = [s for s in sigs if not s.get("err")][:max_recent]
    recent = []
    for s in chosen:
        tx = call("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if tx:
            recent.append(tx)
    txs = sorted((launch_txs or []) + recent, key=_time)
    amm = detect_amm(txs)
    quote_mint = _best_quote_mint(mint, pool, txs)
    if not quote_mint:
        return None
    peak = 0.0
    biggest = (0.0, None)  # delta, tx
    for tx in txs:
        pre, post = _token_balances(tx, pool, quote_mint)
        if pre == 0.0 and post == 0.0:
            continue
        peak = max(peak, pre, post)
        delta = post - pre
        if delta < biggest[0]:
            biggest = (delta, tx)
    if peak <= 0 or biggest[1] is None:
        return None
    now = _current_quote_balance(call, pool, quote_mint)
    if now > peak * 0.05:
        return None
    is_removal, who = _is_removal_tx(biggest[1], amm, mint, quote_mint, pool)
    if not is_removal:
        return None
    row = {
        "pool": pool,
        "quote_mint": quote_mint,
        "quote_symbol": "SOL" if quote_mint == SOL_MINT else quote_mint,
        "quote_peak": peak,
        "quote_now": now,
        "drain_tx": _sig(biggest[1]),
        "drainer": who or _payer(biggest[1]),
    }
    return {"text": format_liquidity_pull(row), "pool": pool}


def _fmt_amount(x):
    x = float(x)
    if x >= 1000:
        return f"{x:,.1f}".rstrip("0").rstrip(".")
    if x >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.4g}"


def format_liquidity_pull(pool):
    sym = pool.get("quote_symbol") or pool.get("quote_mint") or "quote token"
    return (f"Liquidity pulled: pool {pool['pool']} quote reserve dropped from {_fmt_amount(pool['quote_peak'])} {sym} "
            f"to {_fmt_amount(pool['quote_now'])} {sym}; last large removal tx {pool.get('drain_tx') or 'unknown'} "
            f"by {pool.get('drainer') or 'unknown'}.")


def _wallet_token_delta(tx, wallet, mint):
    pre, post = _token_balances(tx, wallet, mint)
    return post - pre


def _wallet_sol_gain(tx, wallet):
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    idx = None
    for i, key in enumerate(keys):
        pub = key.get("pubkey") if isinstance(key, dict) else key
        if pub == wallet:
            idx = i
            break
    if idx is None:
        return 0.0
    meta = (tx or {}).get("meta") or {}
    pre = (meta.get("preBalances") or [])
    post = (meta.get("postBalances") or [])
    if idx >= len(pre) or idx >= len(post):
        return 0.0
    return max((post[idx] - pre[idx]) / 1_000_000_000.0, 0.0)


def _wallet_outflow_kind(tx, wallet, mint, pools):
    d = _wallet_token_delta(tx, wallet, mint)
    if d >= 0:
        return 0.0, ""
    out = -d
    pool_hits = _owner_deltas(tx, mint)
    sold = any(pool_hits.get(p, 0.0) > 0 for p in pools)
    return out, "sold" if sold else "moved"


def insider_exit_summary(call, mint, total_supply, launch_ts, creator, took_by_wallet, tied_wallets, pools, seen_txs):
    lines = []
    pool_targets = set(pools or [])
    pool_targets.add(PUMP_FUN_CURVE)
    if creator:
        sigs = call("getSignaturesForAddress", [creator, {"limit": 100}]) or []
        txs = []
        for s in sigs:
            if s.get("err") or (s.get("blockTime") or 0) < launch_ts:
                continue
            tx = call("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
            if tx:
                txs.append(tx)
        txs.sort(key=_time)
        sold = moved = 0.0
        sells = moves = 0
        first_sell_ts = None
        first_sell_sig = ""
        sol = 0.0
        for tx in txs:
            amt, kind = _wallet_outflow_kind(tx, creator, mint, pool_targets)
            if amt <= 0:
                continue
            if kind == "sold":
                sold += amt
                sells += 1
                sol += _wallet_sol_gain(tx, creator)
                if first_sell_ts is None:
                    first_sell_ts = _time(tx)
                    first_sell_sig = _sig(tx)
            else:
                moved += amt
                moves += 1
        took = float((took_by_wallet or {}).get(creator) or 0.0)
        if sells > 0:
            first_gap = int(max((first_sell_ts or launch_ts) - launch_ts, 0))
            lines.append(f"Creator {creator} took {took / total_supply:.2%}, sold {sold / total_supply:.2%} in {sells} sells "
                         f"for ~{sol:.1f} SOL, first sell {first_gap} s after launch, tx {first_sell_sig}.")
        elif moves > 0:
            lines.append(f"Creator {creator} took {took / total_supply:.2%}, moved {moved / total_supply:.2%} in {moves} transfers after launch.")
    tied = sorted(set(tied_wallets or []) - ({creator} if creator else set()))
    if tied:
        sold = moved = 0.0
        for tx in seen_txs or []:
            if _time(tx) < launch_ts:
                continue
            for w in tied:
                amt, kind = _wallet_outflow_kind(tx, w, mint, pool_targets)
                if kind == "sold":
                    sold += amt
                elif kind == "moved":
                    moved += amt
        took = sum(float((took_by_wallet or {}).get(w) or 0.0) for w in tied)
        if sold > 0:
            lines.append(f"{len(tied)} tied wallets took {took / total_supply:.2%}, have sold {sold / total_supply:.2%} so far.")
        elif moved > 0:
            lines.append(f"{len(tied)} tied wallets took {took / total_supply:.2%}, have moved {moved / total_supply:.2%} so far.")
        else:
            lines.append(f"{len(tied)} tied wallets took {took / total_supply:.2%}, have sold 0.00% so far.")
    return lines


def launch_insiders(creator, bundle_wallets, clusters):
    """Creator plus first-minutes wallets tied to it via creator-funded cluster or launch bundle."""
    tied = set(bundle_wallets or [])
    for c in clusters or []:
        if c.get("funder") == creator:
            tied.update(c.get("wallets") or [])
    if creator:
        tied.add(creator)
    return tied


def apply_solana_checks(call, mint, pools, creator, launch_ts, total_supply, took_by_wallet, bundle_wallets, clusters,
                        launch_txs, pair_liquidity=None):
    """Return extra Solana pull + insider findings (LP control intentionally not checked yet)."""
    flags, notes, cant = [], [], []
    ranked = sorted(set(pools or []), key=lambda p: float((pair_liquidity or {}).get(p, 0.0)), reverse=True)[:3]
    for pool in ranked:
        try:
            pulled = pool_pull_flag(call, mint, pool, launch_txs, max_recent=10)
        except RuntimeError:
            pulled = None
            notes.append(f"Couldn't check pull history for pool {pool}: recent pool transactions couldn't be read from this RPC.")
        if pulled:
            flags.append(pulled["text"])
    insiders = launch_insiders(creator, bundle_wallets, clusters)
    try:
        flags.extend(insider_exit_summary(call, mint, total_supply, launch_ts, creator, took_by_wallet,
                                          insiders - ({creator} if creator else set()), set(pools or []), launch_txs))
    except RuntimeError:
        notes.append("Couldn't check creator post-launch exits: creator transaction history couldn't be read from this RPC.")
    return {"flags": flags, "notes": notes, "cant_check": cant}
