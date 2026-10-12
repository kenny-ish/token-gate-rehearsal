"""Rehearse a token-gated EVM call with eth_call state overrides."""

import argparse
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


# Keccak-f[1600] constants. Ethereum uses Keccak padding (0x01), not SHA3 padding.
ROUND_CONSTANTS = tuple(int(value, 16) for value in (
    "1 8082 800000000000808a 8000000080008000 808b 80000001 "
    "8000000080008081 8000000000008009 8a 88 80008009 8000000a "
    "8000808b 800000000000008b 8000000000008089 8000000000008003 "
    "8000000000008002 8000000000000080 800a 800000008000000a "
    "8000000080008081 8000000000008080 80000001 8000000080008008"
).split())
ROTATIONS = (0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39,
             41, 45, 15, 21, 8, 18, 2, 61, 56, 14)
MASK64 = (1 << 64) - 1


def keccak256(data):
    """Return Ethereum's Keccak-256 digest for bytes."""
    def rotate(value, count):
        return ((value << count) | (value >> (64 - count))) & MASK64

    padded = bytearray(data)
    padded.append(0x01)
    padded.extend(bytes((-len(padded)) % 136))
    padded[-1] |= 0x80
    state = [0] * 25

    for start in range(0, len(padded), 136):
        for index in range(17):
            begin = start + index * 8
            state[index] ^= int.from_bytes(padded[begin:begin + 8], "little")
        for round_constant in ROUND_CONSTANTS:
            columns = [
                state[x] ^ state[x + 5] ^ state[x + 10]
                ^ state[x + 15] ^ state[x + 20]
                for x in range(5)
            ]
            correction = [
                columns[(x - 1) % 5] ^ rotate(columns[(x + 1) % 5], 1)
                for x in range(5)
            ]
            moved = [0] * 25
            for y in range(5):
                for x in range(5):
                    index = x + 5 * y
                    moved[y + 5 * ((2 * x + 3 * y) % 5)] = rotate(
                        state[index] ^ correction[x], ROTATIONS[index]
                    )
            for y in range(5):
                for x in range(5):
                    state[x + 5 * y] = (
                        moved[x + 5 * y]
                        ^ ((~moved[(x + 1) % 5 + 5 * y])
                           & moved[(x + 2) % 5 + 5 * y])
                    )
            state[0] ^= round_constant

    return b"".join(value.to_bytes(8, "little") for value in state)[:32]


def hex_data(value, size=None):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value):
        raise ValueError("expected even-length 0x-prefixed hex data")
    if size is not None and len(value) != 2 + size * 2:
        raise ValueError(f"expected {size} bytes")
    return value.lower()


def integer(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("expected an unsigned integer")
    if isinstance(value, str):
        if not re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value):
            raise ValueError("expected a decimal or 0x integer")
        value = int(value, 16 if value.startswith("0x") else 10)
    if value < 0 or value >= 2**256:
        raise ValueError("integer outside uint256")
    return value


def quantity(value):
    return hex(integer(value))


def word(value):
    return "0x" + f"{integer(value):064x}"


def address(value):
    return hex_data(value, 20)


def address_word(value):
    return f"{int(address(value), 16):064x}"


def mapping_slot(key, slot):
    """Compute keccak256(abi.encode(key, slot)) for a Solidity mapping."""
    return "0x" + keccak256(bytes.fromhex(address_word(key) + f"{integer(slot):064x}")).hex()


def erc20_balance_slot(owner, slot):
    return mapping_slot(owner, slot)


def erc20_allowance_slot(owner, spender, slot):
    inner = mapping_slot(owner, slot)
    return mapping_slot(spender, int(inner, 16))


def parse_balance_override(spec):
    parts = spec.split(":")
    if len(parts) != 4:
        raise ValueError("balance override must be TOKEN:OWNER:SLOT:AMOUNT")
    return address(parts[0]), address(parts[1]), integer(parts[2]), integer(parts[3])


def parse_allowance_override(spec):
    parts = spec.split(":")
    if len(parts) != 5:
        raise ValueError("allowance override must be TOKEN:OWNER:SPENDER:SLOT:AMOUNT")
    return (address(parts[0]), address(parts[1]), address(parts[2]),
            integer(parts[3]), integer(parts[4]))


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class Rpc:
    READ_ONLY_METHODS = {"eth_chainId", "eth_getBlockByNumber", "eth_call"}

    def __init__(self, url, timeout=20):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("RPC URL must use HTTP or HTTPS")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.url = url
        self.timeout = timeout
        self.request_id = 0

    def __call__(self, method, params):
        if method not in self.READ_ONLY_METHODS:
            raise ValueError("RPC method is not supported by this read-only tool")
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "token-gate-rehearsal/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(4_000_001)
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError(f"RPC transport failed: {exc}") from exc
        if len(raw) > 4_000_000:
            raise ValueError("RPC response exceeds 4 MB")
        try:
            reply = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("RPC response is not JSON") from exc
        if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
                or type(reply.get("id")) is not int
                or reply["id"] != self.request_id):
            raise ValueError("invalid JSON-RPC envelope or response id")
        if ("result" in reply) == ("error" in reply):
            raise ValueError("expected exactly one of result or error")
        if "error" in reply:
            error = reply["error"]
            if (not isinstance(error, dict) or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)):
                raise ValueError("invalid JSON-RPC error")
            raise RpcError(error["code"], error["message"], error.get("data"))
        return reply["result"]


def pin_block(rpc, block):
    tag = block if block in ("latest", "safe", "finalized", "earliest") else quantity(block)
    header = rpc("eth_getBlockByNumber", [tag, False])
    if not isinstance(header, dict):
        raise ValueError("block unavailable")
    number = quantity(header.get("number"))
    block_hash = hex_data(header.get("hash"), 32)
    if tag.startswith("0x") and tag != number:
        raise ValueError("RPC returned a different block than requested")
    chain_id = integer(rpc("eth_chainId", []))
    # Use the returned block number for both calls. This pins latest/finalized
    # before the two eth_call requests and works with providers without EIP-1898.
    return number, {
        "chain_id": chain_id,
        "block": int(number, 16),
        "block_hash": block_hash,
    }


def _revert_data(value):
    for _ in range(4):
        if not isinstance(value, dict):
            break
        value = value.get("data", value.get("result", value.get("return")))
    if value is None:
        return None
    try:
        return hex_data(value)
    except ValueError:
        return None


def call_result(rpc, transaction, block_tag, override=None):
    params = [transaction, block_tag]
    if override is not None:
        params.append(override)
    try:
        result = rpc("eth_call", params)
        return {"status": "return", "data": hex_data(result)}
    except RpcError as exc:
        if exc.code != 3 and "revert" not in str(exc).lower():
            raise
        return {"status": "revert", "data": _revert_data(exc.data)}


def _set_storage(override, token, key, value):
    account = override.setdefault(token, {})
    storage = account.setdefault("stateDiff", {})
    previous = storage.get(key)
    encoded = word(value)
    if previous is not None and previous != encoded:
        raise ValueError(f"two overrides assign different values to {token}:{key}")
    storage[key] = encoded


def build_state_override(from_address, native_balance=None, balances=(), allowances=()):
    sender = address(from_address)
    override = {}
    if native_balance is not None:
        override[sender] = {"balance": quantity(native_balance)}
    for token, owner, slot, amount in balances:
        _set_storage(override, token, erc20_balance_slot(owner, slot), amount)
    for token, owner, spender, slot, amount in allowances:
        _set_storage(override, token, erc20_allowance_slot(owner, spender, slot), amount)
    if not override:
        raise ValueError("provide at least one native, balance, or allowance override")
    return override


def rehearse_call(rpc, from_address, to_address, data, *, value=0, gas=1_000_000,
                  block="finalized", native_balance=None, balances=(), allowances=()):
    sender = address(from_address)
    target = address(to_address)
    calldata = hex_data(data)
    gas = integer(gas)
    if gas == 0:
        raise ValueError("gas must be positive")
    transaction = {
        "from": sender,
        "to": target,
        "data": calldata,
        "value": quantity(value),
        "gas": quantity(gas),
    }
    override = build_state_override(sender, native_balance, balances, allowances)
    block_tag, info = pin_block(rpc, block)
    baseline = call_result(rpc, transaction, block_tag)
    simulated = call_result(rpc, transaction, block_tag, override)
    if baseline["data"] is None or simulated["data"] is None:
        changed = None
    else:
        changed = baseline != simulated
    return {
        **info,
        "transaction": transaction,
        "baseline": baseline,
        "simulated": simulated,
        "changed": changed,
        "state_override": override,
    }


def main(argv=None, rpc=None):
    parser = argparse.ArgumentParser(
        description="Rehearse an EVM call with hypothetical native/token state."
    )
    parser.add_argument("--rpc", required=True, help="HTTP(S) JSON-RPC URL")
    parser.add_argument("--from", dest="from_address", required=True)
    parser.add_argument("--to", dest="to_address", required=True)
    parser.add_argument("--data", required=True, help="call data, including 0x")
    parser.add_argument("--value", default="0", help="call value in wei")
    parser.add_argument("--gas", default="1000000")
    parser.add_argument("--block", default="finalized")
    parser.add_argument("--native-balance", help="hypothetical balance of --from in wei")
    parser.add_argument(
        "--erc20-balance", action="append", default=[], metavar="TOKEN:OWNER:SLOT:AMOUNT",
        help="repeatable mapping override; SLOT is the Solidity balance mapping slot",
    )
    parser.add_argument(
        "--erc20-allowance", action="append", default=[],
        metavar="TOKEN:OWNER:SPENDER:SLOT:AMOUNT",
        help="repeatable nested mapping override; SLOT is the allowance mapping slot",
    )
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args(argv)
    try:
        balances = [parse_balance_override(item) for item in args.erc20_balance]
        allowances = [parse_allowance_override(item) for item in args.erc20_allowance]
        native = None if args.native_balance is None else integer(args.native_balance)
        client = rpc if rpc is not None else Rpc(args.rpc, args.timeout)
        result = rehearse_call(
            client,
            args.from_address,
            args.to_address,
            args.data,
            value=args.value,
            gas=args.gas,
            block=args.block,
            native_balance=native,
            balances=balances,
            allowances=allowances,
        )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RpcError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
