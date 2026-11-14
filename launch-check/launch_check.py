#!/usr/bin/env python3
"""Launch check: who launched a token, who bought first, who funded them, and who holds it now.

Usage:  python3 launch_check.py <token address> [--chain ethereum|base|bsc|robinhood|elysium|hyperevm] [--json] [--debug]
        (Solana mints are detected by their address format)

Public RPCs by default. PUBLICNODE_TOKEN (free, allnodes.com/publicnode) unlocks archive data on Ethereum, Base, BNB and HyperEVM;
RPC_<CHAIN> (e.g. RPC_BASE) uses your own endpoint. Solana: HELIUS_API_KEY (free) or RPC_SOLANA.
Stdlib only. Reports what it found and what it could not check; it never says a coin is safe.
"""
import collections
import http.client
import json
import os
import random
import re
import threading
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import snipers

UA = {"User-Agent": "launch-check/0.1 (+https://github.com/Gkisokay/crypto-skills)", "Content-Type": "application/json"}
EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
SOLANA_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
ZERO_TOPIC = "0x" + "0" * 64
DEAD_TOPIC = "0x" + "0" * 24 + DEAD[2:]
BURN_V2 = "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"  # Uniswap v2 and forks (PancakeSwap)
BURN_V3 = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"  # Uniswap v3 and forks
MINT_V2 = "0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f"
MINT_V3 = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
INCREASE_V3 = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
INITIALIZE_V4 = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
MODIFY_V4 = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
SWAP_V4 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
MINIMUM_LIQUIDITY = 1000

# chunk = blocks per eth_getLogs call that the default RPC accepted in testing (Nov 10 2026)
CHAINS = {
    "ethereum": {"gt": "eth", "rpc": "https://ethereum-rpc.publicnode.com", "chunk": 10000},
    "base": {"gt": "base", "rpc": "https://base-rpc.publicnode.com", "chunk": 50000},
    "bsc": {"gt": "bsc", "rpc": "https://bsc-rpc.publicnode.com", "chunk": 50000},
    "robinhood": {"gt": "robinhood", "rpc": "https://rpc.mainnet.chain.robinhood.com", "chunk": 5000000, "workers": 2, "batch": 5},
    "elysium": {"gt": None, "rpc": "https://rpc.elysium.kinetiq.xyz", "chunk": 5000, "workers": 2, "batch": 10, "rps": 8},  # Kinetiq official, archive; Boxa's RPC closed to outside sites Nov 10
    "solana": {"gt": "solana", "rpc": None, "chunk": 0},  # handled by solana_check.py
    "hyperevm": {"gt": "hyperevm", "rpc": "https://hyperliquid-rpc.publicnode.com", "chunk": 1000},  # rpc.hyperliquid.xyz rate-limits too hard
}

LAUNCH_WINDOW_S = 180   # early buyers: transfers within this many seconds of the mint
FUNDING_LOOKBACK_DAYS = 7  # funders: first native-coin deposit into each early buyer, up to this far back
FUNDING_LOOKBACK_S = 3600  # fallback block scan when the RPC has pruned old balances
MAX_FUND_BLOCKS = 8000  # cap on blocks scanned for funders (fast chains get a shorter lookback)
MAX_LOGS = 150000       # holder replay gives up past this many transfers and falls back to summaries
WORKERS = 4            # parallel RPC requests (per-chain "workers" overrides); lower it if your RPC rate-limits
MIN_CLUSTER_SHARE = 0.005  # a funder cluster must have bought at least 0.5% of supply to be flagged
MIN_FDV_FLAG = 50000     # below this an FDV mismatch is noise (dead or rugged coins)
BUSY_NONCE = 1000       # a funder that has sent this many txs is likely an exchange, faucet or bot
MAX_AGE_DAYS = 7
MIN_POOL_USD = 1000
BUNDLE_WINDOW_S = 5
FUNDING_BUDGET_S = 60
MONO = time.monotonic


def http_json(url, body=None, tries=7):
    data = json.dumps(body).encode() if body is not None else None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, UA), timeout=60) as r:
                return json.load(r)
        # OSError covers URLError, timeouts (socket.timeout is not TimeoutError on Python 3.9), resets and TLS errors
        # raised while the body is read; HTTPException covers truncated replies
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:
            if isinstance(e, http.client.InvalidURL):
                raise
            if isinstance(e, urllib.error.HTTPError) and e.code == 403:
                msg = e.read().decode(errors="replace")
                if "archive" in msg.lower():  # publicnode without a token: old state is refused, not throttled
                    raise RuntimeError(msg) from e
            # publicnode answers 403, not 429, when it throttles
            if i == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code not in (403, 429, 500, 502, 503, 504)):
                raise
            time.sleep(min(2 ** i, 30) * (0.5 + random.random()))  # jitter so parallel workers don't retry in step


class Reverted(RuntimeError):
    """The contract call itself failed (not the RPC): don't retry it."""


class Rpc:
    def __init__(self, url, chunk, workers, batch_size=20, rps=None):
        self.url, self.chunk, self.workers, self.batch_size = url, chunk, workers, batch_size
        self.rps, self._next, self._lock = rps, 0.0, threading.Lock()

    def _pace(self, n):
        """Hold requests to the RPC's published rate; a batch of n calls counts as n requests (Conduit, Helius)."""
        if not self.rps:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + n / self.rps
        time.sleep(max(start - now, 0))

    @staticmethod
    def _is_revert(err):
        msg = (err.get("message") if isinstance(err, dict) else str(err)) or ""
        return (isinstance(err, dict) and err.get("code") == 3) or "revert" in msg.lower()

    @staticmethod
    def _is_logs_range_error(msg):
        msg = msg.lower()
        return "block range" in msg or "narrow the block range" in msg or "query spans" in msg

    @staticmethod
    def _is_logs_max_results_error(msg):
        msg = msg.lower()
        return any(k in msg for k in ("max results", "more than", "too many results", "response size"))

    def call(self, method, params):
        for i in range(6):
            self._pace(1)
            d = http_json(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "error" not in d:
                return d["result"]
            err = d["error"]
            if self._is_revert(err):
                raise Reverted(f"{method}: {err}")
            msg = (err.get("message") if isinstance(err, dict) else str(err)) or ""
            if method == "eth_getLogs" and (self._is_logs_range_error(msg) or self._is_logs_max_results_error(msg)):
                raise RuntimeError(f"{method}: {err}")
            if i == 5:
                raise RuntimeError(f"{method}: {err}")
            time.sleep(min(2 ** i, 30) * (0.5 + random.random()))

    def _call_or_none(self, method, params):
        try:
            return self.call(method, params)
        except Reverted:
            return None  # same contract as a batch item: a reverted call reads as None

    def _batch(self, part):
        body = [{"jsonrpc": "2.0", "id": j, "method": m, "params": p} for j, (m, p) in enumerate(part)]
        last = None
        for i in range(6):
            self._pace(len(part))
            res = http_json(self.url, body)
            if not isinstance(res, list):  # some RPCs don't take batches
                return [self._call_or_none(m, p) for m, p in part]
            got = {}
            retry = False
            errs = []
            for x in res:
                idx = x.get("id")
                if idx is None:
                    continue
                if "error" not in x:
                    got[idx] = x.get("result")
                    continue
                err = x["error"]
                if self._is_revert(err):
                    got[idx] = None
                else:
                    retry = True
                    errs.append(err)
            if len(got) == len(part) and not retry:
                return [got[j] for j in range(len(part))]
            last = errs or [x["error"] for x in res if "error" in x]
            # an item that errored (often a per-item rate limit) would otherwise read as an empty block
            time.sleep(min(2 ** i, 30) * (0.5 + random.random()))
        raise RuntimeError(f"batch kept failing: {last[:1]}")

    def batch(self, calls, size=None):
        size = size or self.batch_size
        parts = [calls[i:i + size] for i in range(0, len(calls), size)]
        with ThreadPoolExecutor(self.workers) as ex:
            return [x for part in ex.map(self._batch, parts) for x in part]

    def _range(self, flt, a, b):
        try:
            return self.call("eth_getLogs", [dict(flt, fromBlock=hex(a), toBlock=hex(b))])
        except RuntimeError as e:
            if b - a < 10:
                raise
            mid = (a + b) // 2  # the RPC refused the range (too many results or too wide): split it
            msg = str(e)
            if self._is_logs_range_error(msg):
                self.chunk = min(self.chunk, max(mid - a + 1, 10))  # hard block-range caps should tune later spans
            return self._range(flt, a, mid) + self._range(flt, mid + 1, b)

    def logs(self, flt, frm, to, cap=None):
        """Logs from block frm to to, fetched in parallel waves; stops early once past cap."""
        spans = [(b, min(b + self.chunk - 1, to)) for b in range(frm, to + 1, self.chunk)]
        got = []
        with ThreadPoolExecutor(self.workers) as ex:
            for i in range(0, len(spans), self.workers * 2):
                for part in ex.map(lambda ab: self._range(flt, *ab), spans[i:i + self.workers * 2]):
                    got += part
                if cap and len(got) > cap:
                    return got, False
        return got, True

    def block_ts(self, n):
        for i in range(4):  # a load-balanced public RPC can answer from a node that hasn't seen the newest block yet
            blk = self.call("eth_getBlockByNumber", [hex(n), False])
            if blk:
                return int(blk["timestamp"], 16)
            time.sleep(0.5 * (i + 1))
        raise RuntimeError(f"eth_getBlockByNumber: block {n} is not available from this RPC")

    def block_at(self, t, head, head_ts, bt):
        """First block at or after time t. Searches only near t: public RPCs refuse very old blocks."""
        lo = max(int(head - (head_ts - t) / bt * 1.5) - 1000, 0)
        hi = head
        while lo < hi:
            m = (lo + hi) // 2
            if self.block_ts(m) < t:
                lo = m + 1
            else:
                hi = m
        return lo

    def eth_call(self, to, data):
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])


def word(addr):
    return "0" * 24 + addr.lower()[2:]


def word_u256(n):
    return hex(n)[2:].zfill(64)


def words(data):
    return [int(data[2 + 64 * i:66 + 64 * i], 16) for i in range((len(data) - 2) // 64)]


def signed(n, bits):
    return n - (1 << bits) if n >= (1 << (bits - 1)) else n


def i256(hexword):
    return signed(int(hexword, 16), 256)


def decode_string(hexdata):
    b = bytes.fromhex(hexdata[2:])
    if len(b) >= 64:
        n = int.from_bytes(b[32:64], "big")
        return b[64:64 + n].decode(errors="replace")
    return b.rstrip(b"\0").decode(errors="replace")


def wallet_kind(code):
    """EOA, EIP-7702 smart EOA (still a person), or contract."""
    if code in (None, "0x"):
        return "person"
    if code.startswith("0xef0100"):
        return "person (7702)"
    return "contract"


def real_fdv(price, total_supply, burned):
    return price * (total_supply - burned)


def funder_clusters(sends, creator, min_wallets=3):
    """sends: (funder, recipient) pairs. A cluster is a funder that paid 3+ early buyers, or the creator paying any."""
    by = collections.defaultdict(set)
    for f, r in sends:
        by[f].add(r)
    return {f: sorted(ws) for f, ws in by.items() if len(ws) >= min_wallets or f == creator}


def bundle_groups(open_ts, buys, total, held=None):
    """buys: wallet -> (amount bought, first-buy timestamp). held: the most the buyers held at once, when tokens
    resold inside the window would make the per-wallet sum overstate it (Solana)."""
    if not buys or total <= 0:
        return []
    rows = [(w, amt / total, ts) for w, (amt, ts) in buys.items() if amt > 0]
    if not rows:
        return []
    fast = [(w, sh, ts) for w, sh, ts in rows if ts - open_ts <= BUNDLE_WINDOW_S]
    if len(fast) >= 4:
        sizes = sorted(sh for _, sh, _ in fast)
        med = sizes[len(sizes) // 2] if len(sizes) % 2 else (sizes[len(sizes) // 2 - 1] + sizes[len(sizes) // 2]) / 2
        near = [(w, sh, ts) for w, sh, ts in fast if med > 0 and abs(sh - med) <= med * 0.10]
        took = sum(sh for _, sh, _ in near)
        if len(near) >= 4 and took >= 0.20:
            span = max(ts for _, _, ts in near) - open_ts
            s = f"{span:.2f}".rstrip("0").rstrip(".")
            return [{"wallets": [w for w, _, _ in near],
                     "text": f"Likely bundle: {len(near)} wallets bought {took:.1%} of supply within {s} s of trading opening, "
                             f"in near-identical sizes (~{med:.1%} each)."}]
    took_all = held / total if held is not None else sum(sh for _, sh, _ in rows)
    if took_all >= 0.30:
        return [{"wallets": [w for w, _, _ in rows],
                 "text": f"{len(rows)} wallets took {took_all:.1%} of supply in the first 3 minutes of trading."}]
    return []


def bundle_flags(open_ts, buys, total, wallet_profiles=None):
    groups = bundle_groups(open_ts, buys, total)
    groups = snipers.relabel_bundle_groups(groups, open_ts, buys, total, wallet_profiles or {})
    return [x["text"] for x in groups]


def wallet_flow_note(rpc, token, wallets, total, unit, complete, logs, replay_bal, pools):
    wallets = sorted(set(wallets))
    if not wallets or total <= 0:
        return "; they hold 0.0% now."
    if replay_bal is None:
        res = rpc.batch([("eth_call", [{"to": token, "data": "0x70a08231" + word(w)}, "latest"]) for w in wallets])
        holds = {w: int(x or "0x0", 16) / unit for w, x in zip(wallets, res)}
    else:
        holds = {w: max(replay_bal.get(w, 0), 0) / unit for w in wallets}
    hold_share = sum(holds.values()) / total
    note = f"; they hold {hold_share:.1%} now"
    if not complete or logs is None or replay_bal is None:
        return note + "."
    ws = set(wallets)
    out = collections.Counter()
    for l in logs:
        frm = "0x" + l["topics"][1][26:]
        if frm not in ws:
            continue
        to = "0x" + l["topics"][2][26:]
        if to in ws:
            continue
        out[to] += int(l["data"], 16) / unit
    sold = sum(amt for to, amt in out.items() if to in pools)
    moved = sum(amt for to, amt in out.items() if to not in pools)
    if sold > 0 and moved <= 0:
        return note + f" (sold {sold / total:.1%} into the pools)."
    if moved > 0:
        to, _ = max(((a, x) for a, x in out.items() if a not in pools), key=lambda x: x[1])
        k = "a plain wallet" if wallet_kind(rpc.batch([("eth_getCode", [to, "latest"])])[0]).startswith("person") else "a contract"
        to_hold = max(replay_bal.get(to, 0), 0) / unit / total
        sold_part = f" (sold {sold / total:.1%} into the pools);" if sold > 0 else ";"
        others = sum(1 for a in out if a not in pools)
        dest = f"to {to} ({k})" if others == 1 else f"to {others} other addresses, most of it to {to} ({k})"
        return note + f"{sold_part} {moved / total:.1%} moved {dest}, which holds {to_hold:.1%} now."
    return note + "."


def replay(logs):
    bal = collections.Counter()
    for l in logs:
        amt = int(l["data"], 16)
        bal["0x" + l["topics"][1][26:]] -= amt
        bal["0x" + l["topics"][2][26:]] += amt
    return bal


def removal_time(rpc, last, first_add, start_ts):
    """When a removal happened, measured from that pool's own first liquidity add when the scan saw one.
    DexScreener's pool-creation times can be missing or late, which would otherwise give a negative time."""
    blk = int(last["blockNumber"], 16)
    ts = rpc.block_ts(blk)
    if first_add is not None and first_add <= blk:
        gap, since = ts - rpc.block_ts(first_add), "after liquidity was first added to that pool"
    else:
        gap, since = ts - start_ts, "after the first pool opened"
    if gap < 0:  # the removal predates every opening time we have: give the time itself rather than a negative span
        return time.strftime("at %Y-%m-%d %H:%M:%SZ", time.gmtime(ts))
    return f"{human_span(gap)} {since}"


def liquidity_pulls(rpc, pools, token, creator, frm, to, start_ts):
    """Wallets that took the bulk of a pool's liquidity out (Uniswap v2/v3-style Burn events), counted in the pool's
    other token and net of what the same wallet put back afterwards (Mint events), so a rebalance isn't a pull:
    flagged when one wallet's net removal is at least as much as the pool still holds."""
    if not pools:
        return []
    logs, _ = rpc.logs({"address": sorted(pools), "topics": [[BURN_V2, BURN_V3, MINT_V2, MINT_V3]]}, frm, to)
    moves = []  # (log, +1 added / -1 removed, amount0, amount1), in chain order
    for l in logs:
        ws = words(l["data"])
        if len(ws) < 2:
            continue
        kind = l["topics"][0]
        a0, a1 = ws[-2:] if kind in (BURN_V3, MINT_V3) else ws[:2]  # v3 data starts with the liquidity amount
        if a0 or a1:  # v3 positions poke fees with zero-amount burns
            moves.append((l, 1 if kind in (MINT_V2, MINT_V3) else -1, a0, a1))
    if not any(sign < 0 for _, sign, _, _ in moves):
        return []
    txs = sorted({l["transactionHash"] for l, _, _, _ in moves})
    senders = rpc.batch([("eth_getTransactionByHash", [h]) for h in txs])
    sender = {h: (tx or {}).get("from", "").lower() for h, tx in zip(txs, senders)}
    out = []
    for pool in sorted({l["address"].lower() for l, sign, _, _ in moves if sign < 0}):
        t0 = "0x" + rpc.eth_call(pool, "0x0dfe1681")[-40:]
        other = t0 if t0 != token else "0x" + rpc.eth_call(pool, "0xd21220a7")[-40:]
        unit = 10 ** int(rpc.eth_call(other, "0x313ce567"), 16)
        sym = decode_string(rpc.eth_call(other, "0x95d89b41"))
        left = int(rpc.eth_call(other, "0x70a08231" + word(pool)), 16) / unit
        took = collections.defaultdict(lambda: [0.0, None])  # wallet -> [net removed, last removal]
        first_add = None
        for l, sign, a0, a1 in moves:
            if l["address"].lower() != pool:
                continue
            if sign > 0 and first_add is None:
                first_add = int(l["blockNumber"], 16)
            w = took[sender[l["transactionHash"]]]
            amt = (a0 if t0 == other else a1) / unit
            if sign < 0:
                w[0] += amt
                w[1] = l
            else:
                w[0] = max(w[0] - amt, 0.0)
        out_total = sum(a for a, _ in took.values()) + left
        for who, (amt, last) in took.items():
            if not who or not last or amt <= 0 or amt < left:
                continue
            # once one wallet has emptied a pool, any later crumb is also "at least what's left": only the creator, or a
            # wallet that took most of what came out, is a pull
            if who != creator and amt < 0.5 * out_total:
                continue
            out.append(f"Liquidity pulled: {'The creator' if who == creator else 'Wallet'} {who} removed {amt:,.4g} {sym} "
                       f"from pool {pool}, {removal_time(rpc, last, first_add, start_ts)}; the pool has {left:,.4g} {sym} left. "
                       f"Last removal: tx {last['transactionHash']}")
    return out


def lp_findings(rpc, pool, creator, held, total, burned, kind):
    if total <= 0:
        return [], []
    rows = [(a, amt / total) for a, amt in held.items() if amt > 0 and a not in (ZERO, DEAD)]
    labels = {a: "creator" if a == creator else None for a, _ in rows}
    others = [a for a, _ in rows if labels[a] is None]
    codes = rpc.batch([("eth_getCode", [a, "latest"]) for a in others]) if others else []
    for a, c in zip(others, codes):
        labels[a] = wallet_kind(c)
    people = [(a, sh) for a, sh in rows if labels[a] == "creator" or labels[a].startswith("person")]
    contracts = [(a, sh) for a, sh in rows if labels[a] == "contract"]
    flags, notes = [], []
    if people:
        who, sh = max(people, key=lambda x: x[1])
        if sh >= 0.50:
            who = f"wallet {who}" + (" (the creator)" if who == creator else "")
            if kind == "v3":
                flags.append(f"Liquidity can be pulled: {who} controls {sh:.1%} of pool {pool}'s liquidity (v3 positions) "
                             "in a plain wallet (not burned or locked).")
            elif kind == "v4":
                flags.append(f"Liquidity can be pulled: {who} controls {sh:.1%} of pool {pool}'s liquidity (v4 positions) "
                             "in a plain wallet (not burned or locked).")
            else:
                flags.append(f"Liquidity can be pulled: {who} holds {sh:.1%} of pool {pool}'s LP tokens in a plain wallet "
                             "(not burned or locked).")
    if contracts and sum(sh for _, sh in contracts) >= 0.50:
        locker, _ = max(contracts, key=lambda x: x[1])
        sh = sum(x for _, x in contracts)
        if kind == "v3":
            notes.append(f"{sh:.1%} of pool {pool}'s liquidity (v3 positions) is controlled by contract {locker} "
                         "(may be a locker, or a multisig or smart wallet someone controls; check what it is and when it unlocks).")
        elif kind == "v4":
            notes.append(f"{sh:.1%} of pool {pool}'s liquidity is controlled by contract {locker} "
                         "(may be a launchpad, a locker, or a smart wallet someone controls; check whether it can withdraw).")
        else:
            notes.append(f"{sh:.1%} of pool {pool}'s LP tokens are in contract {locker} "
                         "(may be a locker, or a multisig or smart wallet someone controls; check what it is and when it unlocks).")
    if burned >= 0.95:
        if kind == "v3":
            notes.append(f"Pool {pool}'s liquidity (v3 positions) is burned ({burned:.1%}), so that liquidity can't be pulled.")
        elif kind == "v4":
            notes.append(f"Pool {pool}'s liquidity (v4 positions) is burned ({burned:.1%}), so that liquidity can't be pulled.")
        else:
            notes.append(f"Pool {pool}'s LP tokens are burned ({burned:.1%}), so that liquidity can't be pulled.")
    return flags, notes


def lp_v2_state(rpc, pool, frm, to):
    logs, _ = rpc.logs({"address": pool, "topics": [TRANSFER]}, frm, to)
    if not logs:
        return None
    bal = replay(logs)
    total = int(rpc.eth_call(pool, "0x18160ddd"), 16)
    min_liq = next((int(l["data"], 16) for l in logs
                    if l["topics"][1] == ZERO_TOPIC and l["topics"][2] == ZERO_TOPIC), 0)
    min_liq = min(min_liq, MINIMUM_LIQUIDITY)
    supply = total - min_liq
    fee_to = None
    try:
        factory = "0x" + rpc.eth_call(pool, "0xc45a0155")[-40:]
        fee_to = "0x" + rpc.eth_call(factory, "0x017e7e58")[-40:]
        if fee_to == ZERO:
            fee_to = None
    except RuntimeError:
        pass
    if fee_to:
        supply -= max(bal.get(fee_to, 0), 0)
    pair_topic = "0x" + "0" * 24 + pool[2:]
    burned_raw = sum(int(l["data"], 16) for l in logs
                     if l["topics"][2] in (ZERO_TOPIC, DEAD_TOPIC) and l["topics"][1] not in (ZERO_TOPIC, pair_topic))
    burned = min(burned_raw / supply, 1.0) if supply > 0 else 0.0
    held = {a: b for a, b in bal.items() if b > 0 and a != fee_to}
    return held, supply, burned


def lp_v3_state(rpc, pool, frm, to):
    mints, _ = rpc.logs({"address": pool, "topics": [MINT_V3]}, frm, to)
    if not mints:
        return None
    burns, _ = rpc.logs({"address": pool, "topics": [BURN_V3]}, frm, to)
    txs = sorted({l["transactionHash"] for l in mints})
    recs = rpc.batch([("eth_getTransactionReceipt", [h]) for h in txs]) if txs else []
    token_ids = collections.defaultdict(list)
    for h, rc in zip(txs, recs):
        for l in (rc or {}).get("logs", []):
            t = l.get("topics") or []
            if len(t) > 1 and t[0] == INCREASE_V3:
                token_ids[(h, (l.get("address") or "").lower())].append(int(t[1], 16))
    direct = collections.Counter()
    nft = set()
    for l in mints:
        owner = "0x" + l["topics"][1][26:]
        ws = words(l["data"])
        liq = ws[1] if len(ws) > 1 else 0
        if liq <= 0:
            continue
        ids = token_ids[(l["transactionHash"], owner)]
        if ids:
            nft.add((owner, ids.pop(0)))
        else:
            direct[(owner, l["topics"][2], l["topics"][3])] += liq
    for l in burns:
        owner = "0x" + l["topics"][1][26:]
        ws = words(l["data"])
        if ws:
            direct[(owner, l["topics"][2], l["topics"][3])] -= ws[0]
    held = collections.Counter()
    for (owner, _, _), liq in direct.items():
        if liq > 0:
            held[owner] += liq
    nft = sorted(nft)
    if nft:
        res = rpc.batch([("eth_call", [{"to": mgr, "data": "0x99fbab88" + word_u256(tid)}, "latest"]) for mgr, tid in nft])
        live = []
        for (mgr, tid), x in zip(nft, res):
            if x is None:
                continue
            ws = words(x)
            if len(ws) > 7 and ws[7] > 0:
                live.append((mgr, tid, ws[7]))
        if live:
            own = rpc.batch([("eth_call", [{"to": mgr, "data": "0x6352211e" + word_u256(tid)}, "latest"]) for mgr, tid, _ in live])
            for (_, _, liq), who in zip(live, own):
                if who is None:
                    continue
                held["0x" + who[-40:]] += liq
    total = sum(held.values())
    burned = (held.get(ZERO, 0) + held.get(DEAD, 0)) / total if total else 0.0
    return held, total, burned


def v4_manager(rpc, token, ids, frm, to, logs=None):
    if logs is None:  # only the token's first transactions are read, so stop after the first wave of logs
        logs, _ = rpc.logs({"address": token, "topics": [TRANSFER]}, frm, to, cap=20)
    ids = {x.lower() for x in ids}
    logs = sorted(logs, key=lambda l: (int(l.get("blockNumber", "0x0"), 16),
                                       int(l.get("transactionIndex", "0x0"), 16),
                                       int(l.get("logIndex", "0x0"), 16)))
    txs = []
    seen = set()
    for l in logs:
        h = l.get("transactionHash")
        if h and h not in seen:
            txs.append(h)
            seen.add(h)
            if len(txs) >= 20:
                break
    if not txs:
        return None
    recs = rpc.batch([("eth_getTransactionReceipt", [h]) for h in txs])
    for rc in recs:
        for l in (rc or {}).get("logs", []):
            ts = l.get("topics") or []
            if len(ts) > 1 and ts[0] in (SWAP_V4, INITIALIZE_V4, MODIFY_V4) and ts[1].lower() in ids and l.get("address"):
                return l["address"].lower()
    return None


def lp_control_v4(rpc, token, ids, creator, frm, to, start_ts, manager=None, ids_control=None, pool_liquidity=None):
    if not ids:
        return [], []
    ids = {x.lower() for x in ids}
    ids_control = {x.lower() for x in (ids if ids_control is None else ids_control)}
    manager = manager or v4_manager(rpc, token, ids, frm, to)
    if not manager:
        return [], ["Uniswap v4 pools couldn't be checked: couldn't find the PoolManager from recent token tx receipts."]
    logs, _ = rpc.logs({"address": manager, "topics": [[INITIALIZE_V4, MODIFY_V4], sorted(ids)]}, frm, to)
    if not logs:
        return [], []
    hooks = {}
    mods = []
    for l in logs:
        t = l["topics"]
        if t[0] == INITIALIZE_V4:
            ws = words(l["data"])
            if len(ws) > 2:
                h = "0x" + word_u256(ws[2])[-40:]
                if h != ZERO:
                    hooks[t[1].lower()] = h
            continue
        if t[0] != MODIFY_V4 or len(t) < 3:
            continue
        ws = words(l["data"])
        if len(ws) < 4:
            continue
        delta = i256("0x" + word_u256(ws[2]))
        pos = (t[1].lower(), "0x" + t[2][26:], signed(ws[0], 256), signed(ws[1], 256), ws[3])
        mods.append((l, pos, delta))
    live = collections.Counter()
    for _, pos, d in mods:
        live[pos] += d
    senders = sorted({sender for _, sender, _, _, _ in live})
    nft_managers = set()
    for s in senders:
        try:
            if "positions nft" in decode_string(rpc.eth_call(s, "0x06fdde03")).lower():
                nft_managers.add(s)
        except RuntimeError:
            pass
    held = collections.defaultdict(collections.Counter)
    nft = []
    for (pid, sender, _, _, salt), liq in live.items():
        if liq <= 0:
            continue
        if sender in nft_managers:
            nft.append((pid, sender, salt, liq))
        else:
            held[pid][sender] += liq
    if nft:
        own = rpc.batch([("eth_call", [{"to": mgr, "data": "0x6352211e" + word_u256(salt)}, "latest"])
                         for _, mgr, salt, _ in nft])
        for (pid, _, _, liq), who in zip(nft, own):
            if who is None:
                continue
            held[pid]["0x" + who[-40:]] += liq
    flags, notes = [], []
    for pid in sorted(ids):
        if pid in ids_control and pid in hooks:
            notes.append(f"Pool {pid} uses hook contract {hooks[pid]}; hooks can change how the pool trades or who can withdraw. "
                         "Check what it does.")
        total = sum(held[pid].values())
        burned = (held[pid].get(ZERO, 0) + held[pid].get(DEAD, 0)) / total if total else 0.0
        if pid in ids_control:
            f, n = lp_findings(rpc, pid, creator, held[pid], total, burned, "v4")
            flags += f
            notes += n
    if not mods:
        return flags, notes
    txs = sorted({l["transactionHash"] for l, _, _ in mods})
    send = rpc.batch([("eth_getTransactionByHash", [h]) for h in txs])
    tx_from = {h: (tx or {}).get("from", "").lower() for h, tx in zip(txs, send)}
    by_tx = collections.defaultdict(lambda: [0, None])  # (pool, tx) -> [net delta, any log from tx]
    for l, _, delta in mods:
        w = by_tx[(l["topics"][1].lower(), l["transactionHash"])]
        w[0] += delta
        w[1] = l
    by_pool = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, None]))  # pool -> wallet -> [sum removals, last removal log]
    for (pid, tx), (net, last) in by_tx.items():
        if net >= 0:
            continue
        who = tx_from.get(tx, "")
        if not who:
            continue
        w = by_pool[pid][who]
        w[0] += -net
        w[1] = last
    peak, running = collections.Counter(), collections.Counter()  # the most liquidity each pool ever had
    for _, pos, d in mods:
        running[pos[0]] += d
        peak[pos[0]] = max(peak[pos[0]], running[pos[0]])

    first_add = {}  # pool -> block of its first liquidity add
    for l, pos, d in mods:
        if d > 0:
            first_add.setdefault(pos[0], int(l["blockNumber"], 16))

    def when(pid, last):
        return removal_time(rpc, last, first_add.get(pid), start_ts)

    known_usd = sum((pool_liquidity or {}).values())
    others = []  # removals by wallets other than the creator while the coin still has liquidity elsewhere
    for pid, wallets in by_pool.items():
        left = sum(held[pid].values())
        for who, (removed, last) in wallets.items():
            if removed <= 0 or removed < left or not last:
                continue
            share = min(removed / peak[pid], 1.0) if peak[pid] > 0 else 0.0
            if share < 0.5:  # a small position leaving a pool that later emptied is not a pull
                continue
            liq = (pool_liquidity or {}).get(pid)
            if who != creator and known_usd >= MIN_POOL_USD:
                others.append((share, who, pid, last))
            elif liq is not None and liq >= MIN_POOL_USD:
                notes.append(f"The creator {who} removed {share:.1%} of pool {pid}'s peak "
                             f"liquidity positions {when(pid, last)}, but the pool still shows ${liq:,.0f} of liquidity "
                             "(likely held by its hook). Check where the liquidity sits.")
            else:
                flags.append(f"Liquidity pulled: {'The creator' if who == creator else 'Wallet'} {who} removed {share:.1%} "
                             f"of pool {pid}'s peak liquidity, {when(pid, last)}. "
                             f"Last removal: tx {last['transactionHash']}")
    if others:
        share, who, pid, last = max(others, key=lambda o: o[0])
        notes.append(f"{len({o[1] for o in others})} wallets other than the creator took liquidity positions out of "
                     f"{len({o[2] for o in others})} v4 pools, while the coin's pools still show ${known_usd:,.0f} of liquidity. "
                     "LPs removing their own positions is routine unless one of them is tied to the team. "
                     f"Largest: wallet {who}, {share:.1%} of pool {pid}'s peak liquidity, {when(pid, last)}, "
                     f"tx {last['transactionHash']}.")
    return flags, notes


def lp_control(rpc, pools, creator, frm, to):
    if not pools:
        return [], []
    flags, notes = [], []
    for pool in sorted(pools):
        pool = pool.lower()
        v2 = lp_v2_state(rpc, pool, frm, to)
        if v2:
            held, total, burned = v2
            f, n = lp_findings(rpc, pool, creator, held, total, burned, "v2")
        else:
            v3 = lp_v3_state(rpc, pool, frm, to)
            if not v3:
                notes.append(f"Who controls pool {pool}'s liquidity wasn't checked: no LP-token transfers or v3 mints since launch "
                             "(not a Uniswap v2/v3-style pool, or its liquidity was added earlier).")
                continue
            held, total, burned = v3
            f, n = lp_findings(rpc, pool, creator, held, total, burned, "v3")
        flags += f
        notes += n
    return flags, notes


def flags(r):
    out = list(r.get("extra_flags", []))
    s = r["supply"]
    if r.get("creator_share") is not None and r["creator_share"] > 0.05:
        out.append(f"Creator wallet holds {r['creator_share']:.1%} of supply.")
    for c in r.get("clusters", []):
        who = ("The creator" if c["funder"] == r.get("creator") else
               f"One {c['funder']} (likely a multisend)" if c["funder"].startswith("contract call") else f"One wallet ({c['funder']})")
        if c.get("flow_note"):
            out.append(f"{who} funded {len(c['wallets'])} first-minutes buyers; they bought {c['bought_share']:.1%}{c['flow_note']}")
        else:
            out.append(f"{who} funded {len(c['wallets'])} first-minutes buyers; they bought {c['bought_share']:.1%} "
                       f"and hold {c['held_share']:.1%} now.")
    m = r.get("market") or {}
    if (m.get("dex_fdv") or 0) > MIN_FDV_FLAG and (s.get("real_fdv") or 0) > 0 and max(m["dex_fdv"], s["real_fdv"]) > 1.5 * min(m["dex_fdv"], s["real_fdv"]):
        why = ("DexScreener looks to be counting supply that was burned or no longer exists." if m["dex_fdv"] > s["real_fdv"]
               else "DexScreener looks to be using a smaller supply (for example circulating supply only).")
        out.append(f"DexScreener FDV ${m['dex_fdv']:,.0f} vs real FDV ${s['real_fdv']:,.0f} "
                   f"(price × supply still in existence); {why}")
    h = r.get("holders") or {}
    if h.get("top10_people_share") is not None and h["top10_people_share"] > 0.30:
        out.append(f"Top 10 people (pools, burns, contracts excluded) hold {h['top10_people_share']:.1%}.")
    return out


def market(addr, chain):
    d = http_json(f"https://api.dexscreener.com/latest/dex/tokens/{addr}")
    evm = addr.startswith("0x")
    norm = (lambda a: a.lower()) if evm else (lambda a: a)  # Solana addresses are base58 and case-sensitive
    usd = lambda p: (p.get("liquidity") or {}).get("usd")  # None when DexScreener has no figure (common on v4 hook pools)
    pairs = [p for p in d.get("pairs") or [] if norm(p["baseToken"]["address"]) == norm(addr)]
    if not chain:
        known = [p for p in pairs if p["chainId"] in CHAINS]
        if not known:
            raise SystemExit(f"DexScreener lists no pools for {addr} on {', '.join(CHAINS)}. Check the address; if the coin "
                             "is only minutes old it may not be listed yet, so pass --chain <chain> to check it anyway.")
        chain = max(known, key=lambda p: usd(p) or 0)["chainId"]
    pairs = [p for p in pairs if p["chainId"] == chain]
    if not pairs:
        return chain, None
    top = max(pairs, key=lambda p: usd(p) or 0)
    pair_liquidity = {}  # only pools with a figure: a missing figure means unknown, not $0
    for p in pairs:
        a = norm(p.get("pairAddress") or "")
        if a and usd(p) is not None:
            pair_liquidity[a] = pair_liquidity.get(a, 0.0) + float(usd(p))
    created = [p["pairCreatedAt"] for p in pairs if p.get("pairCreatedAt")]
    return chain, {
        "symbol": top["baseToken"]["symbol"], "price": float(top.get("priceUsd") or 0), "dex_fdv": top.get("fdv"),
        "liquidity": sum(pair_liquidity.values()) if pair_liquidity else None,
        "pairs": sorted({norm(p["pairAddress"]) for p in pairs if evm and len(p.get("pairAddress") or "") == 42}),
        "pairs_any": sorted({norm(p["pairAddress"]) for p in pairs if p.get("pairAddress")}),
        "pair_liquidity": pair_liquidity,
        "first_pair_ts": min(created) / 1000 if created else None,
    }


def gt_holders(chain, addr):
    net = CHAINS[chain]["gt"]
    if not net:
        return None
    try:
        a = http_json(f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{addr}/info")["data"]["attributes"]
    except (urllib.error.URLError, KeyError, TypeError):
        return None
    return a.get("holders")


def check(addr, chain=None):
    if not addr.startswith("0x"):  # Solana addresses are base58 and case-sensitive
        chain, m = market(addr, "solana")
        import solana_check
        return solana_check.check_solana(sys.modules[__name__], addr, m)
    addr = addr.lower()
    chain, m = market(addr, chain)
    cfg = CHAINS[chain]
    url = os.environ.get("RPC_" + chain.upper()) or cfg["rpc"]
    if "publicnode.com" in url and os.environ.get("PUBLICNODE_TOKEN") and not os.environ.get("RPC_" + chain.upper()):
        url = url.rstrip("/") + "/" + os.environ["PUBLICNODE_TOKEN"]  # free personal token: archive data, fewer limits
    rpc = Rpc(url, cfg["chunk"], cfg.get("workers", WORKERS), cfg.get("batch", 20),
              None if os.environ.get("RPC_" + chain.upper()) else cfg.get("rps"))
    r = {"address": addr, "chain": chain, "market": m, "notes": []}

    try:
        dec = int(rpc.eth_call(addr, "0x313ce567"), 16)
        unit = 10 ** dec
        total = int(rpc.eth_call(addr, "0x18160ddd"), 16) / unit
    except (ValueError, Reverted):  # "0x" or a revert: there is no token contract at this address on this chain
        raise SystemExit(f"{addr} isn't a token contract on {chain}. Check the address and the chain.")
    if total <= 0:
        raise SystemExit(f"{addr} reports a total supply of 0 on {chain}; there is nothing to check.")
    burned = sum(int(x or "0x0", 16) for x in rpc.batch([("eth_call", [{"to": addr, "data": "0x70a08231" + word(a)}, "latest"])
                                                  for a in (ZERO, DEAD)])) / unit
    r["symbol"] = m["symbol"] if m else decode_string(rpc.eth_call(addr, "0x95d89b41"))
    r["supply"] = {"total": total, "burned": burned,
                   "real_fdv": real_fdv(m["price"], total, burned) if m and m["price"] else None}

    head = int(rpc.call("eth_blockNumber", []), 16)
    head_ts = rpc.block_ts(head)
    bt = (head_ts - rpc.block_ts(head - 1000)) / 1000 or 1.0
    if m and m.get("first_pair_ts"):
        age_s = head_ts - m["first_pair_ts"]
        if age_s > MAX_AGE_DAYS * 86400:
            age_d = int(age_s / 86400)
            r["notes"].append(f"This coin is {age_d} days old. launch-check is built for new launches (first {MAX_AGE_DAYS} days), "
                              "so the launch, bundle and liquidity checks were skipped.")
            return finish(r, rpc, addr, unit, total, burned, None, head)
    out_of_history = False
    try:
        anchor = (rpc.block_at(m["first_pair_ts"], head, head_ts, bt)
                  if m and m.get("first_pair_ts") and m["first_pair_ts"] < head_ts else head)
    except RuntimeError as e:  # the first pool is older than a no-token RPC's recent state
        if "archive" not in str(e).lower():
            raise
        anchor, out_of_history = head, True
    # the mint comes before the first pool; walk back from the pool up to 7 days to find it
    mint = None
    lookback = int(7 * 86400 / bt)
    b = anchor
    while not out_of_history and b > max(anchor - lookback, 0) and not mint:
        frm = max(b - rpc.chunk, anchor - lookback, 0)
        try:
            found, _ = rpc.logs({"address": addr, "topics": [TRANSFER, ZERO_TOPIC]}, frm, min(b + 50, head))
        except RuntimeError as e:  # public RPCs stop serving logs past their recent window
            if not any(k in str(e).lower() for k in ("archive", "pruned", "not available", "history")):
                raise
            out_of_history = True
            break
        if found:
            mint = found[0]
        b = frm - 1
    if not mint:
        r["notes"].append("Couldn't find the mint, so the launch and liquidity checks were skipped: the RPC doesn't serve history that far back. "
                          + ("Set PUBLICNODE_TOKEN (free at allnodes.com/publicnode) and run it again."
                             if "publicnode.com" in rpc.url else
                             "Set RPC_" + chain.upper() + " to an archive RPC (most providers' free tiers have one).")
                          if out_of_history else
                          "Couldn't find the mint within 7 days before the first pool, so launch checks were skipped.")
        return finish(r, rpc, addr, unit, total, burned, None, head, rug_from=None if out_of_history else anchor)

    t0 = int(mint["blockNumber"], 16)
    t0_ts = rpc.block_ts(t0)
    ref = min(t0 + 1000, head)  # block time around the launch, not today's: Elysium went from ~0.32s to ~0.11s at mainnet
    bt = (rpc.block_ts(ref) - t0_ts) / (ref - t0) if ref > t0 else bt
    creator = rpc.call("eth_getTransactionByHash", [mint["transactionHash"]])["from"].lower()
    r["launch"] = {"block": t0, "ts": t0_ts, "creator": creator, "block_time_s": round(bt, 3)}
    r["creator"] = creator

    win = int(LAUNCH_WINDOW_S / bt)
    all_logs, complete = rpc.logs({"address": addr, "topics": [TRANSFER]}, t0, head, cap=MAX_LOGS)
    holder_logs = all_logs if complete else None  # finish() replays these; None means the scan hit MAX_LOGS
    start = first_buy_block(all_logs, creator, addr) if complete else None
    if start is None:  # too many transfers to replay: fall back to the minutes after the mint
        start = t0
        all_logs, complete = None, False
    r["launch"]["first_buy_block"] = start
    if start != t0:
        r["launch"]["first_buy_ts"] = rpc.block_ts(start)
    if complete:
        early_logs = [l for l in all_logs if start <= int(l["blockNumber"], 16) <= start + win]
    else:
        early_logs, _ = rpc.logs({"address": addr, "topics": [TRANSFER]}, start, min(start + win, head))
    recv = collections.Counter()
    first_block = {}
    for l in early_logs:
        blk = int(l["blockNumber"], 16)
        to = "0x" + l["topics"][2][26:]
        if to not in (ZERO, DEAD, creator, addr):
            recv[to] += int(l["data"], 16) / unit
            first_block[to] = min(first_block.get(to, blk), blk) if to in first_block else blk
    cand = list(recv)
    codes = rpc.batch([("eth_getCode", [a, "latest"]) for a in cand])
    people = {a for a, c in zip(cand, codes) if wallet_kind(c).startswith("person")}
    r["launch"]["early_buyers"] = len(people)
    # the most early buyers held at once: summing receipts double-counts tokens sold and bought again in the window
    running, live, peak_held = collections.Counter(), 0.0, 0.0
    for l in early_logs:
        frm, to, amt = "0x" + l["topics"][1][26:], "0x" + l["topics"][2][26:], int(l["data"], 16) / unit
        if frm in people:
            out = min(amt, running[frm])
            running[frm] -= out
            live -= out
        if to in people:
            running[to] += amt
            live += amt
        peak_held = max(peak_held, live)
    r["launch"]["early_bought_share"] = peak_held / total if total else 0
    if people:
        open_ts = t0_ts if start == t0 else r["launch"]["first_buy_ts"]
        blocks = sorted({first_block[a] for a in people if a in first_block})
        ts = {}
        if blocks:
            got = rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in blocks])
            ts = {b: int((g or {}).get("timestamp", "0x0"), 16) for b, g in zip(blocks, got)}
        buys = {a: (recv[a], ts.get(first_block[a], open_ts)) for a in people if a in first_block}
        r["bundles"] = bundle_groups(open_ts, buys, total, peak_held)
        profiles = snipers.evm_wallet_profiles(rpc, list(buys))
        r["bundles"] = snipers.relabel_bundle_groups(r["bundles"], open_ts, buys, total, profiles, held=peak_held)

    sends, unresolved = first_funders(rpc, sorted(people), start, min(start + win, head), bt, r)
    if unresolved:
        why = r["launch"].get("funding_unresolved") or {}
        older, unfunded = why.get("older", 0), why.get("unfunded", 0)
        parts = []
        if older:
            parts.append(f"{older} were already funded more than {FUNDING_LOOKBACK_DAYS} days before launch (older wallets, not fresh ones)")
        if unfunded:
            parts.append(f"{unfunded} held no native coin in the launch window (someone else paid their gas, or they only received tokens)")
        if unresolved - older - unfunded > 0:
            parts.append(f"{unresolved - older - unfunded} were funded inside a contract call, or the lookup didn't finish or the RPC "
                         "doesn't keep that history (an archive RPC or a key fixes the last two)")
        r["notes"].append(f"Couldn't find who funded {unresolved} of {len(people)} early buyers: " + "; ".join(parts) + ".")
    cl = funder_clusters(sends, creator)
    busy = {}
    if cl:
        real = [f for f in cl if f.startswith("0x")]
        nonces = rpc.batch([("eth_getTransactionCount", [f, "latest"]) for f in real])
        busy = {f: int(n, 16) >= BUSY_NONCE for f, n in zip(real, nonces)}
    held_all = {}
    ws_all = sorted({w for ws in cl.values() for w in ws})
    if ws_all:
        res = rpc.batch([("eth_call", [{"to": addr, "data": "0x70a08231" + word(w)}, "latest"]) for w in ws_all])
        held_all = {w: int(x or "0x0", 16) / unit for w, x in zip(ws_all, res)}
    r["clusters"] = []
    for f, ws in cl.items():
        if busy.get(f) and f != creator:
            r["notes"].append(f"{f} funded {len(ws)} early buyers but has sent {BUSY_NONCE}+ txs "
                              "(likely an exchange, faucet or bot), so it isn't counted as a cluster.")
            continue
        bought = sum(recv[w] for w in ws) / total
        if bought < MIN_CLUSTER_SHARE and f != creator:
            continue  # dust: wallets that got crumbs in the window, not buyers
        r["clusters"].append({"funder": f, "wallets": ws, "bought_share": bought,
                              "held_share": sum(held_all.get(w, 0) for w in ws) / total})
    return finish(r, rpc, addr, unit, total, burned, t0, head, holder_logs, too_many=holder_logs is None)


def first_buy_block(logs, creator, token):
    """First block where tokens leave a pool or curve for someone other than the creator: when trading opened.
    On Ethereum a token is often minted hours before liquidity is added, so the mint block is the wrong start."""
    skip_to = {ZERO, DEAD, creator, token}
    for l in logs:
        frm, to = "0x" + l["topics"][1][26:], "0x" + l["topics"][2][26:]
        if frm not in (ZERO, creator) and to not in skip_to:
            return int(l["blockNumber"], 16)
    return None


def first_funders(rpc, wallets, t0, end, bt, r):
    """Who first sent each wallet native coin. Binary-searches each wallet's first funded block using old balances
    (any distance back, same cost on fast and slow chains); where the RPC has pruned that history, falls back to
    scanning blocks in the FUNDING_LOOKBACK_S before the launch."""
    stop = MONO() + FUNDING_BUDGET_S
    timed_out = False

    def out_of_time():
        return MONO() >= stop

    def timeout_note(traced):
        r.setdefault("notes", []).append(f"Funding lookups stopped after {FUNDING_BUDGET_S} s (slow RPC); {traced} of {len(wallets)} early buyers traced. "
                                         "A faster RPC (RPC_<CHAIN>) traces the rest.")

    lo0 = max(t0 - int(FUNDING_LOOKBACK_DAYS * 86400 / bt), 1)
    lo = {w: lo0 for w in wallets}
    hi = {w: end for w in wallets}  # buyers still hold gas money at the end of the launch window
    found = None
    try:
        while any(lo[w] < hi[w] for w in wallets):
            if out_of_time():
                timed_out = True
                break
            todo = [w for w in wallets if lo[w] < hi[w]]
            mids = {w: (lo[w] + hi[w]) // 2 for w in todo}
            res = rpc.batch([("eth_getBalance", [w, hex(mids[w])]) for w in todo])
            for w, x in zip(todo, res):
                if int(x, 16) > 0:
                    hi[w] = mids[w]
                else:
                    lo[w] = mids[w] + 1
        found = {w: lo[w] for w in wallets if lo[w] == hi[w]}
        r["launch"]["funding_method"] = f"first-funded block per wallet, up to {FUNDING_LOOKBACK_DAYS} days back"
    except RuntimeError:  # pruned state: scan the blocks just before launch instead
        found = None
    sends, unresolved, traced = [], 0, set()
    why = r["launch"].setdefault("funding_unresolved", collections.Counter())
    if found:
        blocks = sorted(set(found.values()))
        txs = {}
        step = max(getattr(rpc, "batch_size", 20) or 20, 1)
        for i in range(0, len(blocks), step):
            if out_of_time():
                timed_out = True
                break
            part = blocks[i:i + step]
            got = rpc.batch([("eth_getBlockByNumber", [hex(b), True]) for b in part])
            txs.update(zip(part, got))
        by_block = collections.defaultdict(list)
        for w, b in found.items():
            if b not in txs:
                unresolved += 1
                continue
            hits = [t["from"].lower() for t in (txs[b] or {}).get("transactions", [])
                    if (t.get("to") or "").lower() == w and int(t["value"], 16) > 0]
            if hits:
                sends.append((hits[0], w))
                traced.add(w)
            elif lo0 < b < end:  # funded inside a contract call (multisend/disperse): group by the block it happened in
                by_block[b].append(w)
            else:  # b == lo0: funded before the lookback; b == end: no native coin at any point in the window
                why["older" if b <= lo0 else "unfunded"] += 1
                unresolved += 1
        for b, ws in by_block.items():
            if len(ws) >= 3:
                sends += [(f"contract call in block {b}", w) for w in ws]
                traced.update(ws)
            else:
                unresolved += len(ws)
        unresolved += len(set(wallets) - set(found))
        if timed_out:
            timeout_note(len(traced))
        return sends, unresolved
    pre = int(FUNDING_LOOKBACK_S / bt)
    span = list(range(max(t0 - pre, 0), t0 + 1))[-MAX_FUND_BLOCKS:]
    r["launch"]["funding_method"] = f"block scan of the {len(span)} blocks (~{len(span) * bt / 60:.0f} min) before launch"
    seen = set()
    step = max(getattr(rpc, "batch_size", 20) or 20, 1)
    for i in range(0, len(span), step):
        if out_of_time():
            timed_out = True
            break
        part = span[i:i + step]
        for blk in rpc.batch([("eth_getBlockByNumber", [hex(n), True]) for n in part]):
            for tx in (blk or {}).get("transactions", []):
                to = (tx.get("to") or "").lower()
                if to in lo and int(tx["value"], 16) > 0:
                    sends.append((tx["from"].lower(), to))
                    seen.add(to)
    if timed_out:
        timeout_note(len(seen))
    return sends, len(set(wallets) - seen)


def finish(r, rpc, addr, unit, total, burned, t0, head, logs=None, rug_from=None, too_many=False):
    creator = r.get("creator")
    m = r.get("market") or {}
    pools_all = set(m.get("pairs") or [])
    pair_liq = {k.lower(): v for k, v in (m.get("pair_liquidity") or {}).items()}
    v4_ids_all = {p.lower() for p in (m.get("pairs_any") or []) if len(p) == 66}
    # control checks skip dust pools; a pool DexScreener gives no liquidity figure for is unknown, not dust, so it stays in
    v4_ids_control = {p for p in v4_ids_all if pair_liq.get(p, MIN_POOL_USD) >= MIN_POOL_USD}
    pools = {p for p in pools_all if pair_liq.get(p.lower(), MIN_POOL_USD) >= MIN_POOL_USD}
    scan_from = t0 if t0 is not None else rug_from
    h = {}
    replay_bal = None
    if logs is not None:
        complete = True
    elif t0 is not None and not too_many:
        logs, complete = rpc.logs({"address": addr, "topics": [TRANSFER]}, t0, head, cap=MAX_LOGS)
    else:
        complete = False
    v4_pool_manager = (v4_manager(rpc, addr, v4_ids_all, scan_from, head, logs if complete else None)
                       if scan_from is not None and v4_ids_all else None)
    if scan_from is not None and not pools_all and not v4_ids_all:
        r["notes"].append("DexScreener lists no pools for this coin on this chain yet, so the liquidity checks (who controls it, "
                          "whether it was pulled) had nothing to check, and pools show as 'contract' in the holder list. "
                          "New pools can take a few minutes to be listed; run it again later.")
    if complete:
        replay_bal = replay(logs)
        top = [(a, b / unit) for a, b in replay_bal.most_common(25) if a not in (ZERO,) and b > 0]
        codes = rpc.batch([("eth_getCode", [a, "latest"]) for a, _ in top])
        rows, people = [], 0.0
        pool_like = set(pools_all)
        if v4_pool_manager:
            pool_like.add(v4_pool_manager)
        for (a, amt), c in zip(top, codes):
            kind = ("burn" if a == DEAD else "creator" if a == creator else "pool" if a in pool_like else wallet_kind(c))
            rows.append({"address": a, "share": amt / total, "kind": kind})
        h["top"] = rows[:20]
        h["top10_people_share"] = sum(x["share"] for x in [x for x in rows if x["kind"].startswith("person")][:10])
        h["transfers"] = len(logs)
    elif t0 is None:
        h["note"] = "No launch block to replay from; holder list from GeckoTerminal's summary only."
    else:
        h["note"] = "Too many transfers to replay; holder list from GeckoTerminal's summary only."
    if creator:
        r["creator_share"] = int(rpc.eth_call(addr, "0x70a08231" + word(creator)), 16) / unit / total
    gt = gt_holders(r["chain"], addr)
    if gt:
        h["count"] = gt.get("count")
        h["distribution"] = gt.get("distribution_percentage")
    r["holders"] = h
    pool_targets = set(pools_all)
    if v4_pool_manager:
        pool_targets.add(v4_pool_manager)
    for b in r.get("bundles", []):
        r.setdefault("extra_flags", []).append(b["text"].rstrip(".") + wallet_flow_note(rpc, addr, b["wallets"], total, unit,
                                                                                         complete, logs, replay_bal, pool_targets))
    for c in r.get("clusters", []):
        c["flow_note"] = wallet_flow_note(rpc, addr, c["wallets"], total, unit,
                                          complete, logs, replay_bal, pool_targets)
        c["held_share"] = sum(max((replay_bal or {}).get(w, 0), 0) for w in c["wallets"]) / unit / total if complete and replay_bal else c.get("held_share", 0)
    if scan_from is not None:
        start = m.get("first_pair_ts") or (r.get("launch") or {}).get("ts") or 0
        lp_flags, lp_notes = lp_control(rpc, pools, creator, scan_from, head)
        r.setdefault("extra_flags", []).extend(lp_flags)
        r["notes"].extend(lp_notes)
        v4_flags, v4_notes = lp_control_v4(rpc, addr, v4_ids_all, creator, scan_from, head, start,
                                           v4_pool_manager, v4_ids_control, pair_liq)
        r.setdefault("extra_flags", []).extend(v4_flags)
        r["notes"].extend(v4_notes)
        r.setdefault("extra_flags", []).extend(liquidity_pulls(rpc, pools_all, addr, creator, scan_from, head, start))
    r["flags"] = flags(r)
    r["cant_check"] = [
        "Funding sent as tokens (USDC/USDT) rather than the native coin.",
        "Funding through a bridge or an exchange withdrawal (the funder then looks like a busy wallet and is skipped).",
        "Who controls the creator wallet, or whether its tokens are promised to an airdrop or vesting.",
        "Contract risks (mint, blacklist, tax, upgradeable proxy); check the contract separately.",
        "LP control and liquidity pulls cover Uniswap v2/v3/v4-style pools; Aerodrome and other non-Uniswap designs are not covered yet.",
        "Pools DexScreener doesn't list. Pool addresses, pool age and liquidity figures come from DexScreener, not from the chain.",
        "On v3/v4 pools, shares are of liquidity units; positions in different price ranges aren't directly comparable.",
        "Smart-contract wallets (Safe multisigs, Coinbase Smart Wallet and similar) are labeled 'contract', so a person using "
        "one isn't counted among early buyers, funders or 'person' holders. 'person' means a plain wallet, which can also be a bot or an exchange.",
        "'Creator' means the wallet that sent the transaction that first minted the token. On launchpads that can be the "
        "launchpad's own wallet, not the person behind the coin.",
    ]
    return r


def human_span(seconds):
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min" if seconds < 7200 else f"{seconds / 3600:.1f}h"


def fmt(r):
    m = r.get("market") or {}
    s = r["supply"]
    lines = [f"Launch check: {r['symbol']} on {r['chain']} · {r['address']}"]
    if m:
        liq = "unknown (DexScreener gives no figure)" if m.get("liquidity") is None else f"${m['liquidity']:,.0f}"
        lines.append(f"Price ${m['price']:.8g} · liquidity {liq} · DexScreener FDV ${m['dex_fdv'] or 0:,.0f}"
                     + (f" · real FDV ${s['real_fdv']:,.0f}" if s.get("real_fdv") else ""))
    lines.append(f"Supply {s['total']:,.0f} · burned {s['burned']:,.0f}")
    L = r.get("launch")
    if L:
        lines.append(f"{'Created in slot' if r['chain'] == 'solana' else 'Minted block'} {L['block']} ({time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(L['ts']))}) by {L['creator']}"
                     f" · creator holds {r.get('creator_share', 0):.1%} now")
        if L.get("first_buy_ts"):
            lines.append(f"Trading opened {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(L['first_buy_ts']))} "
                         f"(block {L['first_buy_block']}), {human_span(L['first_buy_ts'] - L['ts'])} after the mint")
        lines.append(f"First {L.get('window_s', LAUNCH_WINDOW_S)}s of trading: {L['early_buyers']} wallets took {L['early_bought_share']:.1%} of supply")
    lines.append("")
    lines.append("Red flags:" if r["flags"] else
                 "Red flags: none found by these checks (that is not the same as safe)." if L else
                 "Red flags: none found, but the launch checks didn't run (see the note below), so this says little.")
    lines += [f"- {f}" for f in r["flags"]]
    h = r["holders"]
    if h.get("top"):
        lines.append("")
        lines.append(f"Top holders (replayed {h['transfers']:,} transfers):" if "transfers" in h
                     else "Top holders (largest 20 token accounts):")
        lines += [f"  {x['address']}  {x['share']:6.2%}  {x['kind']}" for x in h["top"][:12]]
    if h.get("count"):
        dist = ", ".join(f"{k.replace('_', '-')} {float(v):.1f}%" for k, v in (h.get("distribution") or {}).items() if v is not None)
        lines.append(f"Holders {h['count']:,}" + (f" · share by rank: {dist}" if dist else "") + " (GeckoTerminal, pools included)")
    if h.get("note"):
        lines.append(h["note"])
    for n in r["notes"]:
        lines.append(f"Note: {n}")
    lines.append("")
    lines.append("Couldn't check:")
    lines += [f"- {c}" for c in r["cant_check"]]
    return "\n".join(lines)


FREE_KEYS = ("Free keys fix this: PUBLICNODE_TOKEN from allnodes.com/publicnode (Ethereum, Base, BNB, HyperEVM) "
             "or HELIUS_API_KEY from helius.dev (Solana). Or set RPC_<CHAIN> to your own endpoint.")


def main(argv):
    try:
        run(argv)
    except urllib.error.HTTPError as e:
        host = urllib.parse.urlparse(e.filename or "").netloc or "the server"  # host only: the full URL can carry a key
        if e.code in (401, 404) and ("publicnode.com" in host or "helius" in host):  # 403 is how publicnode throttles
            raise SystemExit(f"{host} rejected the request (HTTP {e.code}). If you set PUBLICNODE_TOKEN, HELIUS_API_KEY "
                             "or RPC_<CHAIN>, check the value is right.")
        raise SystemExit(f"{host} kept refusing requests (HTTP {e.code}), usually rate limiting on a public endpoint. "
                         + FREE_KEYS)
    except http.client.InvalidURL:
        raise SystemExit("That address or RPC URL has characters that can't go in a web address. Check it and try again.")
    except (OSError, http.client.HTTPException) as e:
        if "--debug" in argv:
            raise
        raise SystemExit(f"Network problem: {type(e).__name__}: {getattr(e, 'reason', e)}. Check your connection and run it again.")
    except RuntimeError as e:
        if "--debug" in argv:
            raise
        if "rate" in str(e).lower():
            raise SystemExit(f"The RPC kept rate-limiting ({e}). " + FREE_KEYS)
        raise SystemExit(f"The RPC returned an error this check can't work around: {e}. Run it again, "
                         "or set RPC_<CHAIN> to another endpoint. " + FREE_KEYS)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, ZeroDivisionError) as e:
        if "--debug" in argv:
            raise
        raise SystemExit(f"launch-check hit data it didn't expect ({type(e).__name__}: {e}), so there is no result. "
                         "Nothing was found either way. Run it again with --debug for details.")


def run(argv):
    chain = None
    if "--chain" in argv:
        i = argv.index("--chain")
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            raise SystemExit("--chain needs a value: " + ", ".join(CHAINS))
        chain = argv[i + 1].lower()
        argv = argv[:i] + argv[i + 2:]
        if chain not in CHAINS:
            raise SystemExit(f"Unknown chain '{chain}'. Use one of: " + ", ".join(CHAINS))
    args = [a for a in argv if not a.startswith("--")]
    if not args or not args[0].strip():
        raise SystemExit(__doc__)
    addr = args[0].strip()
    if EVM_ADDRESS.fullmatch(addr):
        if chain == "solana":
            raise SystemExit(f"{addr} is an EVM address, not a Solana one. Drop --chain solana or pass the Solana mint address.")
    elif SOLANA_ADDRESS.fullmatch(addr):
        if chain and chain != "solana":
            raise SystemExit(f"{addr} looks like a Solana address, but --chain is {chain}.")
    else:
        raise SystemExit(f"'{addr}' isn't a token address. Pass the contract address: 0x followed by 40 hex characters, "
                         "or a Solana mint address (32 to 44 base58 characters).")
    r = check(addr, chain)
    print(json.dumps(r, indent=1) if "--json" in argv else fmt(r))


if __name__ == "__main__":
    main(sys.argv[1:])
