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
    owned = call("getTokenAccountsByOwner", [pool, {"mint": quote_mint}, {"encoding": "jsonParsed"}]).get("value") or []
    total = 0.0
    for row in owned:
        info = ((((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
        total += float((info.get("tokenAmount") or {}).get("uiAmount") or 0.0)
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


def apply_solana_checks(call, mint, pools, creator, launch_ts, total_supply, took_by_wallet, bundle_wallets, clusters,
                        launch_txs, pair_liquidity=None):
    """Return Solana pull findings (LP control intentionally not checked yet)."""
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
    return {"flags": flags, "notes": notes, "cant_check": cant}
