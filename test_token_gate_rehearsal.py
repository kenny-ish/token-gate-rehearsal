import contextlib
import io
import json
import unittest
from unittest.mock import patch

import token_gate_rehearsal as tool


# Ethereum mainnet block 17,000,000 returned by eth_getBlockByNumber(0x1036640, false).
# It is a real chain value used to test block pinning, not a generated fixture.
BLOCK = {
    "number": "0x1036640",
    "hash": "0x96cfa0fb5e50b0a3f6cc76f3299cfbf48f17e8b41798d1394474e67ec8a97e9f",
}
BLOCK_TAG = "0x1036640"
REF_HASH = BLOCK["hash"]
OWNER = "0x" + "aa" * 20
TOKEN = "0x" + "bb" * 20
TARGET = "0x" + "cc" * 20
SPENDER = "0x" + "dd" * 20
CALLDATA = "0x12345678"


class FakeRpc:
    def __init__(self, simulated="0x", baseline_error=None, simulated_error=None):
        self.calls = []
        self.simulated = simulated
        self.baseline_error = baseline_error
        self.simulated_error = simulated_error

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_getBlockByNumber":
            self.assert_block_request(params)
            return BLOCK.copy()
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_call":
            self.assert_call(params)
            if len(params) == 2 and self.baseline_error is not None:
                raise self.baseline_error
            if len(params) == 3 and self.simulated_error is not None:
                raise self.simulated_error
            return self.simulated if len(params) == 3 else "0x"
        raise AssertionError(f"unexpected method: {method}")

    @staticmethod
    def assert_block_request(params):
        if params != ["0x1036640", False]:
            raise AssertionError(params)

    @staticmethod
    def assert_call(params):
        if params[0] != {
            "from": OWNER,
            "to": TARGET,
            "data": CALLDATA,
            "value": "0x0",
            "gas": "0xf4240",
        }:
            raise AssertionError(params[0])
        if params[1] != BLOCK_TAG:
            raise AssertionError(params[1])
        if len(params) == 3 and not isinstance(params[2], dict):
            raise AssertionError(params[2])


def fixture_rehearsal(fake=None):
    return tool.rehearse_call(
        fake or FakeRpc(simulated="0x01"),
        OWNER,
        TARGET,
        CALLDATA,
        block="0x1036640",
        balances=[(TOKEN, OWNER, 9, 1_000_000)],
        allowances=[(TOKEN, OWNER, SPENDER, 10, 2_000_000)],
    )


class KeccakAndEncodingTests(unittest.TestCase):
    def test_keccak_is_ethereum_keccak_not_sha3(self):
        self.assertEqual(
            tool.keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        )
        self.assertEqual(tool.keccak256(b"withdraw(uint256)")[:4].hex(), "2e1a7d4d")

    def test_real_block_fixture_is_pinned(self):
        fake = FakeRpc()
        number, info = tool.pin_block(fake, "0x1036640")
        self.assertEqual(number, BLOCK_TAG)
        self.assertEqual(info["block"], 17_000_000)
        self.assertEqual(info["block_hash"], REF_HASH)

    def test_mapping_slots_and_override_layout(self):
        balance_key = tool.erc20_balance_slot(OWNER, 9)
        allowance_key = tool.erc20_allowance_slot(OWNER, SPENDER, 10)
        self.assertEqual(len(balance_key), 66)
        self.assertEqual(len(allowance_key), 66)
        override = tool.build_state_override(
            OWNER,
            native_balance=3,
            balances=[(TOKEN, OWNER, 9, 1_000_000)],
            allowances=[(TOKEN, OWNER, SPENDER, 10, 2_000_000)],
        )
        self.assertEqual(override[OWNER]["balance"], "0x3")
        self.assertEqual(override[TOKEN]["stateDiff"][balance_key], "0x" + f"{1_000_000:064x}")
        self.assertEqual(override[TOKEN]["stateDiff"][allowance_key], "0x" + f"{2_000_000:064x}")

    def test_conflicting_slot_values_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "different values"):
            tool.build_state_override(
                OWNER,
                balances=[(TOKEN, OWNER, 9, 1), (TOKEN, OWNER, 9, 2)],
            )

    def test_spec_parsers_reject_ambiguous_input(self):
        with self.assertRaises(ValueError):
            tool.parse_balance_override("broken")
        with self.assertRaises(ValueError):
            tool.parse_allowance_override("broken")
        with self.assertRaises(ValueError):
            tool.address("0x12")


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.network = patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("network access is forbidden in tests"),
        )
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_baseline_and_simulated_call_are_distinct(self):
        fake = FakeRpc(simulated="0x01")
        report = fixture_rehearsal(fake)
        self.assertTrue(report["changed"])
        self.assertEqual(report["baseline"], {"status": "return", "data": "0x"})
        self.assertEqual(report["simulated"], {"status": "return", "data": "0x01"})
        calls = [params for method, params in fake.calls if method == "eth_call"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], BLOCK_TAG)
        self.assertEqual(len(calls[1],), 3)
        self.assertIn(TOKEN, calls[1][2])

    def test_same_return_value_is_unchanged(self):
        report = fixture_rehearsal(FakeRpc(simulated="0x"))
        self.assertFalse(report["changed"])

    def test_revert_data_is_preserved(self):
        report = fixture_rehearsal(
            FakeRpc(
                simulated="0x01",
                baseline_error=tool.RpcError(3, "execution reverted", "0xdeadbeef"),
            )
        )
        self.assertEqual(report["baseline"], {"status": "revert", "data": "0xdeadbeef"})
        self.assertTrue(report["changed"])

    def test_missing_revert_data_is_indeterminate(self):
        report = fixture_rehearsal(
            FakeRpc(
                simulated_error=tool.RpcError(3, "execution reverted"),
            )
        )
        self.assertIsNone(report["changed"])
        self.assertIsNone(report["simulated"]["data"])

    def test_non_revert_rpc_error_is_not_swallowed(self):
        fake = FakeRpc(simulated_error=tool.RpcError(-32000, "state override unsupported"))
        with self.assertRaises(tool.RpcError):
            fixture_rehearsal(fake)

    def test_transport_allows_only_read_methods_and_checks_envelope(self):
        def response(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(timeout, 20)
            return io.BytesIO(json.dumps({
                "jsonrpc": "2.0", "id": body["id"], "result": "0x1",
            }).encode())

        with patch("urllib.request.urlopen", side_effect=response):
            rpc = tool.Rpc("https://fixture.invalid")
            self.assertEqual(rpc("eth_chainId", []), "0x1")
            with self.assertRaises(ValueError):
                rpc("eth_sendRawTransaction", [])

    def test_bad_envelope_is_rejected(self):
        reply = {"jsonrpc": "2.0", "id": 999, "result": "0x1"}
        with patch(
            "urllib.request.urlopen",
            return_value=io.BytesIO(json.dumps(reply).encode()),
        ):
            with self.assertRaises(ValueError):
                tool.Rpc("https://fixture.invalid")("eth_chainId", [])

    def test_cli_prints_fixture_result_without_network(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = tool.main(
                [
                    "--rpc", "https://fixture.invalid",
                    "--from", OWNER,
                    "--to", TARGET,
                    "--data", CALLDATA,
                    "--block", "0x1036640",
                    "--erc20-balance", f"{TOKEN}:{OWNER}:9:1000000",
                ],
                rpc=FakeRpc(simulated="0x01"),
            )
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["changed"])


class InputValidationTests(unittest.TestCase):
    def test_no_override_and_zero_gas_are_rejected_before_rpc(self):
        fake = FakeRpc()
        with self.assertRaises(ValueError):
            tool.rehearse_call(fake, OWNER, TARGET, CALLDATA, gas=0)
        with self.assertRaises(ValueError):
            tool.rehearse_call(fake, OWNER, TARGET, CALLDATA)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
