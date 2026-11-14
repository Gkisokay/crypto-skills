"""Classify early takers as bot snipers, fresh-wallet bundles, or unknown."""

import collections
from concurrent.futures import ThreadPoolExecutor

PROFILE_WORKERS = 6

SNIPER_TX_COUNT = 1000
FRESH_MAX_TX_COUNT = 20
FRESH_MAX_AGE_S = 15 * 60
SNIPER_MINT_COUNT = 8
MAX_PROFILED_WALLETS = 60


def _seconds_text(seconds):
    s = max(float(seconds), 0.0)
    if abs(s - round(s)) < 0.05:
        return str(int(round(s)))
    return f"{s:.2f}".rstrip("0").rstrip(".")


def classify_wallet(profile, launch_ts):
    """Return bot-sniper, fresh-bundle, or unknown for one wallet profile."""
    profile = profile or {}
    tx_count = profile.get("tx_count")
    mint_count = profile.get("distinct_mints")
    if tx_count is not None and tx_count >= SNIPER_TX_COUNT:
        return "bot-sniper"
    if mint_count is not None and mint_count >= SNIPER_MINT_COUNT and (tx_count is None or tx_count >= 200):
        return "bot-sniper"
    first_seen = profile.get("first_seen_ts")
    age_s = None if first_seen is None or launch_ts is None else max(0, launch_ts - first_seen)
    funded_for_launch = bool(profile.get("funded_for_launch"))
    fresh_signal = funded_for_launch or bool(profile.get("fresh_signal")) or (tx_count is not None and tx_count <= 3)
    if tx_count is not None and tx_count <= FRESH_MAX_TX_COUNT and fresh_signal and age_s is not None and age_s <= FRESH_MAX_AGE_S:
        return "fresh-bundle"
    return "unknown"


def classify_wallets(wallets, profiles, launch_ts):
    """Count classifier labels for a wallet list."""
    out = collections.Counter()
    for w in wallets:
        out[classify_wallet((profiles or {}).get(w), launch_ts)] += 1
    return out


def early_take_flag(open_ts, buys, total, wallets, profiles, held=None):
    """Flag text for early takers with sniper vs fresh-wallet counts."""
    if not wallets or total <= 0:
        return ""
    took = held / total if held is not None else sum((buys.get(w) or (0.0, open_ts))[0] for w in wallets) / total
    times = [float((buys.get(w) or (0.0, open_ts))[1]) for w in wallets]
    span_s = max(times) - float(open_ts) if times else 0.0
    profiled = [w for w in wallets if w in (profiles or {})]
    counts = classify_wallets(profiled, profiles, open_ts)
    when = "in the launch block" if span_s < 1 else f"in the first {_seconds_text(span_s)} s"
    if mostly_snipers(counts, len(profiled)):
        return (f"Snipers: {len(wallets)} wallets took {took:.1%} {when}; {counts['bot-sniper']} of the "
                f"{len(profiled)} profiled are sniper bots (1,000+ txs).")
    return (f"Likely bundle: {len(wallets)} wallets took {took:.1%} {when}; of the {len(profiled)} profiled, "
            f"{counts['bot-sniper']} look like sniper bots (1,000+ txs) and "
            f"{counts['fresh-bundle']} are fresh wallets made for this launch.")


def mostly_snipers(counts, profiled):
    return profiled > 0 and counts["bot-sniper"] * 2 > profiled


def sniper_suffix(wallets, profiles, launch_ts):
    """Clause for the 3-minute early-buyers line, so sniper-heavy launches don't read as a bundle."""
    profiled = [w for w in wallets if w in (profiles or {})]
    counts = classify_wallets(profiled, profiles, launch_ts)
    if not profiled:
        return ""
    if mostly_snipers(counts, len(profiled)):
        return f" They are mostly sniper bots: {counts['bot-sniper']} of the {len(profiled)} profiled have 1,000+ txs."
    return (f" Of the {len(profiled)} profiled, {counts['bot-sniper']} are sniper bots (1,000+ txs) and "
            f"{counts['fresh-bundle']} are fresh wallets made for this launch.")


def same_block_take_flag(wallets, open_ts, buys, total, profiles):
    return early_take_flag(open_ts, buys, total, wallets, profiles)


def profile_wallets_by_take(took_by_wallet, max_wallets=MAX_PROFILED_WALLETS):
    ranked = [w for w, _ in sorted((took_by_wallet or {}).items(), key=lambda x: x[1], reverse=True)]
    if len(ranked) <= max_wallets:
        return ranked, False
    return ranked[:max_wallets], True


def relabel_bundle_groups(groups, open_ts, buys, total, profiles, held=None):
    """Replace bundle wording with sniper/fresh wording when profiles are available."""
    if not profiles:
        return groups
    out = []
    for g in groups or []:
        text = g.get("text") or ""
        if text.startswith("Likely bundle:") or "first 3 minutes of trading" in text:
            g = dict(g)
            g["text"] = early_take_flag(open_ts, buys, total, g.get("wallets") or [], profiles, held=held)
        out.append(g)
    return out


def evm_wallet_profiles(rpc, wallets):
    """Profiles for EVM early takers using nonce as tx count."""
    wallets = sorted(set(wallets))
    if not wallets:
        return {}
    nonces = rpc.batch([("eth_getTransactionCount", [w, "latest"]) for w in wallets])
    out = {}
    for w, n in zip(wallets, nonces):
        try:
            out[w] = {"tx_count": int(n, 16)}
        except (TypeError, ValueError):
            out[w] = {}
    return out


def solana_wallet_profiles(call, wallets, launch_ts, funded_wallets=None):
    """Profiles for Solana early takers using signature pages as tx count."""
    funded_wallets = funded_wallets or set()
    wallets = sorted(set(wallets))
    with ThreadPoolExecutor(PROFILE_WORKERS) as ex:
        pages = list(ex.map(lambda w: call("getSignaturesForAddress", [w, {"limit": 1000}]) or [], wallets))
    out = {}
    for w, page in zip(wallets, pages):
        first_seen = page[-1].get("blockTime") if page and len(page) < 1000 else None
        out[w] = {"tx_count": len(page), "first_seen_ts": first_seen, "funded_for_launch": w in funded_wallets}
    return out
