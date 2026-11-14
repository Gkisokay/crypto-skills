"""Solana side of the launch check: mint/freeze authority, creator, same-slot and first-minutes buyers, who funded
them, and labeled top holders. Called by launch_check.py; returns the same report shape as the EVM check.

RPC: $RPC_SOLANA, else Helius with $HELIUS_API_KEY, else the public RPC (rate-limits fast).
"""
import collections
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
import snipers
import solana_liquidity

SYSTEM = "11111111111111111111111111111111"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MAX_SIG_PAGES = 30   # 1,000 signatures each; older history than this and the launch checks are skipped
MAX_EARLY_TXS = 150  # first-minutes transactions read in full
MAX_WALLETS = 60     # early buyers whose funding is traced
WORKERS = 4          # Helius free allows 10 req/s

# PDA math is inlined so this skill works on its own, with no dependencies
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
P = 2**255 - 19
D = -121665 * pow(121666, P - 2, P) % P


def b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    return n.to_bytes(32, "big")


def b58encode(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def on_curve(b):
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= P:
        return False
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    return x2 == 0 or pow(x2, (P - 1) // 2, P) == 1


def pda(seeds, program):
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58encode(h)


def rpc_url():
    if os.environ.get("RPC_SOLANA"):
        return os.environ["RPC_SOLANA"]
    key = os.environ.get("HELIUS_API_KEY")
    return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else "https://api.mainnet-beta.solana.com"


def owner_deltas(tx, mint):
    """Token change per owner wallet in one transaction, from pre/post token balances."""
    d = collections.Counter()
    meta = (tx or {}).get("meta") or {}
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(key) or []:
            if b.get("mint") == mint and b.get("owner"):
                d[b["owner"]] += sign * float(b["uiTokenAmount"].get("uiAmount") or 0)
    return d


def launch_supply(total, txs, mint, kinds):
    """Launch denominator: current supply or what was minted/deposited at launch, whichever is larger."""
    minted = 0.0
    bal = collections.Counter()
    prog_peak = 0.0
    for tx in sorted(txs, key=lambda x: (x.get("slot") or 0, x.get("blockTime") or 0)):
        d = owner_deltas(tx, mint)
        pos = sum(x for x in d.values() if x > 0)
        neg = sum(-x for x in d.values() if x < 0)
        if pos > neg:
            minted += pos - neg
        for w, x in d.items():
            bal[w] = max(bal[w] + x, 0.0)
        prog_peak = max(prog_peak, sum(v for w, v in bal.items() if kinds.get(w) == "program"))
    return max(total, minted, prog_peak)


def early_flows(txs, mint, kinds, t0):
    """Buys from program owners, hand-offs from people, and each buyer's peak net in the launch window."""
    running, peak = collections.Counter(), collections.Counter()
    slot_of, first_ts = {}, {}
    handoffs = collections.defaultdict(collections.Counter)
    hand_times = collections.defaultdict(list)
    max_live = 0.0
    for tx in sorted(txs, key=lambda x: (x.get("slot") or 0, x.get("blockTime") or 0)):
        d = owner_deltas(tx, mint)
        pos = {w: x for w, x in d.items() if kinds.get(w, "program") == "person" and x > 0}
        neg_prog = sum(-x for w, x in d.items() if kinds.get(w, "program") == "program" and x < 0)
        neg_people = {w: -x for w, x in d.items() if kinds.get(w, "program") == "person" and x < 0}
        pos_sum = sum(pos.values())
        neg_people_sum = sum(neg_people.values())
        source = neg_prog + neg_people_sum
        matched = min(pos_sum, source)
        buy_in = collections.Counter()
        if matched > 0 and source > 0 and pos_sum > 0:
            prog_share = neg_prog / source
            ts = tx.get("blockTime") or t0
            for recv, amt in pos.items():
                recv_matched = matched * amt / pos_sum
                recv_buy = recv_matched * prog_share
                if recv_buy > 0:
                    buy_in[recv] += recv_buy
                recv_hand = recv_matched - recv_buy
                if recv_hand > 0 and neg_people_sum > 0:
                    for snd, out_amt in neg_people.items():
                        part = recv_hand * out_amt / neg_people_sum
                        if part > 0:
                            handoffs[snd][recv] += part
                            hand_times[snd].append(ts)
        for w, x in d.items():
            if kinds.get(w, "program") != "person":
                continue
            prev = running[w]
            running[w] = max(running[w] + buy_in.get(w, 0.0) - max(-x, 0.0), 0.0)
            if buy_in.get(w, 0.0) > 0 and running[w] > prev and w not in slot_of:
                slot_of[w] = tx.get("slot") or 0
                first_ts[w] = tx.get("blockTime") or t0
            peak[w] = max(peak[w], running[w])
        max_live = max(max_live, sum(running.values()))
    return peak, slot_of, first_ts, handoffs, hand_times, max_live


def early_buys(txs, mint, people, t0):
    kinds = collections.defaultdict(lambda: "program")
    for w in people:
        kinds[w] = "person"
    bought, slot_of, first_ts, _, _, _ = early_flows(txs, mint, kinds, t0)
    return bought, slot_of, first_ts


def fee_payer(tx):
    keys = tx["transaction"]["message"]["accountKeys"]
    k = keys[0]
    return k["pubkey"] if isinstance(k, dict) else k


def launch_sigs(call, url, mint, window_s=180):
    if "helius" in url:
        try:
            # sniped launches fill whole pages with failed buys, so keep paging until the launch window is covered
            out, token = [], None
            for _ in range(MAX_SIG_PAGES):
                opts = {"transactionDetails": "signatures", "sortOrder": "asc", "limit": 1000}
                if token:
                    opts["paginationToken"] = token
                page = call("getTransactionsForAddress", [mint, opts]) or {}
                data = page.get("data") or []
                out += data
                token = page.get("paginationToken")
                if len(data) < 1000 or not token or (data[-1].get("blockTime") or 0) > (out[0].get("blockTime") or 0) + window_s:
                    break
            return [x for x in out if not x.get("err")], False
        except RuntimeError:
            pass
    sigs, before = [], None
    for _ in range(MAX_SIG_PAGES):
        page = call("getSignaturesForAddress", [mint, dict({"limit": 1000}, **({"before": before} if before else {}))])
        sigs += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    else:
        return None, True
    return [s for s in reversed(sigs) if not s.get("err")], False


def bundle_flags_with_holding(cc, t0, buys, total, holds, held=None):
    out = []
    for g in cc.bundle_groups(t0, buys, total, held):
        if holds is None:
            out.append(g["text"])
            continue
        hold = sum(holds.get(w, 0.0) for w in g["wallets"]) / total if total else 0.0
        out.append(g["text"].rstrip(".") + f"; they hold {hold:.1%} now.")
    return out


def handoff_flags(sender, bought, handoffs, hand_times, total, holds):
    if total <= 0 or bought.get(sender, 0.0) <= 0:
        return []
    moved = [(w, a) for w, a in (handoffs.get(sender) or {}).items() if a > 0 and bought.get(w, 0.0) <= 0]
    if len(moved) < 5:
        return []
    span = 0.0
    times = hand_times.get(sender) or []
    if times:
        span = max(times) - min(times)
    s = f"{span:.2f}".rstrip("0").rstrip(".")
    recv = sum(a for _, a in moved) / total
    each = recv / len(moved)
    text = (f"Wallet {sender} bought {bought[sender] / total:.1%} and sent tokens on to {len(moved)} wallets that hadn't bought "
            f"within {s} s (~{each:.2%} each); one buyer splitting a position like this makes the holder count look "
            "larger and a big buy look smaller")
    if holds is None:
        return [text + "."]
    hold_now = sum(holds.get(w, 0.0) for w, _ in moved) / total
    return [text + f"; the receivers hold {hold_now:.1%} now."]


def check_solana(cc, mint, m):
    rpc = cc.Rpc(rpc_url(), 0, WORKERS, 1, rps=None if os.environ.get("RPC_SOLANA") else 9)  # Helius free: 10 req/s
    call = rpc.call
    r = {"address": mint, "chain": "solana", "market": m, "notes": [], "extra_flags": []}
    if "helius" not in rpc.url and "RPC_SOLANA" not in os.environ:
        r["notes"].append("Using Solana's public RPC, which rate-limits fast; set HELIUS_API_KEY (free at helius.dev) for reliable checks.")

    acct = call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
    data = (acct or {}).get("data")
    info = (data.get("parsed") or {}).get("info") if isinstance(data, dict) else None
    if not info or "supply" not in info or "decimals" not in info:  # a wallet, a token account, a program, or nothing
        raise SystemExit(f"{mint} isn't a token mint on Solana. Pass the token's mint address, not a wallet or pool address.")
    dec = info["decimals"]
    total = int(info["supply"]) / 10 ** dec
    if total <= 0:
        raise SystemExit(f"{mint} reports a total supply of 0; there is nothing to check.")
    r["symbol"] = m["symbol"] if m else mint[:6]
    r["supply"] = {"total": total, "burned": 0.0, "real_fdv": cc.real_fdv(m["price"], total, 0) if m and m["price"] else None}
    if info.get("mintAuthority"):
        r["extra_flags"].append(f"Mint authority is still set ({info['mintAuthority']}): more tokens can be created.")
    if info.get("freezeAuthority"):
        r["extra_flags"].append(f"Freeze authority is still set ({info['freezeAuthority']}): holders' tokens can be frozen.")
    exts = [e.get("extension") for e in info.get("extensions") or []]
    risky = [e for e in exts if e in ("transferFeeConfig", "transferHook", "permanentDelegate", "pausableConfig")]
    if risky:
        r["extra_flags"].append(f"Token-2022 extensions that can take or block tokens: {', '.join(risky)}.")

    curve = pda([b"bonding-curve", b58decode(mint)], PUMP)
    pools = set((m or {}).get("pairs_any") or [])

    # creation: oldest signatures via Helius fast path, else walk backwards to oldest
    sigs, capped = launch_sigs(call, rpc.url, mint, cc.LAUNCH_WINDOW_S)
    if capped:
        r["notes"].append(f"More than {MAX_SIG_PAGES * 1000:,} transactions, so the launch wasn't reached; launch checks skipped.")
        sigs = None

    creator, txs, groups, bought = None, [], [], {}
    sol_checks = {"flags": [], "notes": [], "cant_check": []}
    if sigs:
        first = call("getTransaction", [sigs[0]["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        creator = fee_payer(first)
        t0, slot0 = first["blockTime"], first["slot"]
        r["creator"] = creator
        r["launch"] = {"block": slot0, "ts": t0, "creator": creator, "block_time_s": 0.4}
        in_window = [s for s in sigs if (s.get("blockTime") or t0) <= t0 + cc.LAUNCH_WINDOW_S]
        early = [s["signature"] for s in in_window[:MAX_EARLY_TXS]]
        if len(in_window) > MAX_EARLY_TXS:  # a busy launch: say how much of the window was actually read
            r["launch"]["window_s"] = max((in_window[MAX_EARLY_TXS - 1].get("blockTime") or t0) - t0, 1)
            r["notes"].append(f"The launch had {len(in_window):,} successful transactions in its first {cc.LAUNCH_WINDOW_S} s; "
                              f"only the first {MAX_EARLY_TXS} were read, covering {r['launch']['window_s']} s.")
        with ThreadPoolExecutor(WORKERS) as ex:
            txs = list(ex.map(lambda s: call("getTransaction", [s, {"encoding": "jsonParsed",
                                                                   "maxSupportedTransactionVersion": 1}]), early))
        owners = sorted({owner for tx in txs for owner in owner_deltas(tx, mint)})
        kinds = account_kinds(call, owners)
        kinds[curve] = "program"
        for p in pools:
            kinds[p] = "program"
        launch_total = launch_supply(total, txs, mint, kinds)
        bought, slot_of, first_ts, handoffs, hand_times, peak_held = early_flows(txs, mint, kinds, t0)
        people = [w for w in sorted({w for w in owners if kinds.get(w) == "person"}) if bought.get(w, 0) > 0]
        r["launch"]["early_buyers"] = len(people)
        # the most early buyers held at once; per-wallet peaks can sum higher when tokens are resold in the window
        r["launch"]["early_bought_share"] = peak_held / launch_total if launch_total else 0
        buys = {w: (bought[w], first_ts.get(w, t0)) for w in people if bought.get(w, 0) > 0}
        profiled, capped = snipers.profile_wallets_by_take({w: bought[w] for w in people}, max_wallets=snipers.MAX_PROFILED_WALLETS)
        profiles = snipers.solana_wallet_profiles(call, profiled, t0)
        if capped:
            r["notes"].append(f"Sniper/fresh profiling ran on the top {snipers.MAX_PROFILED_WALLETS} of {len(people)} early wallets by size.")
        same_slot = [w for w in people if slot_of[w] == slot0]
        if len(same_slot) >= 3:
            r["extra_flags"].append(snipers.same_block_take_flag(same_slot, t0, buys, launch_total, profiles))
        groups = cc.bundle_groups(t0, buys, launch_total, peak_held)
        groups = snipers.relabel_bundle_groups(groups, t0, buys, launch_total, profiles, held=peak_held)
        fan = [s for s in sorted(handoffs) if handoff_flags(s, bought, handoffs, hand_times, launch_total, None)]
        holds = {}
        try:
            need = {x for g in groups for x in g["wallets"]}
            for s in fan:
                need.update(w for w, a in handoffs.get(s, {}).items() if a > 0 and bought.get(w, 0.0) <= 0)
            for w in need:
                holds[w] = holding(call, w, mint)
        except RuntimeError:
            holds = None
        if groups:
            r["extra_flags"] += bundle_flags_with_holding(cc, t0, buys, launch_total, holds, peak_held)
        for s in fan:
            r["extra_flags"] += handoff_flags(s, bought, handoffs, hand_times, launch_total, holds)
        people_by_size = sorted(people, key=lambda w: bought[w], reverse=True)
        sends, unresolved = first_funders(call, people_by_size[:MAX_WALLETS])
        r["launch"]["funding_method"] = "each early buyer's first transaction (who paid for it)"
        if unresolved:
            r["notes"].append(f"{unresolved} early buyers aren't fresh wallets (1,000+ past transactions), so their funding wasn't traced.")
        cl = cc.funder_clusters(sends, creator)
        busy = {f: len(call("getSignaturesForAddress", [f, {"limit": 1000}])) >= 1000 for f in cl if f != creator}
        r["clusters"] = []
        for f, ws in cl.items():
            if busy.get(f):
                r["notes"].append(f"{f} funded {len(ws)} early buyers but has 1,000+ transactions (likely an "
                                  "exchange, faucet or bot), so it isn't counted as a cluster.")
                continue
            bought_share = sum(bought[w] for w in ws) / launch_total if launch_total else 0
            if bought_share < cc.MIN_CLUSTER_SHARE and f != creator:
                continue
            held = sum(holding(call, w, mint) for w in ws)
            r["clusters"].append({"funder": f, "wallets": ws, "bought_share": bought_share,
                                  "held_share": held / launch_total if launch_total else 0})
        r["creator_share"] = holding(call, creator, mint) / total
        bundle_wallets = sorted({w for g in groups for w in g.get("wallets", [])})
        sol_checks = solana_liquidity.apply_solana_checks(
            call, mint, pools, creator, t0, total, bought, bundle_wallets, r.get("clusters", []), txs,
            (m or {}).get("pair_liquidity") or {}
        )
        r["extra_flags"].extend(sol_checks["flags"])
        r["notes"].extend(sol_checks["notes"])

    # top holders: the 20 largest token accounts, labeled by who owns them
    largest = call("getTokenLargestAccounts", [mint])["value"]
    accts = call("getMultipleAccounts", [[a["address"] for a in largest], {"encoding": "jsonParsed"}])["value"]
    owners = [((x or {}).get("data") or {}).get("parsed", {}).get("info", {}).get("owner") for x in accts]
    kinds = account_kinds(call, [o for o in owners if o])
    rows = []
    for a, o in zip(largest, owners):
        amt = float(a.get("uiAmount") or 0)
        kind = ("pump.fun curve" if o == curve else "creator" if o == creator else "pool" if o in pools
                else kinds.get(o, "unknown"))
        rows.append({"address": o or a["address"], "share": amt / total if total else 0, "kind": kind})
    h = {"top": rows, "top10_people_share": sum(x["share"] for x in [x for x in rows if x["kind"] == "person"][:10])}
    gt = cc.gt_holders("solana", mint)
    if gt:
        h["count"] = gt.get("count")
        h["distribution"] = gt.get("distribution_percentage")
    r["holders"] = h
    r.setdefault("clusters", [])
    r["flags"] = cc.flags(r)
    r["cant_check"] = [
        "Funding older than a wallet's first transaction, or routed through an exchange or a bridge.",
        "Wallets that already had 1,000+ transactions before buying (not traced).",
        "Who controls the creator wallet, or whether its tokens are promised to an airdrop or vesting.",
        "Who controls Solana pool liquidity isn't checked yet; pulls are.",
        "'Creator' means the wallet that paid for the oldest transaction found for this mint; on launchpads other than "
        "pump.fun that can be the launchpad's own wallet.",
        "Only the first 3 minutes after creation are read (up to 150 transactions), so a busy launch is only partly covered.",
    ] + sol_checks["cant_check"]
    return r


def account_kinds(call, wallets):
    """person = on-curve and empty/System-owned; off-curve or program-owned addresses are program wallets."""
    out = {}
    for i in range(0, len(wallets), 100):
        part = wallets[i:i + 100]
        on_curve_wallets = []
        for w in part:
            try:
                if not on_curve(b58decode(w)):
                    out[w] = "program"
                    continue
            except ValueError:
                pass
            on_curve_wallets.append(w)
        if not on_curve_wallets:
            continue
        accs = call("getMultipleAccounts", [on_curve_wallets, {"encoding": "base64"}])["value"]
        for w, a in zip(on_curve_wallets, accs):
            out[w] = "person" if a is None or a.get("owner") == SYSTEM else "program"
    return out


def holding(call, wallet, mint):
    res = call("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])["value"]
    return sum(float(x["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0) for x in res)


def first_funders(call, wallets):
    """(payer, wallet) for each fresh wallet's oldest transaction, when someone else paid for it."""
    def one(w):
        page = call("getSignaturesForAddress", [w, {"limit": 1000}])
        if len(page) >= 1000:
            return None
        tx = call("getTransaction", [page[-1]["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        payer = fee_payer(tx)
        return (payer, w) if payer != w else ("", w)
    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(one, wallets))
    sends = [x for x in res if x and x[0]]
    return sends, sum(1 for x in res if x is None)
