"""Copycat ticker/name checks using DexScreener search."""

import urllib.parse
import urllib.error


COPYCAT_MIN_FDV_MULTIPLIER = 3.0
COPYCAT_MIN_DAYS_OLDER = 7
LOOKALIKE_HEAD = 6
LOOKALIKE_TAIL = 4


def _norm_chain(chain):
    return (chain or "").strip().lower()


def _is_evm(addr):
    return isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42


def _looks_like(a, b):
    if not (_is_evm(a) and _is_evm(b)):
        return False
    a_hex = a[2:].lower()
    b_hex = b[2:].lower()
    head_exact = a_hex[:LOOKALIKE_HEAD] == b_hex[:LOOKALIKE_HEAD]
    head_shifted = a_hex[1:4] == b_hex[1:4]
    return (head_exact or head_shifted) and a_hex[-LOOKALIKE_TAIL:] == b_hex[-LOOKALIKE_TAIL:]


def _pretty_chain(chain):
    c = _norm_chain(chain)
    names = {
        "eth": "Ethereum",
        "ethereum": "Ethereum",
        "bsc": "BNB Chain",
        "base": "Base",
        "solana": "Solana",
        "robinhood": "Robinhood Chain",
        "hyperevm": "HyperEVM",
        "elysium": "Elysium",
    }
    return names.get(c, chain)


def _token_rows(search_data, symbol):
    out = {}
    for p in (search_data or {}).get("pairs") or []:
        base = p.get("baseToken") or {}
        addr = (base.get("address") or "").strip()
        if not addr:
            continue
        sym = (base.get("symbol") or "").strip()
        name = (base.get("name") or sym).strip()
        if symbol and sym.upper() != symbol.upper():
            continue
        chain = _norm_chain(p.get("chainId") or "")
        key = (chain, addr.lower() if _is_evm(addr) else addr)
        fdv = float(p.get("fdv") or 0.0)
        ts = p.get("pairCreatedAt")
        ts = float(ts) / 1000.0 if ts else None
        row = out.get(key)
        if not row:
            out[key] = {"chain": chain, "address": addr, "symbol": sym, "name": name, "fdv": fdv, "first_ts": ts}
            continue
        row["fdv"] = max(row["fdv"], fdv)
        if ts is not None and (row["first_ts"] is None or ts < row["first_ts"]):
            row["first_ts"] = ts
    return list(out.values())


def find_copycat_from_search(current_addr, current_chain, market, search_data):
    symbol = (market or {}).get("symbol") or ""
    if not symbol:
        return None
    rows = _token_rows(search_data, symbol)
    if not rows:
        return None
    now_chain = _norm_chain(current_chain)
    now_addr = current_addr.lower() if _is_evm(current_addr) else current_addr
    now_fdv = float((market or {}).get("dex_fdv") or 0.0)
    now_ts = (market or {}).get("first_pair_ts")

    cands = []
    for r in rows:
        addr_norm = r["address"].lower() if _is_evm(r["address"]) else r["address"]
        if r["chain"] == now_chain and addr_norm == now_addr:
            continue
        if now_fdv > 0 and r["fdv"] < now_fdv * COPYCAT_MIN_FDV_MULTIPLIER:
            continue
        if now_ts and r["first_ts"]:
            older_days = int((now_ts - r["first_ts"]) / 86400)
            if older_days < COPYCAT_MIN_DAYS_OLDER:
                continue
        cands.append(r)
    if not cands:
        return None

    def score(x):
        older = int((now_ts - x["first_ts"]) / 86400) if now_ts and x["first_ts"] else 0
        return (x["fdv"], older, x["name"])

    base = max(cands, key=score)
    age_text = (f"{int((now_ts - base['first_ts']) / 86400)} days older"
                if now_ts and base["first_ts"] else "age unknown")
    flag = (f"Same ticker as {base['name']} on {_pretty_chain(base['chain'])} "
            f"({base['address']}, ${base['fdv']:,.0f} FDV, {age_text}).")

    same_chain = [r for r in rows if r["chain"] == base["chain"] and r["address"] != base["address"]]
    lookalike = next((r for r in same_chain if _looks_like(r["address"], base["address"])), None)
    if lookalike:
        flag = (flag[:-1] + f" Same-chain look-alike address {lookalike['address']} shares the first "
                f"{LOOKALIKE_HEAD} and last {LOOKALIKE_TAIL} characters with {base['address']}.")
    return flag


def find_copycat_flag(current_addr, current_chain, market, http_json):
    symbol = (market or {}).get("symbol") or ""
    if not symbol:
        return None, None
    url = "https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(symbol)
    try:
        data = http_json(url)
    except (RuntimeError, OSError, urllib.error.HTTPError, urllib.error.URLError) as e:
        return None, f"Copycat name/ticker check couldn't query DexScreener right now ({type(e).__name__})."
    return find_copycat_from_search(current_addr, current_chain, market, data), None
