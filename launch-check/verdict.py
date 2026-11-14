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
    Rule("BUNDLE", r"^likely bundle:|first 3 minutes of trading", 3, "a group of wallets took a large share at launch"),
    Rule("WALLET_FARM", r"in near-equal slices", 3, "a wallet farm holds a large share in equal slices"),
    Rule("TOKEN_CONTROL_TRAP", r"^selling loses about|^tax getter|^transfers pay|^transfer fee authority|^transfers use a fixed fee"
         r"|^mint authority is still set|^owner 0x[0-9a-f]+ can call|getter .* reports .* of total supply|^has .* functions;", 4,
         "the token's contract can tax, mint or change balances"),
    Rule("CREATOR_EXIT_TIMING", r"^the creator wallet \S+ (?:sold|moved) .* later", 4, "the creator sold or moved its tokens right after launch"),
    Rule("FAKE_MARKET_CAP", r"^fdv is .* while listed liquidity is", 5, "the market cap is far above what its liquidity supports"),
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


def main_verdict(flags, launch_checks_ran=True):
    if not flags:
        if launch_checks_ran:
            return "No red flags found by these checks; that isn't the same as safe."
        return "No red flags found by these checks, but the launch checks did not run; this says little."
    return primary_flag(flags)["verdict"]
