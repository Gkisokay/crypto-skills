"""Solana liquidity-pull and insider-exit helpers for launch-check."""

import collections
import functools
import hashlib


SOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_FUN_CURVE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
METEORA_DAMM_V2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
METEORA_DAMM_V2_AUTHORITY = "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC"  # pool_authority PDA: owns every DAMM v2 pool vault
METEORA_DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
# Meteora's instructions aren't parsed by RPCs, so removals are matched on their Anchor discriminators (DAMM v2 and DLMM)
METEORA_REMOVES = {hashlib.sha256(f"global:{name}".encode()).digest()[:8] for name in (
    "remove_liquidity", "remove_all_liquidity", "remove_liquidity_by_range", "remove_liquidity2",
    "remove_liquidity_by_range2")}
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


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


@functools.lru_cache(maxsize=4096)
def _damm_v2_vault(mint, pool):
    import solana_check  # imports this module, so it can't be imported at the top
    return solana_check.pda([b"token_vault", solana_check.b58decode(mint), solana_check.b58decode(pool)], METEORA_DAMM_V2)


def _balances(tx, key, pool):
    """Token balance rows of one side (pre or post); a DAMM v2 vault of `pool` reads as owned by `pool`, since all
    DAMM v2 vaults share one owner."""
    keys = _account_keys(tx)
    out = []
    for b in ((tx or {}).get("meta") or {}).get(key) or []:
        owner = b.get("owner")
        if owner == METEORA_DAMM_V2_AUTHORITY and b.get("mint"):
            i = b.get("accountIndex")
            if i is not None and 0 <= i < len(keys) and keys[i] == _damm_v2_vault(b["mint"], pool):
                owner = pool
        out.append((owner, b.get("mint"), float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)))
    return out


def _reserve(tx, pool, mint):
    """(pre, post) of the pool's reserve of `mint` in this tx, or None when the tx doesn't touch that reserve."""
    seen = False
    sides = []
    for key in ("preTokenBalances", "postTokenBalances"):
        total = 0.0
        for owner, m, amt in _balances(tx, key, pool):
            if owner == pool and m == mint:
                seen = True
                total += amt
        sides.append(total)
    return tuple(sides) if seen else None


def _token_balances(tx, owner, mint):
    return _reserve(tx, owner, mint) or (0.0, 0.0)


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


def _owner_deltas(tx, mint, pool):
    d = collections.Counter()
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for owner, m, amt in _balances(tx, key, pool):
            if owner and m == mint:
                d[owner] += sign * amt
    return d


def _best_quote_mint(mint, pool, txs):
    peaks = {}
    for tx in txs:
        for key in ("preTokenBalances", "postTokenBalances"):
            for owner, m, amt in _balances(tx, key, pool):
                if owner != pool or not m or m == mint:
                    continue
                peaks[m] = max(peaks.get(m, 0.0), amt)
    if not peaks:
        return None
    return max(peaks, key=lambda m: peaks[m])


def _current_quote_balance(call, pool, quote_mint, txs):
    for tx in txs:  # a DAMM v2 pool owns no token accounts: read its quote vault
        keys = _account_keys(tx)
        for b in ((tx or {}).get("meta") or {}).get("postTokenBalances") or []:
            i = b.get("accountIndex")
            if (b.get("owner") == METEORA_DAMM_V2_AUTHORITY and b.get("mint") == quote_mint and i is not None
                    and 0 <= i < len(keys) and keys[i] == _damm_v2_vault(quote_mint, pool)):
                return float((call("getTokenAccountBalance", [keys[i]]).get("value") or {}).get("uiAmount") or 0.0)
    owned = call("getTokenAccountsByOwner", [pool, {"mint": quote_mint}, {"encoding": "jsonParsed"}]).get("value") or []
    total = 0.0
    for row in owned:
        info = ((((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
        total += float((info.get("tokenAmount") or {}).get("uiAmount") or 0.0)
    return total


def _discriminator(data):
    data = data or ""
    n = 0
    for ch in data:
        if ch not in B58:
            return b""
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return (b"\0" * (len(data) - len(data.lstrip("1"))) + raw)[:8]


def _is_remove_instruction(ix):
    parsed = ix.get("parsed")
    typ = (parsed.get("type") if isinstance(parsed, dict) else "") or ""
    low = typ.lower()
    if any(k in low for k in ("remove", "withdraw", "decrease", "burn")):
        return True
    return not parsed and _discriminator(ix.get("data")) in METEORA_REMOVES


def _is_removal_tx(tx, amm, mint, quote_mint, pool):
    keys = _account_keys(tx)
    d_base = _owner_deltas(tx, mint, pool)
    d_quote = _owner_deltas(tx, quote_mint, pool)
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
    now = _current_quote_balance(call, pool, quote_mint, txs)
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
