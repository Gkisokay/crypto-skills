import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "launch-check"))
import contract_traps as ct  # noqa: E402


TOKEN = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
POOL = "0xcccccccccccccccccccccccccccccccccccccccc"
ROUTER = "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
WETH = "0x4200000000000000000000000000000000000006"
OWNER = "0xdddddddddddddddddddddddddddddddddddddddd"
PROBE = "0x1111111111111111111111111111111111111111"


def _word_addr(addr):
    return "0" * 24 + addr.lower()[2:]


def _word_u256(n):
    return hex(int(n))[2:].zfill(64)


def _slot_map(order, addr, slot):
    a = _word_addr(addr)
    s = _word_u256(slot)
    if order == "vyper":
        return "0x" + ct.keccak256(bytes.fromhex(s + a)).hex()
    return "0x" + ct.keccak256(bytes.fromhex(a + s)).hex()


def _slot_allowance(order, owner, spender, slot):
    o = _word_addr(owner)
    sp = _word_addr(spender)
    s = _word_u256(slot)
    if order == "vyper":
        inner = ct.keccak256(bytes.fromhex(s + o)).hex()
        return "0x" + ct.keccak256(bytes.fromhex(inner + sp)).hex()
    inner = ct.keccak256(bytes.fromhex(o + s)).hex()
    return "0x" + ct.keccak256(bytes.fromhex(sp + inner)).hex()


class FakeRpc:
    def __init__(self, *,
                 code_selectors=(),
                 owner_mode="renounced",
                 balance_layout=("solidity", 3),
                 allowance_layout=("solidity", 3),
                 tax_percent=0.0,
                 honeypot=False,
                 state_override_supported=True):
        self.code_selectors = tuple(code_selectors)
        self.owner_mode = owner_mode
        self.balance_layout = balance_layout
        self.allowance_layout = allowance_layout
        self.tax_percent = tax_percent
        self.honeypot = honeypot
        self.state_override_supported = state_override_supported
        self.calls = []
        self.expected_out = 700_000_000_000_000

    @staticmethod
    def _enc_uint_array(values):
        return "0x" + _word_u256(32) + _word_u256(len(values)) + "".join(_word_u256(v) for v in values)

    @staticmethod
    def _bal_of_data(addr):
        return "0x70a08231" + _word_addr(addr)

    @staticmethod
    def _allowance_data(owner, spender):
        return "0xdd62ed3e" + _word_addr(owner) + _word_addr(spender)

    @staticmethod
    def _owner_word(owner):
        return "0x" + "0" * 24 + owner[2:]

    def _code(self):
        raw = "".join("63" + s[2:] + "00" for s in self.code_selectors)
        return "0x" + raw

    def _state_diff(self, params):
        if len(params) < 3:
            return {}
        override = params[2] or {}
        token_override = override.get(TOKEN.lower()) or {}
        return {k.lower(): v for k, v in (token_override.get("stateDiff") or {}).items()}

    def _balance_from_override(self, params):
        if not self.balance_layout:
            return 0
        slot = _slot_map(self.balance_layout[0], PROBE, self.balance_layout[1]).lower()
        val = self._state_diff(params).get(slot)
        return int(val, 16) if val else 0

    def _allowance_from_override(self, params):
        if not self.allowance_layout:
            return 0
        slot = _slot_allowance(self.allowance_layout[0], PROBE, ROUTER, self.allowance_layout[1]).lower()
        val = self._state_diff(params).get(slot)
        return int(val, 16) if val else 0

    def _swap_amount_out_min(self, data):
        return int(data[74:138], 16)

    def _amount_in(self, data):
        return int(data[10:74], 16)

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_getStorageAt":
            return "0x" + "0" * 64
        if method == "eth_getCode":
            return self._code() if params[0].lower() == TOKEN else "0x"
        if method != "eth_call":
            raise AssertionError((method, params))

        if len(params) > 2 and not self.state_override_supported:
            raise RuntimeError("invalid arguments; expected 2")

        call = params[0]
        to = (call.get("to") or "").lower()
        data = (call.get("data") or "0x").lower()
        sel = data[:10]

        if to == TOKEN:
            if sel == "0x8da5cb5b":
                if self.owner_mode == "missing":
                    raise RuntimeError("execution reverted")
                if self.owner_mode == "renounced":
                    return "0x" + "0" * 64
                return self._owner_word(OWNER)
            if sel == "0x70a08231":
                return hex(self._balance_from_override(params))
            if sel == "0xdd62ed3e":
                return hex(self._allowance_from_override(params))
            return "0x"

        if to == POOL:
            if sel == "0x0dfe1681":
                return self._owner_word(TOKEN)
            if sel == "0xd21220a7":
                return self._owner_word(WETH)
            if sel == "0xc45a0155":
                return self._owner_word("0x8909dc15e40173ff4699343b6eb8132c65e18ec6")
            raise AssertionError((to, data))

        if to == ROUTER:
            if sel == "0xd06ca61f":
                amount_in = self._amount_in(data)
                return self._enc_uint_array([amount_in, self.expected_out])
            if sel == "0x791ac947":
                amount_out_min = self._swap_amount_out_min(data)
                if self.honeypot:
                    raise RuntimeError("execution reverted: TOKEN_TRANSFER_BLOCKED")
                floor = int(self.expected_out * (100.0 - self.tax_percent) / 100.0)
                if amount_out_min > floor:
                    raise RuntimeError("execution reverted: INSUFFICIENT_OUTPUT_AMOUNT")
                return "0x"
            raise AssertionError((to, data))
        raise AssertionError((to, data))


class KeccakAndSelectors(unittest.TestCase):
    def test_keccak_vectors(self):
        self.assertEqual(
            ct.keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        )

    def test_selector_vectors(self):
        self.assertEqual(ct.selector("transfer(address,uint256)"), "0xa9059cbb")
        self.assertEqual(ct.selector("balanceOf(address)"), "0x70a08231")
        self.assertEqual(ct.selector("approve(address,uint256)"), "0x095ea7b3")
        self.assertEqual(ct.selector("owner()"), "0x8da5cb5b")
        self.assertEqual(ct.selector("version()"), "0x54fd4d50")
        self.assertEqual(ct.selector("openTrading()"), "0xc9567bf9")


class ContractTraps(unittest.TestCase):
    def check(self, rpc, chain="base"):
        return ct.check_contract_traps(
            rpc,
            TOKEN,
            chain,
            {"pairs": [POOL], "pair_liquidity": {POOL: 42000.0}},
            1_000_000_000_000_000_000_000_000,
        )

    def test_detects_solidity_slot_layout(self):
        out = self.check(FakeRpc(balance_layout=("solidity", 3), allowance_layout=("solidity", 3)))
        self.assertFalse(any("layout not found" in x for x in out["cant_check"]), out["cant_check"])
        self.assertFalse(any("Selling reverts in simulation" in x for x in out["flags"]), out["flags"])

    def test_detects_vyper_slot_layout(self):
        out = self.check(FakeRpc(balance_layout=("vyper", 3), allowance_layout=("vyper", 3)))
        self.assertFalse(any("layout not found" in x for x in out["cant_check"]), out["cant_check"])

    def test_unknown_layout_adds_cant_check_and_no_honeypot_flag(self):
        out = self.check(FakeRpc(balance_layout=None, allowance_layout=None, honeypot=True))
        self.assertTrue(any("balance storage layout not found" in x for x in out["cant_check"]), out["cant_check"])
        self.assertFalse(any("Selling reverts in simulation" in x for x in out["flags"]), out["flags"])

    def test_measures_35pct_tax_within_2pct(self):
        out = self.check(FakeRpc(tax_percent=35.0))
        msg = next(x for x in out["flags"] if "Selling loses about" in x)
        pct = float(re.search(r"about ([0-9.]+)%", msg).group(1))
        self.assertLessEqual(abs(pct - 35.0), 2.0, msg)

    def test_zero_tax_has_no_tax_flag(self):
        out = self.check(FakeRpc(tax_percent=0.0))
        self.assertFalse(any("Selling loses about" in x for x in out["flags"]), out["flags"])

    def test_honeypot_only_when_override_confirmed(self):
        flagged = self.check(FakeRpc(honeypot=True, balance_layout=("solidity", 3), allowance_layout=("solidity", 3)))
        self.assertTrue(any("Selling reverts in simulation" in x for x in flagged["flags"]), flagged["flags"])
        blocked = self.check(FakeRpc(honeypot=True, balance_layout=None, allowance_layout=None))
        self.assertFalse(any("Selling reverts in simulation" in x for x in blocked["flags"]), blocked["flags"])

    def test_renounced_owner_plus_mint_fn_no_flag(self):
        out = self.check(FakeRpc(code_selectors=[ct.selector("mint(address,uint256)")], owner_mode="renounced"))
        self.assertFalse(any("can call mint(address,uint256)" in x for x in out["flags"]), out["flags"])

    def test_live_owner_plus_mint_fn_flags(self):
        out = self.check(FakeRpc(code_selectors=[ct.selector("mint(address,uint256)")], owner_mode="live"))
        self.assertTrue(any("Owner " + OWNER in x and "mint(address,uint256)" in x for x in out["flags"]), out["flags"])

    def test_no_owner_function_with_mint_fn_adds_unknown_control_line(self):
        out = self.check(FakeRpc(code_selectors=[ct.selector("mint(address,uint256)")], owner_mode="missing"))
        self.assertTrue(any("who can call them wasn't checked" in x for x in out["flags"]), out["flags"])

    def test_unknown_router_chain_adds_cant_check(self):
        out = self.check(FakeRpc(), chain="robinhood")
        self.assertTrue(any("robinhood" in x.lower() for x in out["cant_check"]), out["cant_check"])


if __name__ == "__main__":
    unittest.main()
