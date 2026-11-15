#!/usr/bin/env python3
"""Map linked top holders of a Base, Robinhood Chain or Solana token; prints a plain-text answer for the chat."""

import argparse
import collections
import concurrent.futures
import datetime
import gzip
import hashlib
import io
import json
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CHAIN_ID = 4663
CHAIN_NAME = "Robinhood"
BLOCKSCOUT_BASE = "https://api.blockscout.com/v2/api"
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
USER_AGENT = "cluster-map/1.0"

MAX_WORKERS = 4
MAX_REQUESTS_PER_SECOND = 4.0
HISTORY_OFFSET = 1000
RPC_BATCH_SIZE = 5
HTTP_TIMEOUT_S = 30

RETRY_TRIES = 3
BACKOFF_BASE_S = 0.35

BUSY_NONCE = 1000
# Above this even a funding loop (sent to a holder and got ETH back from it) is not trusted: exchange/bridge scale.
SUPERNODE_NONCE = 100_000
TIME_WINDOW_S = 300
LAUNCH_WINDOW_S = 180
ORIGIN_RESOLVE_PER_PERSON = 3
BURST_WINDOW_S = 2
FOLLOW_HOPS = 3
FOLLOW_MIN_WEI = 5 * 10**16
FOLLOW_BUDGET_S = 180
FOLLOW_MAX_STARTS = 20
SUMMARY_MAX_WALLETS = 20
SUMMARY_NOTE_FOLD = 4
FOLLOW_MAX_SELLS = 12
BURST_MIN_WALLETS = 3
BURST_MIN_SHARE = 0.005
BURST_SIZE_TOLERANCE = 0.10

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
NONCE_WALK_MAX = 25
TOKEN_LINK_LOOKBACK_DAYS = 2.0
MAX_LOG_QUERIES = 400
MIN_LOG_SPLIT_BLOCKS = 200
RPC_WORKERS = 4
RPC_REQUESTS_PER_SECOND = 8.0

CHAINS = {
    "robinhood": {
        "chain_id": 4663,
        "name": "Robinhood",
        "source": "blockscout",
        "rpc_url": "https://rpc.mainnet.chain.robinhood.com",
        "rpc_batch": 5,
    },
    "base": {
        "chain_id": 8453,
        "name": "Base",
        "source": "rpc",
        "rpc_url": "https://base-rpc.publicnode.com/{key}",
        "rpc_key_env": "PUBLICNODE_TOKEN",
        "rpc_batch": 10,
        "log_range": 50_000,
        "trace": True,
        # PublicNode refuses eth_getLogs without a contract address, so token links cover the coin plus these.
        "link_tokens": {
            "0x4200000000000000000000000000000000000006": "WETH",
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
        },
    },
    "solana": {
        "chain_id": "solana",
        "name": "Solana",
        "native": "SOL",
        "source": "solana",
        "rpc_url": "https://mainnet.helius-rpc.com/?api-key={key}",
        "rpc_key_env": "HELIUS_API_KEY",
        "rpc_batch": 1,
    },
}

SOL_PAGE = 100
SOL_HOLDER_PAGES = 20
SOL_LAUNCH_PAGES = 10
SOL_BUSY_SIGNATURES = 1000
SOL_EXCHANGE_SPAN_S = 86400  # 1,000 txs inside a day: exchange-scale (mihao control, Nov 15: 1,000 in 192 s)
SOL_FOLLOW_PAGES = 2
SOL_RETRY_TRIES = 5
SOL_RETRY_SLEEP_S = 2
# SOL amounts are carried in 1e-18 units like wei, so 1 SOL compares like 1 ETH in shared thresholds.
LAMPORTS_TO_WEI = 10**9
SOLANA_SYSTEM_PROGRAM = "11111111111111111111111111111111"
SOLANA_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOLANA_ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WSOL_MINT = "So11111111111111111111111111111111111111112"
SOLANA_BURN = "1nc1nerator11111111111111111111111111111111"

DEFAULT_TOP = 250
DEFAULT_CACHE_DIR = "~/.cache/cluster-map/"
CACHE_MAX_AGE_S = 3600
MAX_HISTORY_PAGES = 5

FOLLOW_CHIPS_MAX = 3

EVM_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
HEX_64_RE = re.compile(r"0x[0-9a-fA-F]{64}")
BASE58_ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
SOLANA_SIG_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{64,90}")

ZERO_ADDRESS = "0x" + "0" * 40
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"
BURN_PREFIX = "0x000000000000000000000000000000000000"



def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def normalize_address(value):
    """EVM addresses lowercased; Solana base58 addresses kept as they are (they are case-sensitive)."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if EVM_ADDRESS_RE.fullmatch(text):
        return text.lower()
    if BASE58_ADDRESS_RE.fullmatch(text):
        return text
    return ""


def is_valid_address(value, chain_key="robinhood"):
    if not isinstance(value, str):
        return False
    pattern = BASE58_ADDRESS_RE if chain_key == "solana" else EVM_ADDRESS_RE
    return bool(pattern.fullmatch(value.strip()))


def norm_hash(value):
    text = str(value or "").strip()
    if HEX_64_RE.fullmatch(text):
        return text.lower()
    if SOLANA_SIG_RE.fullmatch(text):
        return text
    return ""


def parse_int(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("0x", "0X")):
            if re.fullmatch(r"0x[0-9a-fA-F]+", text):
                return int(text, 16)
            return 0
        if re.fullmatch(r"-?[0-9]+", text):
            return int(text)
    return 0


def short_addr(addr):
    return addr[:8] + "..." + addr[-6:]


def as_pct(value):
    return f"{value * 100:.3f}%"


def sanitize_error_text(text, api_key):
    clean = text or ""
    if api_key:
        clean = clean.replace(api_key, "[redacted]")
    clean = re.sub(r"([?&]api-?key=)[^&\s]+", r"\1[redacted]", clean, flags=re.IGNORECASE)
    return clean


def add_unique_sample(bucket, value, limit=5):
    if not value:
        return
    if value not in bucket and len(bucket) < limit:
        bucket.append(value)


def is_contract_code(code):
    if code is None:
        return False
    text = str(code).strip().lower()
    return text not in ("", "0x", "0x0", "0x00")


def is_burn_address(addr):
    if addr in (ZERO_ADDRESS, DEAD_ADDRESS, SOLANA_BURN):
        return True
    return addr.startswith(BURN_PREFIX)


def backoff_sleep(attempt_index):
    delay = BACKOFF_BASE_S * (2 ** attempt_index)
    time.sleep(delay)


class RpcStateError(RuntimeError):
    """The node no longer holds state for that block (some backends behind a load balancer are not archive)."""


def is_state_error(error):
    text = str(error.get("message", "") if isinstance(error, dict) else error).lower()
    return "pruned" in text or "missing trie node" in text


class RequestStats:
    def __init__(self):
        self._lock = threading.Lock()
        self.calls_made = 0
        self.cache_hits = 0

    def mark_call(self):
        with self._lock:
            self.calls_made += 1

    def mark_cache_hit(self):
        with self._lock:
            self.cache_hits += 1


class RateLimiter:
    def __init__(self, rps):
        self.rps = float(rps)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + (1.0 / self.rps)
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class DiskCache:
    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(os.path.expanduser(root_dir))
        os.makedirs(self.root_dir, exist_ok=True)
        self._lock = threading.Lock()
        self.oldest_age_s = None

    def _hash_key(self, key_obj):
        raw = json.dumps(key_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _path_for(self, namespace, key_obj):
        digest = self._hash_key(key_obj)
        bucket = digest[:2]
        folder = os.path.join(self.root_dir, namespace, bucket)
        filename = digest + ".json"
        return folder, os.path.join(folder, filename)

    def get(self, namespace, key_obj, max_age_s=None):
        folder, path = self._path_for(namespace, key_obj)
        if not os.path.exists(path):
            return None
        age = time.time() - os.path.getmtime(path)
        if max_age_s is not None and age > max_age_s:
            return None
        with self._lock:
            if max_age_s is not None and (self.oldest_age_s is None or age > self.oldest_age_s):
                self.oldest_age_s = age
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)

    def set(self, namespace, key_obj, value):
        folder, path = self._path_for(namespace, key_obj)
        os.makedirs(folder, exist_ok=True)
        with self._lock:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))


def default_fetch_json(url, data=None, headers=None, timeout=HTTP_TIMEOUT_S):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    try:
        return json.loads(body)
    except ValueError:
        parts = urllib.parse.urlsplit(url)
        snippet = sanitize_error_text(body, "")
        secrets = [v for _, v in urllib.parse.parse_qsl(parts.query) if v]
        secrets += [s for s in parts.path.split("/") if len(s) >= 16]
        for value in secrets:
            snippet = snippet.replace(value, "[redacted]")
        raise RuntimeError(f"{parts.hostname} returned a non-JSON response: {snippet[:80]}")


class BlockscoutClient:
    def __init__(self, api_key, cache, stats, fetch_json=None, limiter=None):
        self.api_key = api_key
        self.cache = cache
        self.stats = stats
        self.fetch_json = fetch_json or default_fetch_json
        self.limiter = limiter or RateLimiter(MAX_REQUESTS_PER_SECOND)

    def _request(self, module, action, params):
        cache_key = {
            "base": BLOCKSCOUT_BASE,
            "chain_id": CHAIN_ID,
            "module": module,
            "action": action,
            "params": params,
        }
        cached = self.cache.get("blockscout", cache_key, max_age_s=None if "txhash" in params else CACHE_MAX_AGE_S)
        if cached is not None:
            self.stats.mark_cache_hit()
            return cached

        query = {"chain_id": CHAIN_ID, "module": module, "action": action, "apikey": self.api_key}
        query.update(params)
        url = BLOCKSCOUT_BASE + "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": USER_AGENT}

        for attempt in range(RETRY_TRIES):
            self.limiter.wait()
            try:
                payload = self.fetch_json(url, None, headers, HTTP_TIMEOUT_S)
                self.stats.mark_call()
                error_text = self._error_body(payload)
                if error_text:
                    if attempt < RETRY_TRIES - 1:
                        backoff_sleep(attempt)
                        continue
                    raise RuntimeError(f"Blockscout request failed: {sanitize_error_text(error_text, self.api_key)}")
                self.cache.set("blockscout", cache_key, payload)
                return payload
            except urllib.error.HTTPError as exc:
                self.stats.mark_call()
                body = exc.read().decode("utf-8", errors="replace")
                clean_body = sanitize_error_text(body, self.api_key)
                if exc.code in (401, 402, 404):
                    raise RuntimeError(f"Blockscout request failed with HTTP {exc.code}: {clean_body}")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < RETRY_TRIES - 1:
                    backoff_sleep(attempt)
                    continue
                raise RuntimeError(f"Blockscout request failed with HTTP {exc.code}: {clean_body}")
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                reason = sanitize_error_text(str(getattr(exc, "reason", exc)), self.api_key)
                if attempt < RETRY_TRIES - 1:
                    backoff_sleep(attempt)
                    continue
                raise RuntimeError(f"Blockscout request failed: {reason}")

    @staticmethod
    def _error_body(payload):
        """Etherscan-style APIs report errors (rate limits included) as HTTP 200 with status "0" and a string result."""
        if not isinstance(payload, dict) or str(payload.get("status")) != "0":
            return ""
        result = payload.get("result")
        text = f"{payload.get('message', '')} {result if isinstance(result, str) else ''}".strip()
        if isinstance(result, list) or "no transactions found" in text.lower() or "no records found" in text.lower():
            return ""
        return text or "status 0 with no result"

    def _extract_result(self, payload):
        if isinstance(payload, dict) and "result" in payload:
            result = payload["result"]
            if isinstance(result, str):
                lowered = result.lower()
                if "no transactions found" in lowered or "no records found" in lowered:
                    return []
            return result
        return payload

    def get_token(self, token):
        payload = self._request("token", "getToken", {"contractaddress": token})
        result = self._extract_result(payload)
        if isinstance(result, list) and result:
            return result[0]
        if isinstance(result, dict):
            return result
        raise RuntimeError("Blockscout token response did not include token metadata.")

    def get_token_holders(self, token, top_n):
        payload = self._request(
            "token",
            "getTokenHolders",
            {"contractaddress": token, "page": 1, "offset": top_n},
        )
        result = self._extract_result(payload)
        if isinstance(result, list):
            return result
        raise RuntimeError("Blockscout holders response did not include holder rows.")

    def get_history(self, address, action):
        rows = []
        note = ""
        truncated = False
        for page in range(1, MAX_HISTORY_PAGES + 1):
            params = {"address": address, "page": page, "offset": HISTORY_OFFSET, "sort": "asc"}
            if page == 1:
                payload = self._request("account", action, params)
            else:
                # Blockscout's upstream fails deep pages of very busy wallets every time; keep what we have.
                try:
                    payload = self._request("account", action, params)
                except RuntimeError as exc:
                    truncated = True
                    note = f"{address}: {action} page {page} failed ({exc}); history cut at {len(rows)} rows."
                    break
            page_rows = self._extract_result(payload)
            if not isinstance(page_rows, list):
                page_rows = []
            if isinstance(payload, dict):
                message = str(payload.get("message", ""))
                if action == "txlistinternal" and page_rows and "not yet been processed" in message.lower():
                    note = f"{address}: txlistinternal included unprocessed-range warning and rows were used."
            rows.extend(page_rows)
            if len(page_rows) < HISTORY_OFFSET:
                break
            if page == MAX_HISTORY_PAGES:
                truncated = True
        return rows, truncated, note

    def get_token_transfers(self, token):
        payload = self._request(
            "account",
            "tokentx",
            {"contractaddress": token, "page": 1, "offset": HISTORY_OFFSET, "sort": "asc"},
        )
        rows = self._extract_result(payload)
        if not isinstance(rows, list):
            rows = []
        return rows

    def tx_internal_native(self, tx_hash):
        rows = self._extract_result(self._request("account", "txlistinternal", {"txhash": tx_hash}))
        out = []
        for row in rows if isinstance(rows, list) else []:
            if parse_int(row.get("value")) > 0 and not str(row.get("isError", "0")) == "1":
                row = dict(row)
                row.setdefault("hash", tx_hash)
                out.append(row)
        return out


class RpcClient:
    def __init__(self, url, cache, stats, fetch_json=None, batch_size=RPC_BATCH_SIZE, max_batch=RPC_BATCH_SIZE, limiter=None, cache_label=None, secret=""):
        self.url = url
        self.secret = secret
        self.cache = cache
        self.stats = stats
        self.fetch_json = fetch_json or default_fetch_json
        self.batch_size = min(max(1, batch_size), max_batch)
        self.limiter = limiter
        # The cache key must not carry a URL with a key in it into anything written out; it is only hashed today.
        self.cache_label = cache_label or url

    def _cache_key(self, method, params):
        return {"url": self.cache_label, "method": method, "params": params}

    @staticmethod
    def _max_age(method, params):
        if method.startswith(("eth_", "trace_")) and "latest" not in json.dumps(params):
            return None
        return CACHE_MAX_AGE_S

    def _wait(self):
        if self.limiter is not None:
            self.limiter.wait()

    def _clean(self, text):
        return sanitize_error_text(str(text), self.secret)

    def call(self, method, params, cacheable=True):
        return self._request_single(method, params, cacheable=cacheable)

    def call_many(self, method, params_list):
        return self._request_batch(method, params_list)

    def _request_single(self, method, params, cacheable=True):
        key = self._cache_key(method, params)
        if cacheable:
            cached = self.cache.get("rpc", key, max_age_s=self._max_age(method, params))
            if cached is not None:
                self.stats.mark_cache_hit()
                return cached
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        body = json.dumps(payload).encode("utf-8")
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        for attempt in range(RETRY_TRIES):
            try:
                self._wait()
                resp = self.fetch_json(self.url, body, headers, HTTP_TIMEOUT_S)
                self.stats.mark_call()
                if isinstance(resp, dict) and "error" in resp:
                    if is_state_error(resp["error"]):
                        raise RpcStateError(f"RPC {method} failed: {self._clean(resp['error'])}")
                    raise RuntimeError(f"RPC {method} failed: {self._clean(resp['error'])}")
                result = resp.get("result") if isinstance(resp, dict) else None
                if cacheable:
                    self.cache.set("rpc", key, result)
                return result
            except RpcStateError:
                if attempt < RETRY_TRIES - 1:
                    backoff_sleep(attempt)
                    continue
                raise
            except urllib.error.HTTPError as exc:
                self.stats.mark_call()
                body_text = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < RETRY_TRIES - 1:
                    backoff_sleep(attempt)
                    continue
                if exc.code in (401, 402, 404):
                    raise RuntimeError(f"RPC request failed with HTTP {exc.code}: {self._clean(body_text)}")
                raise RuntimeError(f"RPC request failed with HTTP {exc.code}: {self._clean(body_text)}")
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                if attempt < RETRY_TRIES - 1:
                    backoff_sleep(attempt)
                    continue
                raise RuntimeError(f"RPC request failed: {self._clean(getattr(exc, 'reason', exc))}")

    def _request_batch(self, method, params_list):
        if not params_list:
            return []
        results = [None] * len(params_list)
        pending = []
        for idx, params in enumerate(params_list):
            key = self._cache_key(method, params)
            cached = self.cache.get("rpc", key, max_age_s=self._max_age(method, params))
            if cached is not None:
                self.stats.mark_cache_hit()
                results[idx] = cached
            else:
                pending.append((idx, params, key))
        if not pending:
            return results

        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        for start in range(0, len(pending), self.batch_size):
            chunk = pending[start : start + self.batch_size]
            payload = []
            for local_id, (_, params, _) in enumerate(chunk):
                payload.append({"jsonrpc": "2.0", "id": local_id, "method": method, "params": params})
            body = json.dumps(payload).encode("utf-8")
            chunk_done = False
            for attempt in range(RETRY_TRIES):
                try:
                    self._wait()
                    resp = self.fetch_json(self.url, body, headers, HTTP_TIMEOUT_S)
                    self.stats.mark_call()
                    if not isinstance(resp, list):
                        raise RuntimeError(f"RPC batch for {method} returned non-list payload.")
                    by_id = {}
                    for row in resp:
                        row_id = row.get("id")
                        if row_id is None:
                            continue
                        if "error" in row:
                            if is_state_error(row["error"]):
                                raise RpcStateError(f"RPC {method} failed: {self._clean(row['error'])}")
                            raise RuntimeError(f"RPC {method} failed: {self._clean(row['error'])}")
                        by_id[row_id] = row.get("result")
                    if len(by_id) != len(chunk):
                        raise RuntimeError(f"RPC batch for {method} was missing results.")
                    for local_id, (idx, _, key) in enumerate(chunk):
                        value = by_id[local_id]
                        results[idx] = value
                        self.cache.set("rpc", key, value)
                    chunk_done = True
                    break
                except RpcStateError:
                    if attempt < RETRY_TRIES - 1:
                        backoff_sleep(attempt)
                        continue
                    raise
                except urllib.error.HTTPError as exc:
                    self.stats.mark_call()
                    retryable = exc.code == 429 or 500 <= exc.code < 600
                    if retryable and attempt < RETRY_TRIES - 1:
                        backoff_sleep(attempt)
                        continue
                    body_text = exc.read().decode("utf-8", errors="replace")
                    if exc.code in (401, 402, 404):
                        raise RuntimeError(f"RPC batch failed with HTTP {exc.code}: {self._clean(body_text)}")
                    raise RuntimeError(f"RPC batch failed with HTTP {exc.code}: {self._clean(body_text)}")
                except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                    if attempt < RETRY_TRIES - 1:
                        backoff_sleep(attempt)
                        continue
                    raise RuntimeError(f"RPC batch failed: {self._clean(getattr(exc, 'reason', exc))}")
            if not chunk_done:
                raise RuntimeError(f"RPC batch for {method} failed repeatedly.")
        return results

    def get_code_many(self, addresses):
        params = [[addr, "latest"] for addr in addresses]
        return self._request_batch("eth_getCode", params)

    def get_nonce_many(self, addresses):
        params = [[addr, "latest"] for addr in addresses]
        rows = self._request_batch("eth_getTransactionCount", params)
        return [parse_int(value) for value in rows]

    def get_tx_many(self, hashes):
        return self._request_batch("eth_getTransactionByHash", [[h] for h in hashes])


def topic_address(addr):
    return "0x" + "0" * 24 + addr[2:]


def address_from_topic(topic):
    if not isinstance(topic, str) or len(topic) < 42:
        return ""
    return normalize_address("0x" + topic[-40:])


def decode_abi_string(hex_data):
    text = (hex_data or "")[2:]
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return ""
    if len(raw) >= 64:
        length = int.from_bytes(raw[32:64], "big")
        if 0 < length <= len(raw) - 64:
            return raw[64 : 64 + length].decode("utf-8", errors="replace")
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace").strip()


class RpcChainSource:
    """Token, holder and history reads from a plain archive RPC with trace_filter (Base on PublicNode).

    No free address-history API covers Base, so per-wallet native history is reconstructed:
    first funding by bisecting archive balance (balance only rises before a wallet's first tx), outgoing sends
    by bisecting the nonce for wallets with few txs, and the exact block read with trace_filter.
    """

    def __init__(self, chain_key, rpc_client):
        self.chain_key = chain_key
        self.chain = CHAINS[chain_key]
        self.rpc = rpc_client
        self.token = ""
        self.head = None
        self.creation_block = None
        self.token_logs = None
        self.coverage = []
        self.notes = []

    def head_block(self):
        if self.head is None:
            self.head = parse_int(self.rpc.call("eth_blockNumber", [], cacheable=False))
        return self.head

    def block_timestamp(self, block):
        header = self.rpc.call("eth_getBlockByNumber", [hex(block), False]) or {}
        return parse_int(header.get("timestamp"))

    def first_block_where(self, method, address, test, lo, hi, seen=None):
        """Smallest block in (lo, hi] where test(value of method at block) holds; test must be monotonic.

        `seen` collects every probed block's value so a caller bisecting the same address again can narrow first.
        """
        step = max(1, self.rpc.batch_size)
        while hi - lo > 1:
            gap = hi - lo
            probes = sorted({lo + (gap * i) // (step + 1) for i in range(1, step + 1)} - {lo, hi})
            if not probes:
                probes = [lo + 1]
            values = self.rpc.call_many(method, [[address, hex(b)] for b in probes])
            new_lo, new_hi = lo, hi
            for block, value in zip(probes, values):
                value = parse_int(value) if method != "eth_getCode" else value
                if seen is not None:
                    seen[block] = value
                if test(value):
                    new_hi = block
                    break
                new_lo = block
            lo, hi = new_lo, new_hi
        return hi

    def get_token(self, token):
        self.token = token
        calls = [
            {"to": token, "data": "0x95d89b41"},
            {"to": token, "data": "0x313ce567"},
            {"to": token, "data": "0x18160ddd"},
        ]
        head = self.head_block()
        symbol_hex, decimals_hex, supply_hex = self.rpc.call_many("eth_call", [[c, hex(head)] for c in calls])
        return {
            "symbol": decode_abi_string(symbol_hex) or "UNKNOWN",
            "decimals": str(parse_int(decimals_hex)),
            "totalSupply": str(parse_int(supply_hex)),
        }

    def _get_logs(self, base_filter, lo, hi):
        flt = dict(base_filter)
        flt["fromBlock"] = hex(lo)
        flt["toBlock"] = hex(hi)
        try:
            return self.rpc.call("eth_getLogs", [flt]) or []
        except RuntimeError:
            if hi - lo < MIN_LOG_SPLIT_BLOCKS:
                raise
            mid = (lo + hi) // 2
            return self._get_logs(base_filter, lo, mid) + self._get_logs(base_filter, mid + 1, hi)

    def scan_logs(self, base_filter, start, end):
        span = self.chain["log_range"]
        ranges = [(lo, min(end, lo + span - 1)) for lo in range(start, end + 1, span)]
        if len(ranges) > MAX_LOG_QUERIES:
            raise RuntimeError(f"log scan needs {len(ranges)} queries (cap {MAX_LOG_QUERIES})")
        out = [None] * len(ranges)
        with concurrent.futures.ThreadPoolExecutor(max_workers=RPC_WORKERS) as pool:
            futures = {pool.submit(self._get_logs, base_filter, lo, hi): i for i, (lo, hi) in enumerate(ranges)}
            for fut in concurrent.futures.as_completed(futures):
                out[futures[fut]] = fut.result()
        logs = [row for chunk in out for row in chunk]
        logs.sort(key=lambda x: (parse_int(x.get("blockNumber")), parse_int(x.get("logIndex"))))
        return logs, len(ranges)

    def _load_token_logs(self, token):
        if self.token_logs is not None:
            return
        head = self.head_block()
        code_now = self.rpc.call("eth_getCode", [token, hex(head)])
        if not is_contract_code(code_now):
            raise RuntimeError(f"{token} has no contract code on {self.chain['name']}.")
        self.creation_block = self.first_block_where("eth_getCode", token, is_contract_code, -1, head)
        try:
            logs, queries = self.scan_logs({"address": token, "topics": [TRANSFER_TOPIC]}, self.creation_block, head)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Holder list on {self.chain['name']} comes from the coin's Transfer logs and this coin is too old "
                f"for the free RPC ({exc}). A holder API would be needed."
            )
        self.token_logs = logs
        self.coverage.append(
            f"holders: rebuilt from {len(logs)} Transfer logs, blocks {self.creation_block:,}–{head:,} ({queries} queries)"
        )

    def _transfer_rows(self, logs):
        rows = []
        for log in logs:
            topics = log.get("topics") or []
            if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
                continue
            rows.append(
                {
                    "from": address_from_topic(topics[1]),
                    "to": address_from_topic(topics[2]),
                    "value": str(parse_int(log.get("data"))),
                    "hash": str(log.get("transactionHash", "")).lower(),
                    "blockNumber": str(parse_int(log.get("blockNumber"))),
                    "timeStamp": str(parse_int(log.get("blockTimestamp"))),
                    "contractAddress": normalize_address(log.get("address", "")),
                }
            )
        return rows

    def get_token_holders(self, token, top_n):
        self._load_token_logs(token)
        balances = collections.Counter()
        for row in self._transfer_rows(self.token_logs):
            value = parse_int(row["value"])
            if row["from"] != ZERO_ADDRESS:
                balances[row["from"]] -= value
            balances[row["to"]] += value
        ranked = sorted(((a, v) for a, v in balances.items() if v > 0 and a != ZERO_ADDRESS), key=lambda x: (-x[1], x[0]))
        return [{"address": a, "value": str(v)} for a, v in ranked[:top_n]]

    def get_token_transfers(self, token):
        self._load_token_logs(token)
        return self._transfer_rows(self.token_logs)[:HISTORY_OFFSET]

    def coin_rows_from(self, address):
        self._load_token_logs(self.token)
        return [r for r in self._transfer_rows(self.token_logs) if r["from"] == address]

    def tx_internal_native(self, tx_hash):
        traces = self.rpc.call("trace_transaction", [tx_hash]) or []
        out = []
        for tr in traces:
            action = tr.get("action") or {}
            value = parse_int(action.get("value"))
            if value <= 0 or tr.get("error") or not tr.get("traceAddress"):
                continue
            block = parse_int(tr.get("blockNumber"))
            out.append(
                {
                    "from": normalize_address(action.get("from", "")),
                    "to": normalize_address(action.get("to", "")),
                    "value": str(value),
                    "hash": tx_hash,
                    "blockNumber": str(block),
                    "timeStamp": str(self.block_timestamp(block)),
                }
            )
        return out

    def _trace_rows(self, block, address, direction):
        key = "toAddress" if direction == "in" else "fromAddress"
        traces = self.rpc.call("trace_filter", [{"fromBlock": hex(block), "toBlock": hex(block), key: [address]}]) or []
        ts = self.block_timestamp(block)
        top_level_sent = 0
        native, internal = [], []
        for tr in traces:
            action = tr.get("action") or {}
            frm = normalize_address(action.get("from", ""))
            to = normalize_address(action.get("to", ""))
            top = not tr.get("traceAddress")
            if direction == "out" and top and frm == address:
                top_level_sent += 1
            value = parse_int(action.get("value"))
            if value <= 0 or not frm or not to or tr.get("error"):
                continue
            if (direction == "in" and to != address) or (direction == "out" and frm != address):
                continue
            row = {
                "from": frm,
                "to": to,
                "value": str(value),
                "hash": str(tr.get("transactionHash", "")).lower(),
                "blockNumber": str(block),
                "timeStamp": str(ts),
            }
            (native if top else internal).append(row)
        return native, internal, top_level_sent

    def _native_history(self, address, head):
        txlist, internal, notes = [], [], []
        nonce_now = parse_int(self.rpc.call("eth_getTransactionCount", [address, hex(head)]))
        first_send = None
        if nonce_now > 0:
            first_send = self.first_block_where("eth_getTransactionCount", address, lambda n: n >= 1, -1, head)
            fund_hi = first_send - 1
        else:
            fund_hi = head
        balance_hi = parse_int(self.rpc.call("eth_getBalance", [address, hex(fund_hi)])) if fund_hi >= 0 else 0
        if balance_hi > 0:
            fund_block = self.first_block_where("eth_getBalance", address, lambda v: v > 0, -1, fund_hi)
            rows_n, rows_i, _ = self._trace_rows(fund_block, address, "in")
            txlist.extend(rows_n)
            internal.extend(rows_i)
            if not rows_n and not rows_i:
                notes.append(f"{address}: first ETH arrived in block {fund_block} but no trace shows the sender.")
        elif nonce_now > 0:
            notes.append(f"{address}: sent its first tx with no ETH before it (sponsored gas?); funder unknown.")
        if 0 < nonce_now <= NONCE_WALK_MAX:
            rows_n, rows_i = self._walk_sends(address, 1, first_send - 1, nonce_now, head)
            txlist.extend(rows_n)
            internal.extend(rows_i)
        return address, {"txlist": txlist, "txlistinternal": internal, "tokentx": []}, nonce_now, notes

    def _walk_sends(self, address, k, lo, nonce_now, head):
        """Every tx sent with nonce k..nonce_now: bisect the block where the nonce reaches k, read it with trace_filter."""
        native, internal = [], []
        seen = {}
        while k <= nonce_now:
            lo = max([lo] + [b for b, n in seen.items() if n < k])
            hi = min([head] + [b for b, n in seen.items() if n >= k and b > lo])
            block = self.first_block_where("eth_getTransactionCount", address, lambda n, k=k: n >= k, lo, hi, seen)
            rows_n, rows_i, sent = self._trace_rows(block, address, "out")
            native.extend(rows_n)
            internal.extend(rows_i)
            k += max(1, sent)
            lo = block
        return native, internal

    def outgoing_native(self, address, after_block):
        """ETH a wallet sent after a block, or None when it has too many txs to walk."""
        head = self.head_block()
        nonce_now = parse_int(self.rpc.call("eth_getTransactionCount", [address, hex(head)]))
        if nonce_now > NONCE_WALK_MAX:
            return None
        if not self.chain.get("trace"):
            return None
        start = min(max(after_block, 0), head)
        nonce_then = parse_int(self.rpc.call("eth_getTransactionCount", [address, hex(start)]))
        rows, _ = self._walk_sends(address, nonce_then + 1, start, nonce_now, head)
        return rows

    def token_link_window(self):
        head = self.head_block()
        start = self.creation_block if self.creation_block is not None else head
        span = min(100_000, head)
        t_head = self.block_timestamp(head)
        t_back = self.block_timestamp(head - span)
        per_s = span / max(1, t_head - t_back)
        lookback = int(TOKEN_LINK_LOOKBACK_DAYS * 86400 * per_s)
        return max(0, start - lookback), head

    def token_links(self, persons):
        if not persons:
            return [], "token links: no people to check"
        start, head = self.token_link_window()
        names = dict(self.chain.get("link_tokens", {}))
        tokens = [self.token] + [t for t in sorted(names) if t != self.token]
        topics = [topic_address(a) for a in persons]
        max_blocks = MAX_LOG_QUERIES * self.chain["log_range"]
        cut = ""
        if head - start + 1 > max_blocks:
            start = head - max_blocks + 1
            cut = " (cut to the query cap)"
        logs, queries = self.scan_logs({"address": tokens, "topics": [TRANSFER_TOPIC, topics, topics]}, start, head)
        label = ", ".join(["this coin"] + [names[t] for t in tokens[1:]])
        text = (
            f"token links: {label} transfers between people, blocks {start:,}–{head:,}{cut} "
            f"({queries} queries); other tokens not read (free RPC needs contract addresses)"
        )
        return self._transfer_rows(logs), text

    def fetch_histories(self, persons):
        head = self.head_block()
        rows_by_person = {p: {"txlist": [], "txlistinternal": [], "tokentx": []} for p in persons}
        notes = []
        truncated = []
        walked = 0
        if self.chain.get("trace"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=RPC_WORKERS) as pool:
                futures = {pool.submit(self._native_history, p, head): p for p in persons}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        address, row_map, nonce_now, local_notes = fut.result()
                    except RpcStateError as exc:
                        truncated.append(futures[fut])
                        notes.append(f"{futures[fut]}: ETH history not read, the RPC kept answering without old state ({exc}).")
                        continue
                    rows_by_person[address] = row_map
                    notes.extend(local_notes)
                    if 0 < nonce_now <= NONCE_WALK_MAX:
                        walked += 1
            truncated.sort()
            missing = f"; {len(truncated)} not read (RPC had no old state)" if truncated else ""
            self.coverage.append(
                f"native: first ETH funding of {len(persons)} people (archive balance bisection + trace_filter); "
                f"outgoing sends of the {walked} with ≤{NONCE_WALK_MAX} txs; other ETH in/out not read{missing}"
            )
        else:
            self.coverage.append("native: not read (no free address-history or trace source for this chain)")
        token_rows, text = self.token_links(persons)
        self.coverage.append(text)
        for row in token_rows:
            for side in (row["from"], row["to"]):
                if side in rows_by_person:
                    rows_by_person[side]["tokentx"].append(row)
        return rows_by_person, truncated, notes


B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ED25519_P = 2**255 - 19
ED25519_D = -121665 * pow(121666, ED25519_P - 2, ED25519_P) % ED25519_P


def b58decode32(text):
    n = 0
    for ch in text:
        n = n * 58 + B58_ALPHABET.index(ch)
    return n.to_bytes(32, "big")


def b58encode(raw):
    n, text = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        text = B58_ALPHABET[r] + text
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + text


def wsol_account(owner):
    """The owner's associated wrapped-SOL account: SOL sent there is wrapped for a trade, not sent away."""
    seeds = b58decode32(owner) + b58decode32(SOLANA_TOKEN_PROGRAM) + b58decode32(WSOL_MINT)
    program = b58decode32(SOLANA_ATA_PROGRAM)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(seeds + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not on_ed25519_curve(digest):
            return b58encode(digest)
    return ""


def on_ed25519_curve(raw):
    """Wallet keys are curve points; program-derived addresses (pools, vaults) are deliberately off the curve."""
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= ED25519_P:
        return False
    x2 = (y * y - 1) * pow(ED25519_D * y * y + 1, ED25519_P - 2, ED25519_P) % ED25519_P
    return x2 == 0 or pow(x2, (ED25519_P - 1) // 2, ED25519_P) == 1


def compact_solana_tx(tx, mint):
    """The parts of a jsonParsed transaction the map uses: SOL transfers and this mint's change per owner."""
    meta = tx.get("meta") or {}
    body = tx.get("transaction") or {}
    msg = body.get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    instructions = list(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        instructions += inner.get("instructions") or []
    xfers = []
    for ins in instructions:
        parsed = ins.get("parsed") if isinstance(ins, dict) else None
        if not isinstance(parsed, dict) or ins.get("program") != "system":
            continue
        info = parsed.get("info") or {}
        if parsed.get("type") in ("transfer", "transferWithSeed"):
            xfers.append([info.get("source", ""), info.get("destination", ""), int(info.get("lamports") or 0)])
    tok = collections.Counter()
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for bal in meta.get(key) or []:
            if bal.get("mint") == mint and bal.get("owner"):
                tok[bal["owner"]] += sign * int((bal.get("uiTokenAmount") or {}).get("amount") or 0)
    return {
        "sig": (body.get("signatures") or [""])[0],
        "slot": tx.get("slot") or 0,
        "ts": tx.get("blockTime") or 0,
        "payer": keys[0] if keys else "",
        "xfer": [x for x in xfers if x[0] and x[1] and x[2] > 0],
        "tok": {o: v for o, v in tok.items() if v},
    }


def solana_token_rows(ctx, mint):
    """Owner-to-owner transfer rows for one transaction, splitting each receipt across the senders pro rata."""
    neg = {o: -v for o, v in ctx["tok"].items() if v < 0}
    pos = {o: v for o, v in ctx["tok"].items() if v > 0}
    total_out = sum(neg.values())
    rows = []
    for receiver, amount in sorted(pos.items()):
        for sender, out in sorted(neg.items()):
            part = amount * out // total_out if total_out else 0
            if part > 0:
                rows.append({
                    "from": sender, "to": receiver, "value": str(part), "hash": ctx["sig"],
                    "blockNumber": str(ctx["slot"]), "timeStamp": str(ctx["ts"]), "contractAddress": mint,
                })
    return rows


def solana_native_rows(ctx):
    return [
        {"from": s, "to": d, "value": str(lamports * LAMPORTS_TO_WEI), "hash": ctx["sig"], "blockNumber": str(ctx["slot"]), "timeStamp": str(ctx["ts"])}
        for s, d, lamports in ctx["xfer"]
    ]


class SolanaSource:
    """Holders, launch trades and each person's oldest transactions for one mint, via Helius.

    Every answer is stored compacted in `memo` (and the disk cache), so a run can be saved as a small fixture and
    replayed with no network.
    """

    def __init__(self, rpc, memo=None, replay=False):
        self.rpc = rpc
        self.memo = {} if memo is None else memo
        self.replay = replay
        self.token = ""
        self.creator = ""
        self.coverage = []
        self.notes = []

    def remember(self, key, fetch):
        k = json.dumps(key, separators=(",", ":"))
        if k not in self.memo:
            if self.replay:
                raise AssertionError("request not in fixture: " + k[:200])
            self.memo[k] = self._fetch(key, fetch)
        value = self.memo[k]
        if isinstance(value, dict) and set(value) == {"failed"}:
            raise RuntimeError(value["failed"])
        return value

    def _fetch(self, key, fetch):
        cache = getattr(self.rpc, "cache", None)
        value = cache.get("solana", key, max_age_s=CACHE_MAX_AGE_S) if cache is not None else None
        if value is not None:
            return value
        for attempt in range(SOL_RETRY_TRIES):
            try:
                value = fetch()
                break
            except RuntimeError as exc:
                if "429" not in str(exc) or attempt == SOL_RETRY_TRIES - 1:
                    return {"failed": str(exc)}
                time.sleep(SOL_RETRY_SLEEP_S * 2 ** attempt)
        if cache is not None:
            cache.set("solana", key, value)
        return value

    def _page(self, address, token=None, after_slot=None):
        opts = {
            "transactionDetails": "full", "sortOrder": "asc", "limit": SOL_PAGE, "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 1, "filters": {"status": "succeeded"},
        }
        if after_slot is not None:
            opts["filters"]["slot"] = {"gt": after_slot}
        if token:
            opts["paginationToken"] = token
        return self.rpc.call("getTransactionsForAddress", [address, opts], cacheable=False) or {}

    def oldest(self, address, pages=1, until_ts=None):
        def fetch():
            txs, token, more = [], None, False
            for _ in range(pages):
                page = self._page(address, token)
                data = page.get("data") or []
                txs += [compact_solana_tx(t, self.token) for t in data]
                token = page.get("paginationToken")
                if not token or len(data) < SOL_PAGE:
                    token = None
                    break
                if until_ts is not None and txs and txs[-1]["ts"] > until_ts(txs[0]["ts"]):
                    break
            more = bool(token)
            return {"txs": txs, "more": more}

        return self.remember(["oldest", address, self.token, pages], fetch)

    def outgoing_native(self, address, after_slot):
        """SOL transfers in the wallet's first SOL_FOLLOW_PAGES pages of transactions after after_slot (follow-forward)."""
        def fetch():
            txs, token = [], None
            for _ in range(SOL_FOLLOW_PAGES):
                page = self._page(address, token, after_slot)
                data = page.get("data") or []
                txs += [compact_solana_tx(t, self.token) for t in data]
                token = page.get("paginationToken")
                if not token or len(data) < SOL_PAGE:
                    token = None
                    break
            return {"txs": txs, "more": bool(token)}

        page = self.remember(["after", address, self.token, after_slot, SOL_FOLLOW_PAGES], fetch)
        own_wsol = wsol_account(address)
        return [row for ctx in page["txs"] for row in solana_native_rows(ctx) if row["to"] != own_wsol]

    def get_token(self, mint):
        self.token = mint

        def fetch():
            res = self.rpc.call("getAsset", {"id": mint}) or {}
            info = res.get("token_info") or {}
            meta = (res.get("content") or {}).get("metadata") or {}
            return {"symbol": meta.get("symbol") or info.get("symbol") or "", "decimals": info.get("decimals") or 0, "supply": info.get("supply") or 0}

        asset = self.remember(["asset", mint], fetch)
        return {"symbol": asset["symbol"] or "UNKNOWN", "decimals": str(asset["decimals"]), "totalSupply": str(asset["supply"])}

    def get_token_holders(self, mint, top):
        def fetch():
            owners, accounts, full = collections.Counter(), 0, True
            for page in range(1, SOL_HOLDER_PAGES + 1):
                res = self.rpc.call("getTokenAccounts", {"mint": mint, "limit": 1000, "page": page}) or {}
                rows = res.get("token_accounts") or []
                accounts += len(rows)
                for row in rows:
                    owners[row.get("owner", "")] += int(row.get("amount") or 0)
                if len(rows) < 1000:
                    break
            else:
                full = False
            return {"owners": [[o, v] for o, v in owners.most_common() if o and v > 0], "accounts": accounts, "complete": full}

        data = self.remember(["holders", mint], fetch)
        self.coverage.append(
            f"holders: {data['accounts']:,} token accounts via Helius DAS"
            + ("" if data["complete"] else f" (first {SOL_HOLDER_PAGES}k only)")
        )
        return [{"address": o, "value": str(v)} for o, v in data["owners"][:top]]

    def get_token_transfers(self, mint):
        launch = self.oldest(mint, pages=SOL_LAUNCH_PAGES, until_ts=lambda t0: t0 + LAUNCH_WINDOW_S)
        txs = launch["txs"]
        if txs:
            self.creator = txs[0]["payer"]
        rows = []
        for ctx in txs:
            rows += solana_token_rows(ctx, mint)
        return rows

    def token_accounts(self, owner):
        def fetch():
            res = self.rpc.call("getTokenAccountsByOwner", [owner, {"mint": self.token}, {"encoding": "jsonParsed"}]) or {}
            return [a.get("pubkey", "") for a in res.get("value") or [] if a.get("pubkey")]

        return self.remember(["accounts", owner, self.token], fetch)

    def _history(self, person):
        own = self.oldest(person)
        pages = [own] + [self.oldest(acct) for acct in self.token_accounts(person)]
        native, tokens, seen = [], [], set()
        for ctx in own["txs"]:
            for row in solana_native_rows(ctx):
                if person in (row["from"], row["to"]):
                    native.append(row)
        for page in pages:
            for ctx in page["txs"]:
                for row in solana_token_rows(ctx, self.token):
                    key = (row["hash"], row["from"], row["to"])
                    if person in (row["from"], row["to"]) and key not in seen:
                        seen.add(key)
                        tokens.append(row)
        truncated = any(page["more"] for page in pages)
        return person, {"txlist": native, "txlistinternal": [], "tokentx": tokens}, truncated

    def fetch_histories(self, persons):
        rows_by_person, truncated, notes = {}, [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=RPC_WORKERS) as pool:
            futures = {pool.submit(self._history, p): p for p in persons}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    person, row_map, cut = fut.result()
                except RuntimeError as exc:
                    person = futures[fut]
                    truncated.append(person)
                    rows_by_person[person] = {"txlist": [], "txlistinternal": [], "tokentx": []}
                    notes.append(f"{person}: history not read ({exc}).")
                    continue
                rows_by_person[person] = row_map
                if cut:
                    truncated.append(person)
        truncated.sort()
        self.coverage.append(
            f"native: SOL transfers in each person's first {SOL_PAGE} transactions (Helius, oldest first)"
        )
        self.coverage.append(
            f"token links: this coin's moves in each person's wallet and token-account history (first {SOL_PAGE} transactions each); "
            "other tokens not read"
        )
        if truncated:
            notes.append(f"{len(truncated)} people have more than {SOL_PAGE} transactions; only their oldest were read.")
        return rows_by_person, [], notes

    def kinds(self, addresses):
        need = [a for a in addresses if json.dumps(["kind", a], separators=(",", ":")) not in self.memo]
        on_curve = []
        for a in need:
            try:
                curve = on_ed25519_curve(b58decode32(a))
            except (ValueError, OverflowError):
                curve = False
            if curve:
                on_curve.append(a)
            else:
                self.remember(["kind", a], lambda: "program")
        if on_curve and self.replay:
            raise AssertionError("request not in fixture: kind of " + on_curve[0])
        for i in range(0, len(on_curve), 100):
            part = on_curve[i : i + 100]
            res = self.rpc.call("getMultipleAccounts", [part, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]) or {}
            accounts = res.get("value") or [None] * len(part)
            for a, acc in zip(part, accounts):
                kind = "person" if acc is None or acc.get("owner") == SOLANA_SYSTEM_PROGRAM else "program"
                self.remember(["kind", a], lambda kind=kind: kind)
        return {a: self.remember(["kind", a], lambda: "program") for a in addresses}

    def signature_count(self, address):
        """Signatures up to SOL_BUSY_SIGNATURES; a wallet that did that many within SOL_EXCHANGE_SPAN_S counts as past
        SUPERNODE_NONCE, since the capped count alone can't tell a busy trader from an exchange hot wallet."""
        def fetch():
            sigs = self.rpc.call("getSignaturesForAddress", [address, {"limit": SOL_BUSY_SIGNATURES}], cacheable=False) or []
            times = [s["blockTime"] for s in sigs if s.get("blockTime")]
            if len(sigs) >= SOL_BUSY_SIGNATURES and times and max(times) - min(times) < SOL_EXCHANGE_SPAN_S:
                return SUPERNODE_NONCE
            return len(sigs)

        return self.remember(["sigs-span", address], fetch)


class SolanaLabels:
    """The EVM label calls the map makes (contract? how many txs?), answered for Solana accounts."""

    batch_size = 1

    def __init__(self, source):
        self.source = source

    def get_code_many(self, addresses):
        kinds = self.source.kinds(list(addresses))
        return ["0x60" if kinds.get(a) == "program" else "0x" for a in addresses]

    def get_nonce_many(self, addresses):
        with concurrent.futures.ThreadPoolExecutor(max_workers=RPC_WORKERS) as pool:
            return list(pool.map(self.source.signature_count, addresses))

    def get_tx_many(self, hashes):
        return [None] * len(hashes)


class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}
        self.rank = {x: 0 for x in items}

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while item != root:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, a, b):
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
            return
        if self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
            return
        self.parent[rb] = ra
        self.rank[ra] += 1

    def groups(self):
        out = collections.defaultdict(list)
        for item in self.parent:
            out[self.find(item)].append(item)
        return out


def classify_holders(raw_holders, rpc_client):
    holders = []
    addr_for_code = []
    for row in raw_holders:
        addr = normalize_address(row.get("address", ""))
        if not addr:
            continue
        value_raw = parse_int(row.get("value"))
        kind = "burn" if is_burn_address(addr) else None
        holders.append({"address": addr, "value_raw": value_raw, "kind": kind})
        if kind is None:
            addr_for_code.append(addr)
    codes = rpc_client.get_code_many(addr_for_code) if addr_for_code else []
    code_by_addr = {}
    for addr, code in zip(addr_for_code, codes):
        code_by_addr[addr] = code
    for row in holders:
        if row["kind"] == "burn":
            continue
        if is_contract_code(code_by_addr.get(row["address"], "0x")):
            row["kind"] = "contract"
        else:
            row["kind"] = "person"
    return holders


def mark_exchange_holders(holders, rpc_client):
    """A holder wallet at exchange scale pays thousands of unrelated customers; as a person it would join them all."""
    wallets = [row["address"] for row in holders if row["kind"] == "person"]
    if not wallets:
        return
    for addr, nonce in zip(wallets, rpc_client.get_nonce_many(wallets)):
        if parse_int(nonce) >= SUPERNODE_NONCE:
            for row in holders:
                if row["address"] == addr:
                    row["kind"] = "exchange"


def discover_launch_window(token, total_supply_raw, blockscout_client, rpc_client):
    transfers = blockscout_client.get_token_transfers(token)
    if not transfers:
        return {
            "creator": "",
            "launch_buyers": {},
            "launch_bought_total_share": 0.0,
            "first_trade_timestamp": None,
            "window_end_timestamp": None,
        }
    addresses = set()
    normalized = []
    for row in transfers:
        frm = normalize_address(row.get("from", ""))
        to = normalize_address(row.get("to", ""))
        ts = parse_int(row.get("timeStamp"))
        value_raw = parse_int(row.get("value"))
        if not frm or not to or ts <= 0 or value_raw <= 0:
            continue
        addresses.add(frm)
        addresses.add(to)
        normalized.append(
            {"from": frm, "to": to, "timestamp": ts, "block": parse_int(row.get("blockNumber")), "value_raw": value_raw, "hash": row.get("hash", "")}
        )
    if not normalized:
        return {
            "creator": "",
            "launch_buyers": {},
            "launch_bought_total_share": 0.0,
            "first_trade_timestamp": None,
            "window_end_timestamp": None,
        }
    code_rows = rpc_client.get_code_many(sorted(addresses))
    is_contract = {}
    for addr, code in zip(sorted(addresses), code_rows):
        is_contract[addr] = is_contract_code(code)

    creator = getattr(blockscout_client, "creator", "") or ""
    for row in normalized:
        if creator:
            break
        if row["from"] == ZERO_ADDRESS:
            creator = row["to"]
    if is_contract.get(creator, False):
        # A launcher or factory contract that received the mint is plumbing, not a person to cluster around.
        creator = ""

    first_trade_ts = None
    first_trade_block = None
    for row in normalized:
        if is_contract.get(row["from"], False) and not is_contract.get(row["to"], False):
            first_trade_ts = row["timestamp"]
            first_trade_block = row["block"]
            break

    launch_buyers_raw = collections.Counter()
    first_buy = {}
    window_end = None
    if first_trade_ts is not None:
        window_end = first_trade_ts + LAUNCH_WINDOW_S
        for row in normalized:
            if row["timestamp"] < first_trade_ts or row["timestamp"] > window_end:
                continue
            if is_contract.get(row["from"], False) and not is_contract.get(row["to"], False):
                launch_buyers_raw[row["to"]] += row["value_raw"]
                if row["to"] not in first_buy:
                    first_buy[row["to"]] = row

    buy_hashes = sorted(
        {norm_hash(r["hash"]) for r in normalized if is_contract.get(r["from"], False) and not is_contract.get(r["to"], False)}
    )
    launch_buyers = {}
    for addr, value_raw in launch_buyers_raw.items():
        launch_buyers[addr] = float(value_raw) / float(total_supply_raw)
    launch_total = sum(launch_buyers.values())
    launch_buys = {
        addr: {
            "person": addr,
            "from": row["from"],
            "timestamp": row["timestamp"],
            "block": row["block"],
            "hash": row["hash"],
            "share": launch_buyers[addr],
        }
        for addr, row in first_buy.items()
    }
    return {
        "creator": creator,
        "launch_buyers": launch_buyers,
        "launch_buys": launch_buys,
        "buy_hashes": buy_hashes,
        "launch_bought_total_share": launch_total,
        "first_trade_timestamp": first_trade_ts,
        "first_trade_block": first_trade_block,
        "window_end_timestamp": window_end,
    }


def fetch_histories(persons, blockscout_client):
    rows_by_person = {}
    truncated = []
    notes = []
    txlist_cut = set()
    failed = set()

    def pull_one(address):
        row_map = {}
        was_truncated = False
        local_notes = []
        for action in ("txlist", "txlistinternal", "tokentx"):
            rows, is_truncated, note = blockscout_client.get_history(address, action)
            row_map[action] = rows
            if is_truncated:
                was_truncated = True
                if action == "txlist":
                    txlist_cut.add(address)
            if note:
                local_notes.append(note)
        return address, row_map, was_truncated, local_notes

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(pull_one, addr): addr for addr in persons}
        for fut in concurrent.futures.as_completed(futures):
            try:
                address, row_map, was_truncated, local_notes = fut.result()
            except RuntimeError as exc:
                person = futures[fut]
                rows_by_person[person] = {"txlist": [], "txlistinternal": [], "tokentx": []}
                truncated.append(person)
                failed.add(person)
                notes.append(f"{person}: history not read ({exc}).")
                continue
            rows_by_person[address] = row_map
            if was_truncated:
                truncated.append(address)
            notes.extend(local_notes)
    # Anyone can emit Transfer(from=holder) from a fake token; only count token rows the holder's own tx produced.
    # A tx list cut at the row cap proves nothing about later sends, so those are kept.
    sent = {p: set() for p in persons}
    sent_until = {}
    for person, row_map in rows_by_person.items():
        for row in row_map["txlist"]:
            if normalize_address(row.get("from", "")) == person:
                sent[person].add(norm_hash(row.get("hash") or row.get("transactionHash")))
        if person in txlist_cut:
            sent_until[person] = max((parse_int(r.get("timeStamp")) for r in row_map["txlist"]), default=0)
    dropped = set()
    for row_map in rows_by_person.values():
        kept = []
        for row in row_map["tokentx"]:
            frm = normalize_address(row.get("from", ""))
            tx_hash = norm_hash(row.get("hash") or row.get("transactionHash"))
            if (frm in sent and frm not in failed and tx_hash not in sent[frm]
                    and parse_int(row.get("timeStamp")) < sent_until.get(frm, float("inf"))):
                dropped.add((tx_hash, frm, normalize_address(row.get("to", ""))))
            else:
                kept.append(row)
        row_map["tokentx"] = kept
    if len(dropped) == 1:
        notes.append("1 token transfer was not counted as a link: the holder shown as sender did not send the transaction "
                     "(look-alike spam, or sent through a relayer).")
    elif dropped:
        notes.append(f"{len(dropped)} token transfers were not counted as links: the holder shown as sender did not send "
                     "the transaction (look-alike spam, or sent through a relayer).")
    return rows_by_person, sorted(truncated), notes


def update_first_native(first_native, person, event):
    old = first_native.get(person)
    if old is None:
        first_native[person] = event
        return
    old_key = (old["timestamp"], old["block"], old["hash"], old["from"])
    new_key = (event["timestamp"], event["block"], event["hash"], event["from"])
    if new_key < old_key:
        first_native[person] = event


def build_interactions(persons, holders_set, histories):
    persons_set = set(persons)
    directed = {}
    counterparties = {}
    first_native = {}
    seen = set()

    def get_direct(a, b):
        key = (a, b)
        if key not in directed:
            directed[key] = {"count": 0, "hashes": [], "kinds": set(), "first_timestamp": None, "last_timestamp": None}
        return directed[key]

    def get_counterparty(cp):
        if cp not in counterparties:
            counterparties[cp] = {
                "persons": set(),
                "sent_to_persons": set(),
                "received_from_persons": set(),
                "native_sent_to_persons": set(),
                "native_received_from_persons": set(),
                "hashes": [],
                "first_timestamp": None,
                "last_timestamp": None,
            }
        return counterparties[cp]

    for person, row_map in histories.items():
        for action in ("txlist", "txlistinternal", "tokentx"):
            rows = row_map.get(action) or []
            for row in rows:
                frm = normalize_address(row.get("from", ""))
                to = normalize_address(row.get("to", ""))
                if not frm or not to or frm == to:
                    continue
                tx_hash = norm_hash(row.get("hash") or row.get("transactionHash"))
                block = parse_int(row.get("blockNumber"))
                ts = parse_int(row.get("timeStamp"))
                value = parse_int(row.get("value"))
                contract_addr = normalize_address(row.get("contractAddress", ""))
                is_native = action in ("txlist", "txlistinternal") and value > 0
                is_token = action == "tokentx" and value > 0
                if not is_native and not is_token:
                    continue
                dedupe_key = (action, tx_hash, frm, to, value, contract_addr, block)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                from_person = frm in persons_set
                to_person = to in persons_set
                kind_name = "native" if is_native else "token"
                if from_person and to_person:
                    slot = get_direct(frm, to)
                    slot["count"] += 1
                    slot["kinds"].add(kind_name)
                    add_unique_sample(slot["hashes"], tx_hash)
                    if slot["first_timestamp"] is None or ts < slot["first_timestamp"]:
                        slot["first_timestamp"] = ts
                    if slot["last_timestamp"] is None or ts > slot["last_timestamp"]:
                        slot["last_timestamp"] = ts
                if from_person and to not in holders_set and not is_burn_address(to):
                    if to in persons_set:
                        continue
                    cp = get_counterparty(to)
                    cp["persons"].add(frm)
                    cp["received_from_persons"].add(frm)
                    if is_native:
                        cp["native_received_from_persons"].add(frm)
                    add_unique_sample(cp["hashes"], tx_hash)
                    if cp["first_timestamp"] is None or ts < cp["first_timestamp"]:
                        cp["first_timestamp"] = ts
                    if cp["last_timestamp"] is None or ts > cp["last_timestamp"]:
                        cp["last_timestamp"] = ts
                # A token a stranger sends you (airdrop spam) says nothing about who owns the wallet; only native funding counts inbound.
                if to_person and frm not in holders_set and not is_burn_address(frm) and is_native:
                    if frm in persons_set:
                        continue
                    cp = get_counterparty(frm)
                    cp["persons"].add(to)
                    cp["sent_to_persons"].add(to)
                    if is_native:
                        cp["native_sent_to_persons"].add(to)
                    add_unique_sample(cp["hashes"], tx_hash)
                    if cp["first_timestamp"] is None or ts < cp["first_timestamp"]:
                        cp["first_timestamp"] = ts
                    if cp["last_timestamp"] is None or ts > cp["last_timestamp"]:
                        cp["last_timestamp"] = ts
                    if is_native:
                        update_first_native(
                            first_native,
                            to,
                            {"person": to, "from": frm, "hash": tx_hash, "block": block, "timestamp": ts},
                        )
    return directed, counterparties, first_native


def merge_directed_links(directed):
    pairs = []
    handled = set()
    for (src, dst), info in sorted(directed.items()):
        key = tuple(sorted((src, dst)))
        if key in handled:
            continue
        handled.add(key)
        rev = directed.get(
            (dst, src),
            {"count": 0, "hashes": [], "kinds": set(), "first_timestamp": None, "last_timestamp": None},
        )
        hashes = []
        for h in info["hashes"] + rev["hashes"]:
            add_unique_sample(hashes, h)
        kinds = sorted(info["kinds"] | rev["kinds"])
        timestamps = [x for x in (info.get("first_timestamp"), rev.get("first_timestamp")) if x is not None]
        first_ts = min(timestamps) if timestamps else None
        timestamps = [x for x in (info.get("last_timestamp"), rev.get("last_timestamp")) if x is not None]
        last_ts = max(timestamps) if timestamps else None
        pairs.append(
            {
                "a": key[0],
                "b": key[1],
                "ab_count": info["count"],
                "ba_count": rev["count"],
                "total_count": info["count"] + rev["count"],
                "both_ways": info["count"] > 0 and rev["count"] > 0,
                "hashes": hashes,
                "kinds": kinds,
                "first_timestamp": first_ts,
                "last_timestamp": last_ts,
            }
        )
    return pairs


def resolve_internal_origins(histories, persons, rpc_client, per_person=ORIGIN_RESOLVE_PER_PERSON):
    """ETH a contract passes on (multisend, disperse) is credited to the wallet that sent the tx.

    Only each person's earliest few inbound internal transfers from contracts are resolved (funding sits there);
    a swap's ETH out comes back to the person's own tx and is left alone.
    """
    persons_set = set(persons)
    candidates = []
    for person in sorted(histories):
        rows = []
        for row in histories[person].get("txlistinternal") or []:
            frm = normalize_address(row.get("from", ""))
            tx_hash = str(row.get("hash") or row.get("transactionHash") or "").lower()
            if normalize_address(row.get("to", "")) != person or parse_int(row.get("value")) <= 0:
                continue
            if not frm or frm in persons_set or not HEX_64_RE.fullmatch(tx_hash):
                continue
            rows.append((row, tx_hash))
        rows.sort(key=lambda x: (parse_int(x[0].get("blockNumber")), parse_int(x[0].get("timeStamp"))))
        candidates.extend((person, row, tx_hash) for row, tx_hash in rows[:per_person])
    if not candidates:
        return 0
    froms = sorted({normalize_address(r["from"]) for _, r, _ in candidates})
    codes = rpc_client.get_code_many(froms)
    contracts = {a for a, c in zip(froms, codes) if is_contract_code(c)}
    hashes = sorted({h for _, r, h in candidates if normalize_address(r["from"]) in contracts})
    if not hashes:
        return 0
    txs = rpc_client.get_tx_many(hashes)
    origin_by_hash = {h: normalize_address((tx or {}).get("from", "")) for h, tx in zip(hashes, txs)}
    resolved = 0
    for person, row, tx_hash in candidates:
        via = normalize_address(row["from"])
        origin = origin_by_hash.get(tx_hash, "")
        if via not in contracts or not origin or origin in (person, via):
            continue
        histories[person]["txlistinternal"] = [r for r in histories[person]["txlistinternal"] if r is not row]
        synthetic = dict(row)
        synthetic["from"] = origin
        synthetic["hash"] = tx_hash
        synthetic["via"] = via
        histories[person]["txlist"] = list(histories[person].get("txlist") or []) + [synthetic]
        resolved += 1
    return resolved


def label_counterparties(counterparties, first_native, rpc_client):
    need_label = set()
    for addr, info in counterparties.items():
        if len(info["persons"]) >= 2:
            need_label.add(addr)
    for event in first_native.values():
        need_label.add(event["from"])
    addresses = sorted(need_label)
    if not addresses:
        return {}
    codes = rpc_client.get_code_many(addresses)
    nonces = rpc_client.get_nonce_many(addresses)
    labels = {}
    for addr, code, nonce in zip(addresses, codes, nonces):
        contract = is_contract_code(code)
        labels[addr] = {
            "address": addr,
            "code": code,
            "nonce": nonce,
            "is_contract": contract,
            "is_busy": contract or nonce >= BUSY_NONCE,
            "is_busy_eoa": (not contract) and nonce >= BUSY_NONCE,
        }
    return labels


def classify_magic_nodes(counterparties, labels):
    nodes = []
    for addr in sorted(counterparties):
        info = counterparties[addr]
        if len(info["persons"]) < 2:
            continue
        label = labels.get(addr, {"is_contract": False, "is_busy": False})
        loop_wallets = sorted(info["native_sent_to_persons"] & info["native_received_from_persons"])
        if label.get("is_contract"):
            continue
        # Exchange hot wallets pay users but users pay exchanges through their own deposit addresses, so a busy
        # wallet that funded a holder and got ETH back from that same holder is an operator, not an exchange.
        if label.get("is_busy") and not (loop_wallets and label.get("nonce", 0) < SUPERNODE_NONCE):
            continue
        sent_native = bool(info["native_sent_to_persons"])
        recv_native = bool(info["native_received_from_persons"])
        if sent_native and recv_native:
            role = "both"
        elif sent_native:
            role = "funder"
        elif recv_native:
            role = "collector"
        elif info["sent_to_persons"] and info["received_from_persons"]:
            role = "both"
        elif info["sent_to_persons"]:
            role = "funder"
        else:
            role = "collector"
        nodes.append(
            {
                "id": f"magic-{len(nodes)+1}",
                "address": addr,
                "wallets": sorted(info["persons"]),
                "hashes": list(info["hashes"]),
                "role": role,
                "funded": sorted(info["native_sent_to_persons"]),
                "paid_back_by": sorted(info["native_received_from_persons"]),
                "loop_wallets": loop_wallets,
                "nonce": label.get("nonce", 0),
                "busy": bool(label.get("is_busy")),
                "first_timestamp": info.get("first_timestamp"),
                "last_timestamp": info.get("last_timestamp"),
            }
        )
    return nodes


def build_time_groups(events, min_people, min_distinct_funders=None):
    if not events:
        return []
    ordered = sorted(events, key=lambda x: (x["timestamp"], x["person"], x["from"]))
    groups = []
    i = 0
    while i < len(ordered):
        start_ts = ordered[i]["timestamp"]
        j = i
        persons = set()
        funders = set()
        while j < len(ordered) and ordered[j]["timestamp"] - start_ts <= TIME_WINDOW_S:
            persons.add(ordered[j]["person"])
            funders.add(ordered[j]["from"])
            j += 1
        enough_people = len(persons) >= min_people
        enough_funders = True
        if min_distinct_funders is not None:
            enough_funders = len(funders) >= min_distinct_funders
        if enough_people and enough_funders:
            groups.append(ordered[i:j])
            i = j
        else:
            i += 1
    return groups


def hhmmss(ts):
    return time.strftime("%H:%M:%S", time.gmtime(max(0, ts)))


def classify_time_nodes(first_native, labels, magic_addresses=(), launch_ts=None):
    by_funder = collections.defaultdict(list)
    busy_events = []
    skip = set(magic_addresses)
    for person, event in sorted(first_native.items()):
        label = labels.get(event["from"], {"is_busy_eoa": False, "is_contract": False})
        if label.get("is_contract") or event["from"] in skip:
            continue
        if label.get("is_busy_eoa"):
            by_funder[event["from"]].append(event)
        # Quiet funders that paid 2+ people are magic nodes (skipped above); the rest are one-off relay wallets,
        # which a wave counts as much as exchange withdrawals.
        busy_events.append(event)

    # After launch, strangers bridging in or withdrawing from the same exchange to buy the same hot coin land
    # minutes apart too; only pre-launch prep counts, for both rules below.
    nodes = []
    for funder, events in sorted(by_funder.items()):
        for group in build_time_groups(events, min_people=2):
            if launch_ts is not None and max(x["timestamp"] for x in group) > launch_ts:
                continue
            wallets = sorted({x["person"] for x in group})
            stamps = [x["timestamp"] for x in group]
            hashes = []
            for row in group:
                add_unique_sample(hashes, row["hash"])
            label_text = f"{hhmmss(min(stamps))}-{hhmmss(max(stamps))}"
            nodes.append(
                {
                    "id": f"time-funder-{len(nodes)+1}",
                    "subtype": "busy-funder-window",
                    "wallets": wallets,
                    "funder_addresses": [funder],
                    "window_start": min(stamps),
                    "window_end": max(stamps),
                    "window_label": label_text,
                    "hashes": hashes,
                    "first_timestamp": min(stamps),
                    "last_timestamp": max(stamps),
                }
            )

    for group in build_time_groups(busy_events, min_people=3, min_distinct_funders=2):
        if launch_ts is not None and max(x["timestamp"] for x in group) > launch_ts:
            continue
        wallets = sorted({x["person"] for x in group})
        funders = sorted({x["from"] for x in group})
        stamps = [x["timestamp"] for x in group]
        hashes = []
        for row in group:
            add_unique_sample(hashes, row["hash"])
        label_text = f"{hhmmss(min(stamps))}-{hhmmss(max(stamps))}"
        nodes.append(
            {
                "id": f"time-wave-{len(nodes)+1}",
                "subtype": "funding-wave",
                "wallets": wallets,
                "funder_addresses": funders,
                "window_start": min(stamps),
                "window_end": max(stamps),
                "window_label": label_text,
                "hashes": hashes,
                "first_timestamp": min(stamps),
                "last_timestamp": max(stamps),
            }
        )
    return nodes


def classify_launch_bursts(launch_buys):
    """Launch buys of near-equal size landing within seconds of each other (a bundle split across wallets)."""
    ordered = sorted(launch_buys.values(), key=lambda x: (x["timestamp"], x["block"], x["person"]))
    nodes = []
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j]["timestamp"] - ordered[i]["timestamp"] <= BURST_WINDOW_S:
            j += 1
        big = [x for x in ordered[i:j] if x["share"] >= BURST_MIN_SHARE]
        group = []
        if len(big) >= BURST_MIN_WALLETS:
            shares = sorted(x["share"] for x in big)
            median = shares[len(shares) // 2]
            group = [x for x in big if abs(x["share"] - median) <= BURST_SIZE_TOLERANCE * median]
        if len(group) >= BURST_MIN_WALLETS:
            stamps = [x["timestamp"] for x in group]
            blocks = [x["block"] for x in group]
            hashes = []
            for row in group:
                add_unique_sample(hashes, row["hash"])
            nodes.append(
                {
                    "id": f"time-burst-{len(nodes)+1}",
                    "subtype": "launch-burst",
                    "wallets": sorted({x["person"] for x in group}),
                    "funder_addresses": sorted({x["from"] for x in group}),
                    "window_start": min(stamps),
                    "window_end": max(stamps),
                    "window_label": f"{hhmmss(min(stamps))}-{hhmmss(max(stamps))}",
                    "block_first": min(blocks),
                    "block_last": max(blocks),
                    "share_total": sum(x["share"] for x in group),
                    "hashes": hashes,
                    "first_timestamp": min(stamps),
                    "last_timestamp": max(stamps),
                }
            )
            i = j
        else:
            i += 1
    return nodes


def outgoing_native_rows(source, address, after_block, histories, skip_hashes=frozenset()):
    if hasattr(source, "outgoing_native"):
        rows = source.outgoing_native(address, after_block)
        if rows is None:
            return None
    elif address in histories:
        rows = list(histories[address].get("txlist") or [])
    else:
        rows, _, _ = source.get_history(address, "txlist")
    out = []
    for row in rows:
        if normalize_address(row.get("from", "")) != address or row.get("via"):
            continue
        if parse_int(row.get("blockNumber")) <= after_block or parse_int(row.get("value")) < FOLLOW_MIN_WEI:
            continue
        if norm_hash(row.get("hash")) in skip_hashes:
            continue
        out.append(row)
    out.sort(key=lambda r: (parse_int(r.get("blockNumber")), str(r.get("hash", ""))))
    return out


def sale_proceeds_rows(source, histories, token, address, after_block):
    """ETH a sell of this coin paid out to someone other than the seller (routers can pay any recipient)."""
    if not hasattr(source, "tx_internal_native"):
        return []
    if hasattr(source, "coin_rows_from"):
        coin_rows = source.coin_rows_from(address)
    else:
        coin_rows = [
            r for r in (histories.get(address, {}).get("tokentx") or [])
            if normalize_address(r.get("contractAddress", "")) == token and normalize_address(r.get("from", "")) == address
        ]
    hashes = []
    for row in sorted(coin_rows, key=lambda r: parse_int(r.get("blockNumber"))):
        tx_hash = str(row.get("hash") or row.get("transactionHash") or "").lower()
        if parse_int(row.get("blockNumber")) > after_block and HEX_64_RE.fullmatch(tx_hash) and tx_hash not in hashes:
            hashes.append(tx_hash)
    out = []
    for tx_hash in hashes[:FOLLOW_MAX_SELLS]:
        for row in source.tx_internal_native(tx_hash):
            to = normalize_address(row.get("to", ""))
            if to and to != address and parse_int(row.get("value")) >= FOLLOW_MIN_WEI:
                out.append(dict(row, sale_from=normalize_address(row.get("from", "")), seller=address))
    return out


def follow_forward(starts, histories, first_native, magic_nodes, source, rpc_client, after_block, budget_s=None, skip_hashes=frozenset(), token=""):
    """Follow ETH the starting wallets sent after launch, hop by hop, until it reaches a stop.

    Stops: the start's first funder or a magic node (money went home), a contract, a busy wallet (exchange-like),
    a wallet already on a path, the hop limit or the time budget. ETH kept as WETH or tokens is not followed, and
    ETH spent buying this coin (a tx that brought the wallet the coin from a contract) is not proceeds.
    """
    budget = FOLLOW_BUDGET_S if budget_s is None else budget_s
    deadline = time.monotonic() + budget
    magic_addrs = {m["address"]: m for m in magic_nodes}
    flows = []
    seen = set(starts)
    frontier = [(start, start, [], after_block) for start in starts]
    labels = {}
    reached_by = collections.defaultdict(set)
    cut = False
    for hop_no in range(1, FOLLOW_HOPS + 1):
        expansions = []
        for start, addr, path, since in frontier:
            if time.monotonic() > deadline:
                cut = True
                flows.append({"start": start, "hops": path, "stop": "time budget", "ends_at": addr})
                continue
            rows = outgoing_native_rows(source, addr, since, histories, skip_hashes)
            proceeds = sale_proceeds_rows(source, histories, token, addr, since) if hop_no == 1 and token else []
            if rows is None and not proceeds:
                flows.append({"start": start, "hops": path, "stop": "too many txs to walk", "ends_at": addr})
                continue
            rows = (rows or []) + proceeds
            if not rows and path:
                flows.append({"start": start, "hops": path, "stop": "sent no ETH on", "ends_at": addr})
            for row in rows:
                expansions.append((start, addr, path, row))
        recipients = sorted({normalize_address(r["to"]) for _, _, _, r in expansions} - set(labels))
        if recipients:
            codes = rpc_client.get_code_many(recipients)
            nonces = rpc_client.get_nonce_many(recipients)
            for a, c, n in zip(recipients, codes, nonces):
                labels[a] = {"is_contract": is_contract_code(c), "nonce": n}
        frontier = []
        for start, addr, path, row in expansions:
            to = normalize_address(row["to"])
            hop = {
                "from": addr,
                "to": to,
                "value_wei": str(parse_int(row.get("value"))),
                "value_eth": parse_int(row.get("value")) / 1e18,
                "hash": norm_hash(row.get("hash")),
                "block": parse_int(row.get("blockNumber")),
                "timestamp": parse_int(row.get("timeStamp")),
                "kind": "sale proceeds" if row.get("seller") else "send",
            }
            if row.get("seller"):
                hop["paid_by"] = row.get("sale_from", "")
            reached_by[to].add(start)
            new_path = path + [hop]
            label = labels.get(to, {"is_contract": False, "nonce": 0})
            funder = (first_native.get(start) or {}).get("from", "")
            stop = ""
            if to == funder:
                stop = "back to the start's first funder"
            elif to in magic_addrs:
                stop = "reached magic node"
            elif to in seen:
                stop = "joins a path already followed"
            elif label["is_contract"]:
                stop = "contract"
            elif label["nonce"] >= BUSY_NONCE:
                stop = f"busy wallet ({label['nonce']:,} txs)"
            elif hop_no == FOLLOW_HOPS:
                stop = "hop limit"
            if stop:
                flows.append({"start": start, "hops": new_path, "stop": stop, "ends_at": to})
            else:
                seen.add(to)
                frontier.append((start, to, new_path, hop["block"]))
        if not frontier:
            break
    for flow in flows:
        flow["returns_home"] = flow["stop"] in ("back to the start's first funder", "reached magic node")
        flow["total_eth"] = flow["hops"][0]["value_eth"] if flow["hops"] else 0.0
    flows.sort(key=lambda f: (not f["returns_home"], -f["total_eth"], f["start"]))
    converge = [
        {"address": a, "starts": sorted(s), "label": labels.get(a, {})}
        for a, s in reached_by.items()
        if len(s) >= 2
    ]
    converge.sort(key=lambda x: (-len(x["starts"]), x["address"]))
    return {
        "flows": flows,
        "starts": list(starts),
        "converge": converge,
        "budget_s": budget,
        "cut_by_budget": cut,
        "hops_max": FOLLOW_HOPS,
    }


def follow_starts(creator, clusters, rank_map):
    """The creator, its cluster and the top cluster (largest share held or bought at launch)."""
    starts = [creator] if creator else []
    rank = rank_map.get(creator, 0) if creator else 0
    if rank:
        starts += clusters[rank - 1]["wallets"]
    if clusters:
        starts += clusters[0]["wallets"]
    out = []
    for addr in starts:
        if addr not in out:
            out.append(addr)
    return out[:FOLLOW_MAX_STARTS]


def tiny_addr(addr):
    return addr[:6] + "…" + addr[-4:]


def fmt_span(seconds):
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{math.ceil(seconds / 60)} min"
    return f"{math.ceil(seconds / 3600)} h"


def fmt_when(ts):
    dt = datetime.datetime.fromtimestamp(max(0, ts), tz=datetime.timezone.utc)
    return f"{dt:%b} {dt.day} {dt:%H:%M} UTC"


def fmt_share(value):
    pct = value * 100
    return f"{pct:.1f}%" if pct >= 1 or pct == 0 else f"{pct:.2f}%"


def link_phrase(kind, node):
    """(wallets covered, tie-break priority, phrase) for one reason a cluster's wallets are linked."""
    n = len(node["wallets"])
    if kind == "time":
        span = fmt_span(node.get("window_end", 0) - node.get("window_start", 0))
        if node["subtype"] == "launch-burst":
            return n, 0, f"bought {fmt_share(node.get('share_total', 0.0))} at launch within {span} of each other in near-equal amounts"
        if node["subtype"] == "busy-funder-window":
            funders = node.get("funder_addresses") or [""]
            return n, 3, f"were funded by the same busy wallet {funders[0]} within {span}"
        return n, 2, f"got their first ETH within {span} of each other"
    who = node["address"]
    if node["role"] == "both" and node.get("loop_wallets"):
        return n, 1, f"got ETH from the same wallet {who}, which got ETH back from them"
    if node["role"] == "both":
        return n, 4, f"sent to and received from the same wallet {who}"
    if node["role"] == "funder":
        return n, 1, f"got ETH from the same wallet {who}"
    return n, 5, f"sent to the same wallet {who}"


def counted(n, wallet_count, first):
    if n == wallet_count:
        return f"All {n} wallets" if first else f"all {n}"
    return f"{n} of these wallets" if first else f"{n}"


def cluster_why(cluster):
    """One plain sentence on why the cluster's wallets are linked (the two reasons covering most wallets)."""
    links = cluster["links"]
    total = cluster["wallet_count"]
    reasons = [link_phrase("time", t) for t in links.get("time", [])]
    reasons += [link_phrase("magic", m) for m in links.get("magic", [])]
    direct = links.get("direct", [])
    if direct:
        wallets = {d["a"] for d in direct} | {d["b"] for d in direct}
        pairs = len(direct)
        reasons.append((len(wallets), 1, f"sent coins or ETH straight to each other ({pairs} pair{'s' if pairs != 1 else ''})"))
    reasons.sort(key=lambda r: (-r[0], r[1]))
    picked, seen = [], set()
    for n, _, phrase in reasons:
        if phrase not in seen:
            seen.add(phrase)
            picked.append((n, phrase))
        if len(picked) == 2:
            break
    if not picked:
        return ""
    parts = [f"{counted(picked[0][0], total, True)} {picked[0][1]}"]
    if len(picked) > 1:
        parts.append(f"{counted(picked[1][0], total, False)} {picked[1][1]}")
    sentence = ", and ".join(parts) if len(parts) == 2 and ", which" not in parts[0] else "; ".join(parts)
    more = len(reasons) - len(picked)
    if more > 0:
        sentence += f" (+{more} more link{'s' if more != 1 else ''})"
    return sentence + "."


def follow_view(clusters, follow, magic_nodes):
    """Per cluster rank: arrows home to magic nodes on the map, and up to FOLLOW_CHIPS_MAX 'ETH out' chips."""
    view = {}
    if not follow:
        return view
    magic_id = {m["address"]: m["id"] for m in magic_nodes}
    rank_of = {w: c["rank"] for c in clusters for w in c["wallets"]}
    flows_by_rank = collections.defaultdict(list)
    for flow in follow.get("flows", []):
        if flow["hops"] and rank_of.get(flow["start"]):
            flows_by_rank[rank_of[flow["start"]]].append(flow)
    for rank, flows in flows_by_rank.items():
        home = collections.defaultdict(float)
        groups = collections.defaultdict(list)
        for flow in flows:
            if flow["returns_home"] and flow["ends_at"] in magic_id and rank_of.get(flow["hops"][0]["from"]) == rank:
                home[(flow["start"], magic_id[flow["ends_at"]], flow["ends_at"], flow["stop"])] += flow["total_eth"]
            else:
                groups[flow["ends_at"]].append(flow)
        ordered = sorted(groups.items(), key=lambda kv: (-len({f["start"] for f in kv[1]}), -sum(f["total_eth"] for f in kv[1]), kv[0]))
        singles = [kv for kv in ordered if len({f["start"] for f in kv[1]}) >= 2][: FOLLOW_CHIPS_MAX - 1]
        rest = [kv for kv in ordered if kv not in singles]
        if len(rest) == 1:
            singles, rest = singles + rest, []
        chips = []
        for addr, group in singles:
            stop = group[0]["stop"]
            desc = "contract" if stop == "contract" else "exchange-like" if stop.startswith("busy wallet") else "their funder" if stop.startswith("back to") else "wallet"
            starts = sorted({f["start"] for f in group})
            eth = sum(f["total_eth"] for f in group)
            chips.append({
                "text": f"ETH out → {tiny_addr(addr)} ({desc}) · {len(starts)} wallet{'s' if len(starts) != 1 else ''} · {eth:.2f} ETH",
                "starts": starts, "eth": eth, "address": addr, "stop": stop, "desc": desc,
            })
        if rest:
            flows_rest = [f for _, g in rest for f in g]
            plain = all(not (f["stop"] == "contract" or f["stop"].startswith("busy wallet")) for f in flows_rest)
            sells = all(f["hops"][0]["kind"] == "sale proceeds" for f in flows_rest)
            eth = sum(f["total_eth"] for f in flows_rest)
            chips.append({
                "text": f"ETH out → {len(rest)} {'new wallets' if plain else 'other addresses'} · {eth:.2f} ETH"
                + (" (paid from their sells)" if sells else ""),
                "starts": sorted({f["start"] for f in flows_rest}), "eth": eth, "address": "", "stop": "many",
            })
        view[rank] = {
            "home": [{"from": s, "to": mid, "address": a, "eth": eth, "stop": stop} for (s, mid, a, stop), eth in sorted(home.items())],
            "chips": chips,
        }
    return view


def cluster_after(view_entry, creator=""):
    """One plain sentence on where the cluster's ETH went after launch, or '' when nothing was followed."""
    if not view_entry:
        return ""
    parts = []
    home = collections.defaultdict(float)
    stops = {}
    for edge in view_entry["home"]:
        home[edge["address"]] += edge["eth"]
        stops[edge["address"]] = edge["stop"]
    for addr, eth in sorted(home.items(), key=lambda kv: -kv[1]):
        whose = "the wallet that funded them" if stops[addr].startswith("back to") else "a wallet linked to them"
        parts.append(f"{eth:.2f} ETH went back to {addr}, {whose}")
    for chip in view_entry["chips"]:
        if chip["address"]:
            n = len(chip["starts"])
            desc = "the coin's creator" if chip["address"] == creator else chip["desc"]
            parts.append(f"ETH went to {chip['address']} ({desc}) · {n} wallet{'s' if n != 1 else ''} · {chip['eth']:.2f} ETH")
        else:
            parts.append(chip["text"].replace("ETH out → ", "ETH went to ", 1))
    return ("After launch: " + "; ".join(parts) + ".") if parts else ""


def union_persons(persons, direct_pairs, magic_nodes, time_nodes):
    uf = UnionFind(persons)
    for row in direct_pairs:
        uf.union(row["a"], row["b"])
    for node in magic_nodes + time_nodes:
        wallets = node["wallets"]
        if len(wallets) < 2:
            continue
        anchor = wallets[0]
        for addr in wallets[1:]:
            uf.union(anchor, addr)
    return uf.groups()


def cluster_link_summaries(members_set, direct_pairs, magic_nodes, time_nodes):
    direct = []
    for row in direct_pairs:
        if row["a"] in members_set and row["b"] in members_set:
            direct.append(row)
    magic = []
    for node in magic_nodes:
        links = sorted([w for w in node["wallets"] if w in members_set])
        if len(links) >= 2:
            copy_node = dict(node)
            copy_node["wallets"] = links
            magic.append(copy_node)
    time_links = []
    for node in time_nodes:
        links = sorted([w for w in node["wallets"] if w in members_set])
        if len(links) >= 2:
            copy_node = dict(node)
            copy_node["wallets"] = links
            time_links.append(copy_node)
    scores = {
        "direct": sum(x["total_count"] for x in direct),
        "magic": sum(len(x["wallets"]) for x in magic),
        "time": sum(len(x["wallets"]) for x in time_links),
    }
    strongest = "direct"
    for key in ("direct", "time", "magic"):
        if scores[key] > scores[strongest]:
            strongest = key
    return {"direct": direct, "magic": magic, "time": time_links, "strongest": strongest}


def cluster_sort_key(cluster):
    return (-max(cluster["share_now"], cluster["bought_at_launch"]), cluster["wallets"][0])


def build_clusters(persons, share_now_by_person, launch_share_by_person, direct_pairs, magic_nodes, time_nodes):
    groups = union_persons(persons, direct_pairs, magic_nodes, time_nodes)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        wallets = sorted(members)
        members_set = set(wallets)
        links = cluster_link_summaries(members_set, direct_pairs, magic_nodes, time_nodes)
        share_now = sum(share_now_by_person.get(addr, 0.0) for addr in wallets)
        launch_share = sum(launch_share_by_person.get(addr, 0.0) for addr in wallets)
        strongest = links["strongest"]
        strongest_hash = ""
        source_rows = links.get(strongest, [])
        if source_rows:
            strongest_hash = (source_rows[0].get("hashes") or [""])[0]
        clusters.append(
            {
                "wallets": wallets,
                "wallet_count": len(wallets),
                "share_now": share_now,
                "share_now_pct": share_now * 100.0,
                "share": share_now,
                "bought_at_launch": launch_share,
                "bought_at_launch_pct": launch_share * 100.0,
                "links": links,
                "strongest_example_hash": strongest_hash,
            }
        )
    clusters.sort(key=cluster_sort_key)
    rank_map = {}
    for idx, cluster in enumerate(clusters, start=1):
        cluster["rank"] = idx
        cluster["name"] = f"Cluster {idx}"
        cluster["wallet_rows"] = [
            {
                "address": a,
                "share_now": share_now_by_person.get(a, 0.0),
                "share_now_pct": share_now_by_person.get(a, 0.0) * 100.0,
                "bought_at_launch": launch_share_by_person.get(a, 0.0),
                "bought_at_launch_pct": launch_share_by_person.get(a, 0.0) * 100.0,
            }
            for a in cluster["wallets"]
        ]
        cluster["strongest_link_type"] = cluster["links"]["strongest"]
        for addr in cluster["wallets"]:
            rank_map[addr] = idx
    for addr in persons:
        if addr not in rank_map:
            rank_map[addr] = 0
    return clusters, rank_map


def collect_person_link_text(persons, direct_pairs, magic_nodes, time_nodes):
    links = {addr: [] for addr in persons}
    for row in direct_pairs:
        label = f"direct {short_addr(row['a'])}<->{short_addr(row['b'])} ({row['total_count']})"
        if row["hashes"]:
            label += " " + ",".join(row["hashes"])
        links[row["a"]].append(label)
        links[row["b"]].append(label)
    for node in magic_nodes:
        label = f"magic {node['role']} {node['address']}"
        if node["hashes"]:
            label += " " + ",".join(node["hashes"])
        for addr in node["wallets"]:
            links.setdefault(addr, []).append(label)
    for node in time_nodes:
        label = f"time {node['window_label']} funders:{','.join(node['funder_addresses'])}"
        if node["hashes"]:
            label += " " + ",".join(node["hashes"])
        for addr in node["wallets"]:
            links.setdefault(addr, []).append(label)
    for addr in links:
        links[addr] = links[addr][:8]
    return links


def build_graph(person_rows, rank_by_person, direct_pairs, magic_nodes, time_nodes):
    nodes = []
    edges = []
    for row in person_rows:
        addr = row["address"]
        nodes.append(
            {
                "id": addr,
                "type": "person",
                "address": addr,
                "share_now": row.get("share_now", 0.0),
                "bought_at_launch": row.get("bought_at_launch", 0.0),
                "is_creator": bool(row.get("is_creator")),
                "is_launch_buyer": bool(row.get("is_launch_buyer")),
                "sold_out": bool(row.get("sold_out")),
                "cluster_rank": rank_by_person.get(addr, 0),
            }
        )
    for node in magic_nodes:
        nodes.append(
            {
                "id": node["id"],
                "type": "magic",
                "address": node["address"],
                "cluster_rank": 0,
                "role": node["role"],
                "wallets": list(node["wallets"]),
                "funded": list(node.get("funded", [])),
                "paid_back_by": list(node.get("paid_back_by", [])),
                "loop_wallets": list(node.get("loop_wallets", [])),
                "hashes": list(node["hashes"]),
                "first_timestamp": node.get("first_timestamp"),
                "last_timestamp": node.get("last_timestamp"),
            }
        )
        for addr in node["wallets"]:
            if rank_by_person.get(addr, -1) >= 0:
                edges.append(
                    {
                        "id": f"edge-{node['id']}-{addr}",
                        "kind": "magic",
                        "from": addr,
                        "to": node["id"],
                        "both_ways": False,
                        "hashes": list(node["hashes"]),
                        "cluster_ranks": sorted({rank_by_person.get(addr, 0)}),
                        "first_timestamp": node.get("first_timestamp"),
                        "last_timestamp": node.get("last_timestamp"),
                    }
                )
    for node in time_nodes:
        nodes.append(
            {
                "id": node["id"],
                "type": "time",
                "window_label": node["window_label"],
                "window_start": node["window_start"],
                "window_end": node["window_end"],
                "cluster_rank": 0,
                "wallets": list(node["wallets"]),
                "funder_addresses": list(node["funder_addresses"]),
                "subtype": node["subtype"],
                "hashes": list(node["hashes"]),
                "first_timestamp": node.get("first_timestamp"),
                "last_timestamp": node.get("last_timestamp"),
            }
        )
        for addr in node["wallets"]:
            if rank_by_person.get(addr, -1) >= 0:
                edges.append(
                    {
                        "id": f"edge-{node['id']}-{addr}",
                        "kind": "time",
                        "from": addr,
                        "to": node["id"],
                        "both_ways": False,
                        "hashes": list(node["hashes"]),
                        "cluster_ranks": sorted({rank_by_person.get(addr, 0)}),
                        "first_timestamp": node.get("first_timestamp"),
                        "last_timestamp": node.get("last_timestamp"),
                    }
                )
    for row in direct_pairs:
        edge_ranks = sorted({rank_by_person.get(row["a"], 0), rank_by_person.get(row["b"], 0)})
        edges.append(
            {
                "id": f"edge-direct-{row['a']}-{row['b']}",
                "kind": "direct",
                "from": row["a"],
                "to": row["b"],
                "both_ways": row["both_ways"],
                "count": row["total_count"],
                "hashes": list(row["hashes"]),
                "cluster_ranks": edge_ranks,
                "ab_count": row["ab_count"],
                "ba_count": row["ba_count"],
                "kinds": list(row["kinds"]),
                "first_timestamp": row.get("first_timestamp"),
                "last_timestamp": row.get("last_timestamp"),
            }
        )
    return {"nodes": nodes, "edges": edges}


def display_share(node):
    return max(node.get("share_now", 0.0), node.get("bought_at_launch", 0.0))


def base_member_radius(share_value):
    return 5.0 + math.sqrt(max(share_value, 0.0)) * 130.0


def pack_circles_in_rect(items, rect, min_radius, max_radius, gap, obstacles):
    x0, y0, x1, y1 = rect
    cx = (x0 + x1) * 0.5
    cy = (y0 + y1) * 0.5
    base = {}
    for item in items:
        base[item["id"]] = min(max_radius, max(min_radius, base_member_radius(item["share"])))
    scale = 1.0
    for _ in range(36):
        placed = {}
        radii = {}
        failed = False
        for item in items:
            r = min(max_radius, max(min_radius, base[item["id"]] * scale))
            found = False
            for step in range(1800):
                if step == 0:
                    px, py = cx, cy
                else:
                    angle = step * 0.49
                    distance = math.sqrt(step) * (0.62 + r * 0.44)
                    px = cx + math.cos(angle) * distance
                    py = cy + math.sin(angle) * distance
                if px - r < x0 or px + r > x1 or py - r < y0 or py + r > y1:
                    continue
                overlap = False
                for ox, oy, oradius in obstacles:
                    if math.hypot(px - ox, py - oy) < (r + oradius + gap):
                        overlap = True
                        break
                if overlap:
                    continue
                for pid, ppos in placed.items():
                    if math.hypot(px - ppos[0], py - ppos[1]) < (r + radii[pid] + gap):
                        overlap = True
                        break
                if overlap:
                    continue
                placed[item["id"]] = (px, py)
                radii[item["id"]] = r
                found = True
                break
            if not found:
                failed = True
                break
        if not failed:
            return placed, radii
        scale *= 0.92
    placed = {}
    radii = {}
    x = x0 + min_radius
    y = y0 + min_radius
    row_h = 0.0
    for item in items:
        r = min_radius
        if x + r > x1:
            x = x0 + r
            y += row_h + gap
            row_h = 0.0
        if y + r > y1:
            y = y1 - r
        placed[item["id"]] = (x, y)
        radii[item["id"]] = r
        row_h = max(row_h, 2 * r)
        x += 2 * r + gap
    return placed, radii


def tile_title(cluster):
    n = cluster.get("wallet_count", 0)
    return (
        f"{cluster.get('name', '')} · {n} wallet{'s' if n != 1 else ''} · holds {fmt_share(cluster.get('share_now', 0.0))} now · "
        f"bought {fmt_share(cluster.get('bought_at_launch', 0.0))} at launch"
    )


def plain_answer(clusters):
    if not clusters:
        return "No group of top holders looks linked in the data read."
    top, rest = clusters[0], clusters[1:]
    now, bought = top.get("share_now", 0.0), top.get("bought_at_launch", 0.0)
    if bought > now:
        text = (f"{top.get('wallet_count', 0)} wallets that are probably one person or team bought {fmt_share(bought)} "
                f"of the supply at launch and hold {fmt_share(now)} now.")
    else:
        text = f"{top.get('wallet_count', 0)} wallets that are probably one person or team hold {fmt_share(now)} of the supply."
    if rest:
        now = sum(c.get("share_now", 0.0) for c in rest)
        bought = sum(c.get("bought_at_launch", 0.0) for c in rest)
        groups = f"{len(rest)} more group{'s' if len(rest) != 1 else ''}"
        if bought > now:
            text += f" {groups} bought {fmt_share(bought)} at launch and hold {fmt_share(now)} now."
        else:
            text += f" {groups} hold {fmt_share(now)}."
    return text


def make_header_lines(report):
    hdr = report["header"]
    chain = f"{hdr['chain_name']} ({hdr['chain_id']})" if isinstance(hdr["chain_id"], int) else hdr["chain_name"]
    line1 = f"{hdr['symbol']} · {chain} · {hdr['token']}"
    line2 = (
        f"generated {hdr['generated_at_utc']} · holders {hdr['holders_read']} · people {hdr['persons']} · "
        f"launch buyers {hdr['launch_buyer_count']} · {hdr['calls_made']} requests, {hdr['cache_hits']} from cache · "
        f"{len(hdr['truncated_histories'])} histories cut short"
    )
    return line1, line2


def build_summary(report):
    """Everything the user needs, as plain text for the chat: answer, clusters, money after launch, what was read."""
    clusters = report["clusters"]
    line1, line2 = make_header_lines(report)
    lines = [line1, "", plain_answer(clusters)]
    for cluster in clusters:
        lines += ["", tile_title(cluster), f"Why: {cluster.get('why', '')}"]
        if cluster.get("after"):
            lines.append(cluster["after"])
        if cluster.get("strongest_example_hash"):
            lines.append(f"Example transaction: {cluster['strongest_example_hash']}")
        lines.append("Wallets:")
        rows = cluster["wallet_rows"]
        if len(rows) > SUMMARY_MAX_WALLETS:
            rows = sorted(rows, key=lambda r: (-r["share_now"], -r["bought_at_launch"]))
        lines += [f"  {row['address']} · holds {as_pct(row['share_now'])} · bought {as_pct(row['bought_at_launch'])} at launch"
                  for row in rows[:SUMMARY_MAX_WALLETS]]
        rest = rows[SUMMARY_MAX_WALLETS:]
        if rest:
            lines.append(f"  +{len(rest)} more wallets holding {fmt_share(sum(r['share_now'] for r in rest))} now, "
                         f"bought {fmt_share(sum(r['bought_at_launch'] for r in rest))} at launch (full list: run with --out <folder>)")
    unlinked = report.get("unclustered_wallets", [])
    if unlinked:
        lines += ["", f"Not linked to anyone: {len(unlinked)} wallet{'s' if len(unlinked) != 1 else ''} holding {as_pct(sum(r['share_now'] for r in unlinked))}."]
    creator = report["header"].get("creator")
    if creator and not any(creator in c["wallets"] for c in clusters):
        follow_data = report.get("follow_forward") or {}
        senders = {f["start"] for f in follow_data.get("flows", []) if any(h["to"] == creator for h in f["hops"])}
        senders |= {s for item in follow_data.get("converge", []) if item["address"] == creator for s in item["starts"]}
        if senders:
            lines.append(f"Creator {creator} is not in a cluster, but {len(senders)} followed wallets sent it ETH after launch.")
        else:
            lines.append(f"Creator {creator} is not linked to any cluster.")
    follow = follow_summary_lines(report.get("follow_forward"))
    if follow:
        lines += ["", "Where the money went after launch:"] + follow
    lines += ["", "How this was read:", line2]
    lines += ["read: " + text for text in report["header"].get("coverage", [])]
    cache_age = report["header"].get("oldest_cache_age_s")
    if cache_age is not None:
        lines.append(f"cache: oldest cached answer used was {cache_age // 60} min old (holders, balances and histories "
                     f"are re-read after {CACHE_MAX_AGE_S // 60} min)")
    notes = report.get("notes", [])
    if notes:
        lines += ["", "Notes:"] + summary_note_lines(notes)
    return "\n".join(lines)


def summary_note_lines(notes):
    texts = [m.group(1) if m else None for m in (re.match(r"\S+: (.+)$", note) for note in notes)]
    counts = collections.Counter(t for t in texts if t)
    out, done = [], set()
    for note, text in zip(notes, texts):
        if counts[text] < SUMMARY_NOTE_FOLD:
            out.append(f"- {note}")
        elif text not in done:
            done.add(text)
            out.append(f"- {counts[text]} wallets: {text} (list: --out <folder>)")
    return out


def follow_summary_lines(follow, limit=5):
    if not follow:
        return []
    flows = [f for f in follow["flows"] if f["hops"]]
    home = [f for f in flows if f["returns_home"]]
    out = [
        f"Followed {len(flows)} ETH payment{'s' if len(flows) != 1 else ''} out of {len(follow['starts'])} wallets; "
        f"{len(home)} went back to a wallet that funded them or is linked to them."
        + (f" Stopped early at the {FOLLOW_BUDGET_S} s limit." if follow.get("cut_by_budget") else "")
    ]
    for flow in flows[:limit]:
        route = " -> ".join([flow["start"]] + [h["to"] for h in flow["hops"]])
        stop = "reached a wallet linked to them" if flow["stop"] == "reached magic node" else flow["stop"]
        out.append(f"  {flow['total_eth']:.4f} ETH {route} [{stop}] tx {flow['hops'][-1]['hash']}")
    for item in follow.get("converge", [])[:limit]:
        out.append(f"  {item['address']} received ETH from {len(item['starts'])} of those wallets")
    return out


def build_report(token, top, blockscout_client, rpc_client, stats, generated_at, chain_key="robinhood"):
    chain = CHAINS[chain_key]
    token_meta = blockscout_client.get_token(token)
    decimals = parse_int(token_meta.get("decimals"))
    total_supply_raw = parse_int(token_meta.get("totalSupply"))
    if total_supply_raw <= 0:
        raise RuntimeError("Token totalSupply is zero.")
    symbol = str(token_meta.get("symbol", "") or "UNKNOWN")
    raw_holders = blockscout_client.get_token_holders(token, top)
    holders = classify_holders(raw_holders, rpc_client)
    if chain["source"] != "solana":
        mark_exchange_holders(holders, rpc_client)
    holders_set = {row["address"] for row in holders}

    for row in holders:
        row["share"] = float(row["value_raw"]) / float(total_supply_raw)
        row["share_pct"] = row["share"] * 100.0
        scale = 10 ** decimals if decimals >= 0 else 1
        row["balance"] = float(row["value_raw"]) / float(scale)

    launch = discover_launch_window(token, total_supply_raw, blockscout_client, rpc_client)
    holder_persons = [row["address"] for row in holders if row["kind"] == "person"]
    holder_now_share = {row["address"]: row["share"] for row in holders if row["kind"] == "person"}
    holder_now_balance = {row["address"]: row["balance"] for row in holders if row["kind"] == "person"}
    persons = set(holder_persons)
    if launch["creator"]:
        persons.add(launch["creator"])
    for addr in launch["launch_buyers"]:
        persons.add(addr)
    persons = sorted(persons)

    launch_share_by_person = {addr: 0.0 for addr in persons}
    for addr, share in launch["launch_buyers"].items():
        launch_share_by_person[addr] = share
    share_now_by_person = {addr: holder_now_share.get(addr, 0.0) for addr in persons}
    balance_now_by_person = {addr: holder_now_balance.get(addr, 0.0) for addr in persons}
    person_rows = []
    for addr in persons:
        is_creator = addr == launch["creator"] and bool(launch["creator"])
        is_launch_buyer = addr in launch["launch_buyers"]
        sold_out = share_now_by_person.get(addr, 0.0) <= 0.0 and (is_creator or is_launch_buyer)
        person_rows.append(
            {
                "address": addr,
                "share_now": share_now_by_person.get(addr, 0.0),
                "share_now_pct": share_now_by_person.get(addr, 0.0) * 100.0,
                "balance_now": balance_now_by_person.get(addr, 0.0),
                "bought_at_launch": launch_share_by_person.get(addr, 0.0),
                "bought_at_launch_pct": launch_share_by_person.get(addr, 0.0) * 100.0,
                "is_creator": is_creator,
                "is_launch_buyer": is_launch_buyer,
                "sold_out": sold_out,
            }
        )

    t_hist = time.monotonic()
    if hasattr(blockscout_client, "fetch_histories"):
        histories, truncated_histories, notes = blockscout_client.fetch_histories(persons)
        coverage = list(getattr(blockscout_client, "coverage", []))
    else:
        histories, truncated_histories, notes = fetch_histories(persons, blockscout_client)
        coverage = [
            "Blockscout: native, internal and token history of every person, all tokens, "
            f"up to {MAX_HISTORY_PAGES}×{HISTORY_OFFSET} rows per list"
        ]
    unread = {n.split(":", 1)[0] for n in notes if ": history not read (" in n}
    for addr in truncated_histories:
        if addr in unread:
            continue
        notes.append(f"{addr}: history truncated at {MAX_HISTORY_PAGES} pages of {HISTORY_OFFSET} rows.")
    resolved = resolve_internal_origins(histories, persons, rpc_client)
    if resolved:
        coverage.append(f"{resolved} contract-forwarded ETH transfers credited to the tx sender (multisend/disperse)")
    history_seconds = time.monotonic() - t_hist
    directed, counterparties, first_native = build_interactions(persons, holders_set, histories)
    direct_pairs = merge_directed_links(directed)
    cp_labels = label_counterparties(counterparties, first_native, rpc_client)
    magic_nodes = classify_magic_nodes(counterparties, cp_labels)
    time_nodes = classify_time_nodes(
        first_native, cp_labels, {n["address"] for n in magic_nodes}, launch_ts=launch["first_trade_timestamp"]
    )
    time_nodes += classify_launch_bursts(launch.get("launch_buys", {}))

    clusters, rank_map = build_clusters(persons, share_now_by_person, launch_share_by_person, direct_pairs, magic_nodes, time_nodes)
    unclustered_wallets = [
        {
            "address": addr,
            "share_now": share_now_by_person.get(addr, 0.0),
            "share_now_pct": share_now_by_person.get(addr, 0.0) * 100.0,
            "bought_at_launch": launch_share_by_person.get(addr, 0.0),
            "bought_at_launch_pct": launch_share_by_person.get(addr, 0.0) * 100.0,
        }
        for addr in sorted(persons)
        if rank_map.get(addr, 0) == 0
    ]
    t_follow = time.monotonic()
    skip_hashes = frozenset(launch.pop("buy_hashes", []))
    if chain.get("follow", True):
        follow = follow_forward(
            follow_starts(launch["creator"], clusters, rank_map), histories, first_native, magic_nodes,
            blockscout_client, rpc_client, launch.get("first_trade_block") or 0,
            skip_hashes=skip_hashes, token=token,
        )
        follow["elapsed_s"] = round(time.monotonic() - t_follow, 1)
        if isinstance(blockscout_client, SolanaSource):
            coverage.append(
                f"money after launch: SOL sent in each wallet's first {SOL_FOLLOW_PAGES * SOL_PAGE} transactions after launch; "
                "SOL wrapped for trades not counted"
            )
    else:
        follow = None
        coverage.append(f"money after launch: not followed on {chain['name']} yet")
    person_links = collect_person_link_text(persons, direct_pairs, magic_nodes, time_nodes)
    graph = build_graph(person_rows, rank_map, direct_pairs, magic_nodes, time_nodes)
    follow_by_rank = follow_view(clusters, follow, magic_nodes)
    for cluster in clusters:
        cluster["why"] = cluster_why(cluster)
        cluster["after"] = cluster_after(follow_by_rank.get(cluster["rank"]), launch["creator"])

    rules = {
        "BUSY_NONCE": BUSY_NONCE,
        "SUPERNODE_NONCE": SUPERNODE_NONCE,
        "TIME_WINDOW_S": TIME_WINDOW_S,
        "LAUNCH_WINDOW_S": LAUNCH_WINDOW_S,
        "MAX_WORKERS": MAX_WORKERS,
        "MAX_REQUESTS_PER_SECOND": MAX_REQUESTS_PER_SECOND,
        "HISTORY_OFFSET": HISTORY_OFFSET,
        "MAX_HISTORY_PAGES": MAX_HISTORY_PAGES,
    }

    report = {
        "header": {
            "token": token,
            "symbol": symbol,
            "chain_name": chain["name"],
            "chain_id": chain["chain_id"],
            "chain": chain_key,
            "coverage": coverage,
            "history_seconds": round(history_seconds, 1),
            "generated_at_utc": generated_at,
            "holders_read": len(holders),
            "persons": len(persons),
            "creator": launch["creator"],
            "launch_buyer_count": len(launch["launch_buyers"]),
            "launch_bought_total_share": launch["launch_bought_total_share"],
            "launch_bought_total_share_pct": launch["launch_bought_total_share"] * 100.0,
            "first_trade_timestamp": launch["first_trade_timestamp"],
            "calls_made": stats.calls_made,
            "cache_hits": stats.cache_hits,
            "truncated_histories": truncated_histories,
            "rules": rules,
        },
        "token": {
            "address": token,
            "symbol": symbol,
            "decimals": decimals,
            "total_supply_raw": total_supply_raw,
            "total_supply": float(total_supply_raw) / float(10 ** decimals if decimals >= 0 else 1),
        },
        "notes": sorted(set(notes)),
        "holders": holders,
        "persons": person_rows,
        "launch": launch,
        "counterparties_labeled": {a: {k: v for k, v in lab.items() if k != "code"} for a, lab in cp_labels.items()},
        "direct_links": direct_pairs,
        "magic_nodes": magic_nodes,
        "time_nodes": time_nodes,
        "clusters": clusters,
        "unclustered_wallets": unclustered_wallets,
        "follow_forward": follow,
        "graph": graph,
        "person_links": person_links,
        "native": chain.get("native", "ETH"),
    }
    if report["native"] != "ETH":
        for cluster in clusters:
            for key in ("why", "after"):
                cluster[key] = native_word(cluster.get(key, ""), report["native"])
    return report


def native_word(text, symbol):
    return re.sub(r"\bETH\b", symbol, text) if symbol != "ETH" else text


def ensure_dirs(path):
    os.makedirs(path, exist_ok=True)


def build_clients(chain_key, cache, stats, fetch_json=None):
    """Data source and RPC client for a chain; keys come from the environment only."""
    chain = CHAINS[chain_key]
    if chain["source"] == "solana":
        key = os.environ.get(chain["rpc_key_env"], "").strip()
        if not key:
            raise SystemExit(
                f"Missing {chain['rpc_key_env']} in environment (Solana holders and histories come from Helius; "
                "a free key at helius.dev works)."
            )
        rpc = RpcClient(
            url=chain["rpc_url"].format(key=key),
            cache=cache,
            stats=stats,
            fetch_json=fetch_json,
            batch_size=1,
            max_batch=1,
            limiter=None if isinstance(fetch_json, ReplayFetch) else RateLimiter(RPC_REQUESTS_PER_SECOND),
            cache_label=chain["rpc_url"],
            secret=key,
        )
        source = SolanaSource(rpc)
        return source, SolanaLabels(source)
    if chain["source"] == "blockscout":
        api_key = os.environ.get("BLOCKSCOUT_API_KEY", "").strip()
        if not api_key:
            raise SystemExit(
                "Missing BLOCKSCOUT_API_KEY in environment (Robinhood history comes from Blockscout; "
                "a free key at dev.blockscout.com works)."
            )
        source = BlockscoutClient(api_key=api_key, cache=cache, stats=stats, fetch_json=fetch_json)
        rpc = RpcClient(url=chain["rpc_url"], cache=cache, stats=stats, fetch_json=fetch_json, batch_size=chain["rpc_batch"])
        return source, rpc
    key_env = chain.get("rpc_key_env")
    key = os.environ.get(key_env, "").strip() if key_env else ""
    if key_env and not key:
        raise SystemExit(
            f"Missing {key_env} in environment ({chain['name']} reads need an archive RPC with trace_filter; "
            "a free PublicNode token at allnodes.com/publicnode works)."
        )
    rpc = RpcClient(
        url=chain["rpc_url"].format(key=key),
        cache=cache,
        stats=stats,
        fetch_json=fetch_json,
        batch_size=chain["rpc_batch"],
        max_batch=chain["rpc_batch"],
        limiter=None if isinstance(fetch_json, ReplayFetch) else RateLimiter(RPC_REQUESTS_PER_SECOND),
        cache_label=chain["rpc_url"],
        secret=key,
    )
    return RpcChainSource(chain_key, rpc), rpc


FIXTURE_EARLY_INBOUND = 5
FIXTURE_ROW_FIELDS = ("from", "to", "value", "hash", "transactionHash", "blockNumber", "timeStamp", "contractAddress")


def fixture_key(url, data, secrets):
    text = url
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"([?&]apikey=)[^&\s]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    body = data.decode("utf-8") if isinstance(data, bytes) else (data or "")
    return text + "\n" + body


def shrink_code_results(key, resp):
    """Bytecode only matters as empty or not; keep 4 bytes so fixtures stay small."""
    try:
        body = json.loads(key.split("\n", 1)[1])
    except (ValueError, IndexError):
        return resp
    calls = body if isinstance(body, list) else [body]
    if not any(isinstance(c, dict) and c.get("method") == "eth_getCode" for c in calls):
        return resp
    rows = resp if isinstance(resp, list) else [resp]
    out = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("result"), str) and is_contract_code(row["result"]):
            row = dict(row)
            row["result"] = row["result"][:10]
        out.append(row)
    return out if isinstance(resp, list) else out[0]


class RecordingFetch:
    """Wraps fetch_json and keeps every response keyed by the request with secrets removed (test fixtures)."""

    def __init__(self, inner, secrets):
        self.inner = inner
        self.secrets = [s for s in secrets if s]
        self.responses = {}
        self._lock = threading.Lock()

    def __call__(self, url, data=None, headers=None, timeout=HTTP_TIMEOUT_S):
        k = fixture_key(url, data, self.secrets)
        try:
            resp = self.inner(url, data, headers, timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            with self._lock:
                self.responses[k] = {"__http_error__": exc.code, "body": body}
            raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(body.encode("utf-8")))
        with self._lock:
            self.responses[k] = resp
        return resp

    def dump(self):
        return {k: shrink_code_results(k, v) for k, v in self.responses.items()}


class ReplayFetch:
    """Serves recorded responses; a request that was never recorded fails loudly."""

    def __init__(self, responses, secrets=()):
        self.responses = responses
        self.secrets = [s for s in secrets if s]
        self.calls = 0

    def __call__(self, url, data=None, headers=None, timeout=HTTP_TIMEOUT_S):
        k = fixture_key(url, data, self.secrets)
        if k not in self.responses:
            raise AssertionError("request not in fixture: " + k[:300])
        self.calls += 1
        resp = self.responses[k]
        if isinstance(resp, dict) and "__http_error__" in resp:
            raise urllib.error.HTTPError(url, resp["__http_error__"], "recorded", {}, io.BytesIO(resp["body"].encode("utf-8")))
        return json.loads(json.dumps(resp))


class RecordingSource:
    """Records what a Blockscout client returned, so a fixture can keep only the rows that shape the map."""

    def __init__(self, inner):
        self.inner = inner
        self.data = {"token": None, "holders": None, "transfers": None, "history": {}}
        self._lock = threading.Lock()

    def get_token(self, token):
        self.data["token"] = self.inner.get_token(token)
        self.data["token_address"] = token
        return self.data["token"]

    def tx_internal_native(self, tx_hash):
        rows = self.inner.tx_internal_native(tx_hash)
        with self._lock:
            self.data.setdefault("tx_internal", {})[tx_hash] = rows
        return rows

    def get_token_holders(self, token, top_n):
        self.data["holders"] = self.inner.get_token_holders(token, top_n)
        return self.data["holders"]

    def get_token_transfers(self, token):
        self.data["transfers"] = self.inner.get_token_transfers(token)
        return self.data["transfers"]

    def get_history(self, address, action):
        rows, truncated, note = self.inner.get_history(address, action)
        with self._lock:
            self.data["history"].setdefault(address, {})[action] = [rows, truncated, note]
        return rows, truncated, note

    def dump(self):
        return compact_source_fixture(self.data)


def trim_row(row):
    return {k: row[k] for k in FIXTURE_ROW_FIELDS if k in row}


def compact_source_fixture(data):
    """Drop history rows that cannot change links, magic or time nodes (spam inbound tokens, one-off counterparties)."""
    persons = set(data["history"])
    holders = {normalize_address(r.get("address", "")) for r in data.get("holders") or []}
    token = normalize_address(data.get("token_address", ""))
    touch = collections.defaultdict(set)
    for person, by_action in data["history"].items():
        for action, (rows, _, _) in by_action.items():
            for row in rows:
                frm, to = normalize_address(row.get("from", "")), normalize_address(row.get("to", ""))
                if parse_int(row.get("value")) <= 0:
                    continue
                if frm in persons and to not in persons and to not in holders:
                    touch[to].add(frm)
                if to in persons and frm not in persons and frm not in holders and action != "tokentx":
                    touch[frm].add(to)
    history = {}
    early = FIXTURE_EARLY_INBOUND
    for person, by_action in data["history"].items():
        history[person] = {}
        for action, (rows, truncated, note) in by_action.items():
            # First funding and origin resolution only look at a person's earliest inbound ETH.
            inbound = []
            if action != "tokentx":
                for row in rows:
                    frm = normalize_address(row.get("from", ""))
                    if normalize_address(row.get("to", "")) == person and frm not in persons and parse_int(row.get("value")) > 0:
                        inbound.append(row)
                inbound.sort(key=lambda r: (parse_int(r.get("blockNumber")), parse_int(r.get("timeStamp"))))
            first_ids = {id(r) for r in [r for r in inbound if normalize_address(r.get("from", "")) not in holders][:early]}
            first_ids |= {id(r) for r in inbound[:early]}
            kept = []
            for row in rows:
                frm, to = normalize_address(row.get("from", "")), normalize_address(row.get("to", ""))
                if parse_int(row.get("value")) <= 0:
                    continue
                keep = (frm in persons and to in persons) or id(row) in first_ids
                keep = keep or (action == "tokentx" and frm == person and normalize_address(row.get("contractAddress", "")) == token)
                keep = keep or (action != "tokentx" and to in persons and len(touch.get(frm, ())) >= 2)
                keep = keep or (frm in persons and to not in persons and len(touch.get(to, ())) >= 2)
                if keep:
                    kept.append(trim_row(row))
            history[person][action] = [kept, truncated, note]
    for person, by_action in data["history"].items():
        if "txlist" not in by_action or "tokentx" not in by_action:
            continue
        token_hashes = {norm_hash(r.get("hash") or r.get("transactionHash")) for r in history[person]["tokentx"][0]
                        if normalize_address(r.get("from", "")) == person}
        for row in by_action["txlist"][0]:
            if (normalize_address(row.get("from", "")) == person and parse_int(row.get("value")) <= 0
                    and norm_hash(row.get("hash") or row.get("transactionHash")) in token_hashes):
                history[person]["txlist"][0].append(trim_row(row))
    return {
        "token": data["token"],
        "token_address": data.get("token_address", ""),
        "holders": data["holders"],
        "transfers": [trim_row(r) for r in data.get("transfers") or []],
        "history": history,
        "tx_internal": {h: [trim_row(r) for r in rows] for h, rows in (data.get("tx_internal") or {}).items()},
    }


class ReplaySource:
    def __init__(self, data):
        self.data = data

    def get_token(self, token):
        return dict(self.data["token"])

    def get_token_holders(self, token, top_n):
        return list(self.data["holders"][:top_n])

    def get_token_transfers(self, token):
        return list(self.data["transfers"])

    def get_history(self, address, action):
        rows, truncated, note = self.data["history"].get(address, {}).get(action, [[], False, ""])
        return list(rows), truncated, note

    def tx_internal_native(self, tx_hash):
        return [dict(r) for r in self.data.get("tx_internal", {}).get(tx_hash, [])]


def save_fixture(path, meta, rpc_responses, source_data, secrets):
    text = json.dumps({"meta": meta, "rpc": rpc_responses, "source": source_data}, sort_keys=True, separators=(",", ":"))
    for secret in secrets:
        if secret and secret in text:
            raise RuntimeError("refusing to save a fixture that contains a key")
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(text)


def run_cluster_map(token_address, out_dir, top, cache_dir, blockscout_client=None, rpc_client=None, generated_at=None, chain_key="robinhood", fetch_json=None, wrap_source=None):
    if chain_key not in CHAINS:
        raise SystemExit(f"Unknown chain {chain_key!r}; known: {', '.join(sorted(CHAINS))}.")
    token = normalize_address(token_address)
    if not is_valid_address(token, chain_key):
        if chain_key == "solana":
            raise SystemExit("Invalid mint address. Expected a base58 Solana address.")
        raise SystemExit("Invalid token address. Expected 0x followed by exactly 40 hex characters.")
    cache = DiskCache(cache_dir)
    stats = RequestStats()
    if blockscout_client is None or rpc_client is None:
        source, rpc = build_clients(chain_key, cache, stats, fetch_json=fetch_json)
        blockscout_client = blockscout_client or source
        rpc_client = rpc_client or rpc
    if wrap_source is not None:
        blockscout_client = wrap_source(blockscout_client)

    generated = generated_at or utc_now_iso()
    started = time.monotonic()
    report = build_report(token, top, blockscout_client, rpc_client, stats, generated, chain_key=chain_key)
    report["header"]["run_seconds"] = round(time.monotonic() - started, 1)
    report["header"]["oldest_cache_age_s"] = None if cache.oldest_age_s is None else round(cache.oldest_age_s)
    summary_text = native_word(build_summary(report), report.get("native", "ETH"))
    if not out_dir:
        return report, summary_text, None
    ensure_dirs(out_dir)
    json_path = os.path.join(out_dir, f"{token}-map.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=True)
    return report, summary_text, json_path


def build_cli_json_summary(report):
    return {
        "token": report["token"]["address"],
        "symbol": report["token"]["symbol"],
        "chain_id": report["header"]["chain_id"],
        "clusters": [
            {
                "rank": cluster["rank"],
                "wallet_count": cluster["wallet_count"],
                "share_now": cluster["share_now"],
                "share_now_pct": cluster["share_now_pct"],
                "bought_at_launch": cluster["bought_at_launch"],
                "bought_at_launch_pct": cluster["bought_at_launch_pct"],
                "strongest_link_type": cluster["strongest_link_type"],
            }
            for cluster in report["clusters"]
        ],
        "persons": report["header"]["persons"],
        "launch_buyer_count": report["header"]["launch_buyer_count"],
        "creator": report["header"]["creator"],
        "truncated_histories": report["header"]["truncated_histories"],
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Map which of a token's holders are linked, and where their money went after launch.")
    parser.add_argument("token", help="Token address (0x + 40 hex chars) or Solana mint (base58)")
    parser.add_argument("--out", default="", help="Also save the full report as JSON in this folder")
    parser.add_argument("--top", default=DEFAULT_TOP, type=int, help="Top holders to read")
    parser.add_argument("--cache", default=DEFAULT_CACHE_DIR, help="Cache directory")
    parser.add_argument("--json", action="store_true", help="Print short JSON summary to stdout")
    parser.add_argument("--chain", required=True, choices=sorted(CHAINS), help="Chain the token is on")
    parser.add_argument("--record", default="", help="Save every API response (keys removed) to this .json.gz fixture")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or [])
    if args.top <= 0:
        raise SystemExit("--top must be a positive integer.")
    recorder = None
    source_recorder = []
    secrets = [os.environ.get(name, "").strip() for name in ("BLOCKSCOUT_API_KEY", "PUBLICNODE_TOKEN", "HELIUS_API_KEY")]
    if args.record:
        recorder = RecordingFetch(default_fetch_json, secrets)
    solana_sources = []

    def wrap(source):
        if isinstance(source, SolanaSource):
            solana_sources.append(source)
            return source
        if not args.record or not isinstance(source, BlockscoutClient):
            return source
        source_recorder.append(RecordingSource(source))
        return source_recorder[0]

    try:
        report, summary_text, _ = run_cluster_map(
            token_address=args.token,
            out_dir=os.path.abspath(os.path.expanduser(args.out)) if args.out else None,
            top=args.top,
            cache_dir=args.cache,
            chain_key=args.chain,
            fetch_json=recorder,
            wrap_source=wrap,
        )
    except RuntimeError as exc:
        raise SystemExit(f"cluster-map: {exc}")
    if recorder is not None:
        rpc_responses = recorder.dump()
        source_data = source_recorder[0].dump() if source_recorder else None
        if source_data is not None:
            rpc_responses = {k: v for k, v in rpc_responses.items() if "api.blockscout.com" not in k}
        if solana_sources:
            source_data, rpc_responses = {"solana": solana_sources[0].memo}, {}
        meta = {"token": report["token"]["address"], "chain": args.chain, "top": args.top, "generated_at_utc": report["header"]["generated_at_utc"]}
    if args.json:
        print(json.dumps(build_cli_json_summary(report), indent=2))
    else:
        print(summary_text)
    if recorder is not None:
        try:
            save_fixture(args.record, meta, rpc_responses, source_data, secrets)
        except (RuntimeError, OSError) as exc:
            raise SystemExit(f"cluster-map: could not save the fixture: {exc}")


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
