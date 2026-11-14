"""EVM contract-trap checks: proxy implementation, risky selectors, owner state, and sell simulation."""

ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
PROBE = "0x1111111111111111111111111111111111111111"
MASK64 = (1 << 64) - 1

EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
EIP1822_IMPL_SLOT = "0xc5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876e6f6f8d54"

ROUTER_BY_CHAIN = {
    "ethereum": "0x7a250d5630b4cf539739df2c5dacab4c659f2488",
    "base": "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
    "bsc": "0x10ed43c718714eb63d5aa57b78b54704e256024e",
}
WRAPPED_BY_CHAIN = {
    "ethereum": "0xc02aa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "base": "0x4200000000000000000000000000000000000006",
    "bsc": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
}
FACTORY_TO_ROUTER = {
    "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f": ROUTER_BY_CHAIN["ethereum"],
    "0xca143ce32fe78f1f7019d7d551a6402fc5350c73": ROUTER_BY_CHAIN["bsc"],
    "0x8909dc15e40173ff4699343b6eb8132c65e18ec6": ROUTER_BY_CHAIN["base"],
}

RHO = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

MINT_SIGS = ("mint(address,uint256)", "mint(uint256)")
BLACKLIST_SIGS = (
    "blacklist(address)",
    "addBlackList(address)",
    "removeBlackList(address)",
    "isBlacklisted(address)",
    "isBot(address)",
    "setBots(address[],bool)",
)
PAUSE_SIGS = ("pause()", "unpause()", "paused()")
TRADING_SIGS = ("setTradingEnabled(bool)", "enableTrading()", "openTrading()", "tradingOpen()")
MAX_TX_GETTER_SIGS = ("maxTxAmount()", "_maxTxAmount()")
MAX_WALLET_GETTER_SIGS = ("maxWallet()", "_maxWalletSize()", "maxWalletAmount()")
TAX_GETTER_SIGS = ("buyTax()", "sellTax()", "_buyTax()", "_sellTax()")


def _rotl64(v, n):
    return ((v << n) | (v >> (64 - n))) & MASK64 if n else v & MASK64


def _keccak_f1600(state):
    for rc in RC:
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for y in range(5):
            for x in range(5):
                state[x + 5 * y] ^= d[x]
        b = [0] * 25
        for y in range(5):
            for x in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl64(state[x + 5 * y], RHO[x][y])
        for y in range(5):
            for x in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y])
        state[0] ^= rc


def keccak256(data):
    rate = 136
    state = [0] * 25
    padded = data + b"\x01"
    padded += b"\x00" * ((rate - (len(padded) + 1) % rate) % rate) + b"\x80"
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _keccak_f1600(state)
    out = bytearray()
    while len(out) < 32:
        for i in range(rate // 8):
            out.extend(state[i].to_bytes(8, "little"))
        if len(out) >= 32:
            break
        _keccak_f1600(state)
    return bytes(out[:32])


def selector(signature):
    return "0x" + keccak256(signature.encode())[:4].hex()


def _selectors(signatures):
    return {selector(sig): sig for sig in signatures}


MINT_SELECTORS = _selectors(MINT_SIGS)
BLACKLIST_SELECTORS = _selectors(BLACKLIST_SIGS)
PAUSE_SELECTORS = _selectors(PAUSE_SIGS)
TRADING_SELECTORS = _selectors(TRADING_SIGS)
MAX_TX_GETTERS = _selectors(MAX_TX_GETTER_SIGS)
MAX_WALLET_GETTERS = _selectors(MAX_WALLET_GETTER_SIGS)
TAX_GETTERS = _selectors(TAX_GETTER_SIGS)


def _unique(items):
    out = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _word_addr(addr):
    return "0" * 24 + addr.lower()[2:]


def _u256_word(n):
    return hex(int(n))[2:].zfill(64)


def _decode_addr(word):
    if not isinstance(word, str) or not word.startswith("0x") or len(word) < 42:
        return None
    return "0x" + word[-40:].lower()


def _decode_u256(word):
    if not isinstance(word, str) or not word.startswith("0x") or len(word) < 3:
        return None
    try:
        return int(word, 16)
    except ValueError:
        return None


def _decode_u256_array(data):
    if not isinstance(data, str) or not data.startswith("0x"):
        return []
    body = data[2:]
    if len(body) < 128:
        return []
    try:
        off = int(body[0:64], 16) * 2
        n = int(body[off:off + 64], 16)
    except ValueError:
        return []
    out = []
    p = off + 64
    for _ in range(n):
        if p + 64 > len(body):
            return []
        out.append(int(body[p:p + 64], 16))
        p += 64
    return out


def _push4_selectors(code_hex):
    if not code_hex or code_hex == "0x":
        return set()
    try:
        b = bytes.fromhex(code_hex[2:])
    except ValueError:
        return set()
    out = set()
    i = 0
    while i < len(b):
        op = b[i]
        if op == 0x63 and i + 4 < len(b):
            out.add("0x" + b[i + 1:i + 5].hex())
            i += 5
            continue
        if 0x60 <= op <= 0x7F:
            i += 1 + (op - 0x5F)
            continue
        i += 1
    return out


def _hash_words(left_hex, right_hex):
    return "0x" + keccak256(bytes.fromhex(left_hex + right_hex)).hex()


def _slot_map(addr, slot, order):
    a = _word_addr(addr)
    s = _u256_word(slot)
    return _hash_words(s, a) if order == "vyper" else _hash_words(a, s)


def _slot_nested(owner, spender, slot, order):
    o = _word_addr(owner)
    sp = _word_addr(spender)
    s = _u256_word(slot)
    if order == "vyper":
        inner = keccak256(bytes.fromhex(s + o)).hex()
        return _hash_words(inner, sp)
    inner = keccak256(bytes.fromhex(o + s)).hex()
    return _hash_words(sp, inner)


def _deep_copy_override(override):
    out = {}
    for addr, cfg in (override or {}).items():
        c = dict(cfg)
        if "stateDiff" in c:
            c["stateDiff"] = dict(c["stateDiff"])
        out[addr] = c
    return out


def _rpc_call(rpc, method, params):
    try:
        return rpc.call(method, params)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(str(e))


def _eth_call(rpc, call_obj, state_override=None):
    params = [call_obj, "latest"]
    if state_override is not None:
        params.append(state_override)
    return _rpc_call(rpc, "eth_call", params)


def _is_override_error(msg):
    m = msg.lower()
    return any(x in m for x in (
        "too many arguments",
        "state override",
        "stateoverride",
        "invalid argument 2",
        "invalid arguments; expected 2",
        "expected 2 arguments",
    ))


def _resolve_implementation(rpc, token):
    for slot, label in (
        (EIP1967_IMPL_SLOT, "EIP-1967 implementation slot"),
        (EIP1822_IMPL_SLOT, "EIP-1822 implementation slot"),
    ):
        v = _rpc_call(rpc, "eth_getStorageAt", [token, slot, "latest"])
        impl = _decode_addr(v)
        if impl and impl != ZERO:
            return impl, label
    beacon = _decode_addr(_rpc_call(rpc, "eth_getStorageAt", [token, EIP1967_BEACON_SLOT, "latest"]))
    if beacon and beacon != ZERO:
        impl = _decode_addr(_eth_call(rpc, {"to": beacon, "data": "0x5c60da1b"}))
        if impl and impl != ZERO:
            return impl, "EIP-1967 beacon slot"
    return token, "token runtime code"


def _main_pool_and_router(rpc, chain, token, market):
    if chain not in ROUTER_BY_CHAIN or chain not in WRAPPED_BY_CHAIN:
        return None, None, None, f"Sell simulation couldn't run on {chain}: no known v2-style router mapping for this chain."
    pairs = [p.lower() for p in (market or {}).get("pairs") or [] if isinstance(p, str) and len(p) == 42]
    if not pairs:
        return None, None, None, f"Sell simulation couldn't run on {chain}: no v2-style pool address was available for this token."
    pair_liq = {k.lower(): v for k, v in ((market or {}).get("pair_liquidity") or {}).items()}
    pool = max(pairs, key=lambda p: pair_liq.get(p, 0.0))
    wrapped = WRAPPED_BY_CHAIN[chain]
    router = ROUTER_BY_CHAIN[chain]
    t0 = _decode_addr(_eth_call(rpc, {"to": pool, "data": "0x0dfe1681"}))
    t1 = _decode_addr(_eth_call(rpc, {"to": pool, "data": "0xd21220a7"}))
    if token not in (t0, t1) or wrapped not in (t0, t1):
        return None, None, None, f"Sell simulation couldn't run on {chain}: main pool {pool} is not a token/wrapped pair."
    factory = _decode_addr(_eth_call(rpc, {"to": pool, "data": "0xc45a0155"}))
    router = FACTORY_TO_ROUTER.get(factory or "", router)
    return pool, router, wrapped, None


def _encode_path(path):
    return _u256_word(len(path)) + "".join(_word_addr(a) for a in path)


def _data_get_amounts_out(amount_in, path):
    return selector("getAmountsOut(uint256,address[])") + _u256_word(amount_in) + _u256_word(64) + _encode_path(path)


def _data_swap_supporting(amount_in, min_out, path, to):
    return (
        selector("swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)")
        + _u256_word(amount_in)
        + _u256_word(min_out)
        + _u256_word(160)
        + _word_addr(to)
        + _u256_word(2**256 - 1)
        + _encode_path(path)
    )


def _probe_layout(rpc, token, router):
    amount = 987654321012345
    base = {token: {"stateDiff": {}}}
    try:
        _eth_call(rpc, {"to": token, "data": selector("balanceOf(address)") + _word_addr(PROBE)}, base)
    except RuntimeError as e:
        if _is_override_error(str(e)):
            return None, None, [(
                "Sell simulation couldn't run because this RPC rejects eth_call state overrides. "
                "Set PUBLICNODE_TOKEN (free at allnodes.com/publicnode) or RPC_<CHAIN> and run again."
            )]
        raise

    bal_layout = None
    for slot in range(11):
        for order in ("solidity", "vyper"):
            loc = _slot_map(PROBE, slot, order)
            ov = _deep_copy_override(base)
            ov[token]["stateDiff"][loc] = hex(amount)
            got = _decode_u256(_eth_call(rpc, {"to": token, "data": selector("balanceOf(address)") + _word_addr(PROBE)}, ov))
            if got == amount:
                bal_layout = (order, slot)
                break
        if bal_layout:
            break
    if not bal_layout:
        return None, None, ["Sell simulation couldn't run: balance storage layout not found."]

    allow_layout = None
    bal_loc = _slot_map(PROBE, bal_layout[1], bal_layout[0])
    for slot in range(11):
        for order in ("solidity", "vyper"):
            ov = _deep_copy_override(base)
            ov[token]["stateDiff"][bal_loc] = hex(amount)
            ov[token]["stateDiff"][_slot_nested(PROBE, router, slot, order)] = hex(amount)
            got = _decode_u256(_eth_call(
                rpc,
                {"to": token, "data": selector("allowance(address,address)") + _word_addr(PROBE) + _word_addr(router)},
                ov,
            ))
            if got == amount:
                allow_layout = (order, slot)
                break
        if allow_layout:
            break
    if not allow_layout:
        return bal_layout, None, ["Sell simulation couldn't run: allowance storage layout not found."]
    return bal_layout, allow_layout, []


def _simulate_sell(rpc, token, chain, market, total_supply_raw):
    pool, router, wrapped, cant = _main_pool_and_router(rpc, chain, token, market)
    if cant:
        return [], [cant]

    bal_layout, allow_layout, layout_cant = _probe_layout(rpc, token, router)
    if layout_cant:
        return [], layout_cant

    amount_in = max(int(total_supply_raw // 10**6), 10**6)
    path = [token, wrapped]
    expected = _decode_u256_array(_eth_call(rpc, {"to": router, "data": _data_get_amounts_out(amount_in, path)}))
    if len(expected) < 2 or expected[-1] <= 0:
        return [], [f"Sell simulation couldn't run: getAmountsOut returned no output for router {router}."]
    expected_out = expected[-1]

    ov = {
        PROBE: {"balance": hex(10**20)},
        token: {"stateDiff": {
            _slot_map(PROBE, bal_layout[1], bal_layout[0]): hex(amount_in),
            _slot_nested(PROBE, router, allow_layout[1], allow_layout[0]): hex(amount_in),
        }},
    }

    def sell_ok(min_out):
        try:
            _eth_call(rpc, {"from": PROBE, "to": router, "data": _data_swap_supporting(amount_in, min_out, path, PROBE)}, ov)
            return True, ""
        except RuntimeError as e:
            return False, str(e)

    ok0, err0 = sell_ok(0)
    if not ok0:
        return [f"Selling reverts in simulation: {err0} (simulated sell via router {router}, pool {pool})."], []

    lo, hi = 0, 10000
    for _ in range(7):
        mid = (lo + hi) // 2
        min_out = expected_out * (10000 - mid) // 10000
        ok, _ = sell_ok(min_out)
        if ok:
            hi = mid
        else:
            lo = mid + 1
    pct = hi / 100.0
    if pct >= 5.0:
        return [f"Selling loses about {pct:.1f}% (simulated sell of {amount_in} raw units via router {router}, pool {pool})."], []
    return [], []


def _owner_state(rpc, token):
    try:
        raw = _eth_call(rpc, {"to": token, "data": selector("owner()")})
    except RuntimeError:
        return "missing", None
    own = _decode_addr(raw)
    if own in (ZERO, DEAD):
        return "renounced", own
    if own:
        return "live", own
    return "missing", None


def _read_first_uint_getter(rpc, token, selectors):
    for sel, sig in selectors.items():
        try:
            raw = _decode_u256(_eth_call(rpc, {"to": token, "data": sel}))
        except RuntimeError:
            raw = None
        if raw is not None:
            return sig, raw
    return None, None


def check_contract_traps(rpc, token, chain, market, total_supply_raw):
    if not hasattr(rpc, "call"):
        raise RuntimeError("RPC object has no call()")
    token = token.lower()
    flags = []
    cant = []

    impl, impl_method = _resolve_implementation(rpc, token)
    code = _rpc_call(rpc, "eth_getCode", [impl, "latest"])
    selectors = _push4_selectors(code)

    risky = {}
    for mp in (MINT_SELECTORS, BLACKLIST_SELECTORS, PAUSE_SELECTORS, TRADING_SELECTORS):
        for sel, sig in mp.items():
            if sel in selectors:
                risky[sel] = sig
    if risky:
        state, owner = _owner_state(rpc, token)
        if state == "live":
            for sig in sorted(risky.values()):
                flags.append(f"Owner {owner} can call {sig} ({impl_method}, PUSH4 scan + owner() eth_call).")
        elif state == "missing":
            names = ", ".join(sorted(set(risky.values())))
            flags.append(f"Has {names} functions; who can call them wasn't checked.")

    for getter_map, label in ((MAX_TX_GETTERS, "Max transaction"), (MAX_WALLET_GETTERS, "Max wallet")):
        sig, raw = _read_first_uint_getter(rpc, token, {k: v for k, v in getter_map.items() if k in selectors})
        if sig and raw and total_supply_raw and 0 < raw < total_supply_raw:
            pct = 100.0 * raw / float(total_supply_raw)
            flags.append(f"{label} getter {sig} reports {pct:.2f}% of total supply (eth_call).")

    for sel, sig in TAX_GETTERS.items():
        if sel not in selectors:
            continue
        raw = _decode_u256(_eth_call(rpc, {"to": token, "data": sel}))
        if raw is not None and raw > 0:
            pct = raw / 100.0 if raw > 100 else float(raw)
            if 0 < pct <= 100:
                flags.append(f"Tax getter {sig} reports {pct:.1f}% (eth_call).")

    sim_flags, sim_cant = _simulate_sell(rpc, token, chain, market, int(total_supply_raw))
    flags.extend(sim_flags)
    cant.extend(sim_cant)
    return {"flags": _unique(flags), "cant_check": _unique(cant)}
