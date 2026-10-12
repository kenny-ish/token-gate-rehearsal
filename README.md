# Token Gate Rehearsal

Token Gate Rehearsal answers one practical question: would a token-gated EVM call return a different result if a chosen account had a hypothetical native balance, ERC-20 balance, or allowance? It only uses `eth_call` with state overrides, so it never submits a transaction.

## Example

This run was made against Ethereum mainnet through `https://rpc.mevblocker.io`. It calls WETH9 `withdraw(uint256)` as an arbitrary account. The baseline reverts because the account has no WETH; the overridden call returns successfully.

```text
$ python token_gate_rehearsal.py --rpc https://rpc.mevblocker.io --from 0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --to 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2 --data 0x2e1a7d4d0000000000000000000000000000000000000000000000000de0b6b3a7640000 --block latest --erc20-balance 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2:0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:3:2000000000000000000
{
  "chain_id": 1,
  "block": 26161175,
  "block_hash": "0xb75aa19483efa243436c6ed2343c63c544a9e0b047b9f72d9b50f1869e17f924",
  "transaction": {
    "from": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "to": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "data": "0x2e1a7d4d0000000000000000000000000000000000000000000000000de0b6b3a7640000",
    "value": "0x0",
    "gas": "0xf4240"
  },
  "baseline": {
    "status": "revert",
    "data": "0x"
  },
  "simulated": {
    "status": "return",
    "data": "0x"
  },
  "changed": true,
  "state_override": {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {
      "stateDiff": {
        "0xca0453669a7127ce38f304ce121e552d78c30286022ebefeef6884684816084d": "0x0000000000000000000000000000000000000000000000001bc16d674ec80000"
      }
    }
  }
}
```

## Usage

```bash
python token_gate_rehearsal.py \
  --rpc https://your-rpc.example \
  --from 0x2222222222222222222222222222222222222222 \
  --to 0x3333333333333333333333333333333333333333 \
  --data 0x12345678 \
  --erc20-balance TOKEN:OWNER:SLOT:AMOUNT \
  --erc20-allowance TOKEN:OWNER:SPENDER:SLOT:AMOUNT \
  --native-balance 1000000000000000000
```

`--erc20-balance` and `--erc20-allowance` can be repeated. `SLOT` is the Solidity storage slot of the token's `balanceOf` or `allowance` mapping. Amounts are raw token units. Use `--value`, `--gas`, and `--block` to set the call, and pass a block number or `finalized` rather than relying on a moving read.

## How it works

The tool computes Solidity mapping keys with Ethereum Keccak-256, builds a `stateDiff` override, then makes a baseline `eth_call` and an overridden `eth_call` at the same pinned block. It reports whether the return or revert result changed. Only read-only JSON-RPC methods are allowed. A missing revert payload is reported as indeterminate.

## Tests

```bash
python -m unittest -v
```
