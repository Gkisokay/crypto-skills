"""Main-risk verdict and stable flag codes for launch-check reports."""
import re


class Rule:
    def __init__(self, code, pattern, severity, verdict):
        self.code = code
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.severity = severity
        self.verdict = verdict


# Ordered rows: add one row for each new flag family.
# Severity order (1 highest): liquidity pulled/can pull, honeypot/sell blocked, bundle/farm, taxes/mint/freeze, rest.
RULES = [
    Rule("LP_PULLED", r"^liquidity pulled\b", 1, "liquidity was pulled from a pool"),
    Rule("LP_CAN_PULL", r"^liquidity can be pulled\b", 1, "one wallet can remove most pool liquidity"),
    Rule("HONEYPOT_OR_SELL_BLOCKED", r"^selling reverts in simulation|^permanent delegate is set|^transfer hook is enabled"
         r"|^pausable extension is enabled|^default account state is frozen|^freeze authority is still set", 2,
         "selling can fail or be blocked"),
    Rule("SNIPER_CLUSTER", r"^snipers:|they are mostly sniper bots", 3, "sniper bots took a large share at launch"),
    Rule("BUNDLE", r"^likely bundle:|^early group:|of supply in the first .+ of trading", 3, "a group of wallets took a large share at launch"),
    Rule("WALLET_FARM", r"in near-equal slices", 3, "a wallet farm holds a large share in equal slices"),
    Rule("TOKEN_CONTROL_TRAP", r"^selling loses about|^tax getter|^transfers pay|^transfer fee authority|^transfers use a fixed fee"
         r"|^mint authority is still set|^owner 0x[0-9a-f]+ can call|getter .* reports .* of total supply|^has .* functions;", 4,
         "the token's contract can tax, mint or change balances"),
    Rule("CREATOR_EXIT_TIMING", r"^the creator wallet \S+ (?:sold|moved) .* later", 4, "the creator sold or moved its tokens right after launch"),
    Rule("FAKE_MARKET_CAP", r"^fdv is .* while (?:listed liquidity|real money in its pools) is", 5,
         "the market cap is far above what its liquidity supports"),
    Rule("COIN_SIDE_POOL", r"^the pool is mostly this coin's own tokens", 5, "the pool's listed value is mostly the coin itself, not real money"),
    Rule("FAKE_VOLUME", r"^recent pool-facing flow is concentrated", 5, "volume comes mostly from a few wallets trading back and forth"),
    Rule("COPYCAT_NAME", r"^same (?:name|ticker|name and ticker) as|lookalike", 5, "it copies the name or ticker of a bigger coin"),
    Rule("CREATOR_HISTORY", r"^this creator wallet \S+ launched", 5, "the creator is a serial launcher"),
    Rule("CREATOR_HOLDS", r"^creator wallet holds\b", 5, "the creator wallet still holds a large supply share"),
    Rule("FUNDER_CLUSTER", r"funded \d+ first-minutes buyers|funded \d+ early buyers", 5,
         "one wallet funded many early buyers"),
    Rule("HANDOFF", r"the receivers hold", 5, "tokens were handed out to fresh wallets right after launch"),
    Rule("FDV_MISMATCH", r"^dexscreener fdv\b", 5, "DexScreener's FDV doesn't match the on-chain supply"),
    Rule("TOP10_PEOPLE", r"^top 10 people\b", 5, "a few plain wallets hold much of the supply"),
]


# A pull flags from 50% (launch_check.lp_control_v4); a creator that took at least this share of a pool's peak liquidity
# leads the verdict, even when it is only a note because the pool still shows liquidity (it may sit in a hook).
CREATOR_PULL_LEAD = 0.90
CREATOR_PULL = re.compile(r"^(?:Liquidity pulled: )?The creator \S+ removed (?P<pct>[\d.]+)% of pool \S+'s peak liquidity"
                          r"(?: positions)?,? (?P<span>.+?) after .+?"
                          r"(?:, but the pool still shows (?P<shows>\$[\d,]+) of liquidity|\. Last removal)")


def _classify(flag_text):
    for rule in RULES:
        if rule.pattern.search(flag_text):
            return rule
    return None


def flag_codes(flags):
    coded = []
    for text in flags or []:
        rule = _classify(text)
        coded.append({"code": rule.code if rule else "FLAG_OTHER", "text": text})
    return coded


def primary_flag(flags):
    if not flags:
        return {"code": "NONE", "severity": None, "text": "", "verdict": ""}

    best = None
    for i, text in enumerate(flags):
        rule = _classify(text)
        if rule is None:
            continue
        row = {"code": rule.code, "severity": rule.severity, "text": text,
               "verdict": f"Main risk: {rule.verdict} ({text})"}
        if best is None or row["severity"] < best["severity"] or (row["severity"] == best["severity"] and i < best["index"]):
            row["index"] = i
            best = row
    if best:
        best.pop("index", None)
        return best
    return {"code": "FLAG_OTHER", "severity": 5, "text": flags[0], "verdict": f"Main risk: {flags[0]}"}


def creator_pull(flags, notes=()):
    """The largest creator pull of at least CREATOR_PULL_LEAD, as (percent, match, text), or None; a tie goes to the flag."""
    pulls = [(float(m["pct"]), m, text) for text in list(flags or []) + list(notes or [])
             if (m := CREATOR_PULL.match(text))]
    pulls = [p for p in pulls if p[0] >= CREATOR_PULL_LEAD * 100]
    return max(pulls, key=lambda p: (p[0], p[1]["shows"] is None)) if pulls else None


def main_verdict(flags, launch_checks_ran=True, notes=()):
    pull = creator_pull(flags, notes)
    if pull:
        pct, m, text = pull
        when = "" if m["span"] == "time unknown" else f" {m['span']} after that pool opened"
        rest = [f for f in flags or [] if f != text]
        if m["shows"]:  # the note: the pool still shows liquidity, so it may not have left
            lead = (f"Main risk: the creator removed {pct:g}% of its liquidity position{when}; the pool still shows "
                    f"{m['shows']} (launch-check: likely held by its hook).")
        else:
            lead = f"Main risk: the creator pulled {pct:g}% of the pool's liquidity{when}."
        return lead + (" Next: " + primary_flag(rest)["verdict"].removeprefix("Main risk: ") if rest else "")
    if not flags:
        if launch_checks_ran:
            return "No red flags found by these checks; that isn't the same as safe."
        return "No red flags found by these checks, but the launch checks didn't run; this says little."
    return primary_flag(flags)["verdict"]
