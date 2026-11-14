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
    Rule("LP_PULLED", r"\bliquidity pulled\b", 1, "liquidity was pulled from a pool"),
    Rule("LP_CAN_PULL", r"\bliquidity can be pulled\b", 1, "one wallet can remove most pool liquidity"),
    Rule("HONEYPOT_OR_SELL_BLOCKED", r"\bhoneypot\b|sell (?:is )?blocked|can't sell|cannot sell", 2,
         "tokens can't be transferred out by ordinary wallets"),
    Rule("BUNDLE", r"\blikely bundle\b|wallets took .*first 3 minutes|bundled launch|fresh-wallet bundle", 3,
         "a fresh-wallet bundle took a large share at launch"),
    Rule("WALLET_FARM", r"wallet[- ]farm|equal-slice holders|farm wallets", 3,
         "a wallet farm appears to hold a large share"),
    Rule("SNIPER_CLUSTER", r"\bsniper", 3, "sniper wallets took a large launch share quickly"),
    Rule("TOKEN_CONTROL_TRAP", r"\btax\b|mint authority|freeze authority|token-2022|transferfeeconfig|transferhook|permanentdelegate|pausableconfig|blacklist|owner mint|pause",
         4, "token controls can change balances or block transfers"),
    Rule("INSIDER_EXIT", r"insider.*exit|creator.*sold|creator exit|sold .* after (?:creation|launch)", 5,
         "insider wallets exited early"),
    Rule("FAKE_MARKET_CAP", r"fake market cap|fdv far above|liquidity can support", 5,
         "reported market cap appears inflated versus liquidity"),
    Rule("FAKE_VOLUME", r"fake volume|trading in circles|wash", 5, "volume looks concentrated in repeated wallet loops"),
    Rule("COPYCAT_NAME", r"copycat|same name|same ticker", 5, "the name or ticker matches another coin"),
    Rule("CREATOR_HISTORY", r"creator history|launched .* before|previous launches", 5,
         "the creator wallet has prior launch history worth checking"),
    Rule("CREATOR_EXIT_TIMING", r"creator .* after .* launch|creator .* after .* create", 5,
         "the creator wallet exited soon after launch"),
    Rule("CREATOR_HOLDS", r"^creator wallet holds\b", 5, "the creator wallet still holds a large supply share"),
    Rule("FUNDER_CLUSTER", r"funded .* first-minutes buyers|funded .* early buyers", 5,
         "one funder appears across many early buyers"),
    Rule("FDV_MISMATCH", r"\bdexscreener fdv\b", 5, "reported FDV and on-chain supply-based FDV diverge"),
    Rule("TOP10_PEOPLE", r"^top 10 people\b", 5, "holder concentration among plain wallets is high"),
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
