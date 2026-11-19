import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coincurve import PublicKey
from chain_ops import (ChainError, Rpc, amount_raw, execute_prepared, owner_of,
                       select_funding, simulate, token_address)
from chain_protocol import (Leaf, asset_token, build_transfer, build_deposit, eip712_digest,
                            evm_address, hex_bytes, keccak256, sign_prehash, to_hex)

KEY = (1).to_bytes(32, 'big')
TOKEN = '0xaca71f1748aa4c3291bbccd905b71224cb1b602f'
OWNER = owner_of(KEY)
RECEIVER = '0x' + '33' * 20
SPOT = bytes.fromhex('7f6471e342706340aa357edd8d5fc577bb0959c1')
POOL = bytes.fromhex('8a6ec99a0b55a63797801cc8dc6e962ba0e3fc84')


def fixture_tx():
    return build_transfer(KEY, hex_bytes(TOKEN, 20), b'\x11' * 32,
                          [(b'\x22' * 20, 2000000)], 1000000, b'\x33' * 20, 9, 7)


def successful_simulation(tx):
    return {'result': {'unlockers': [{'code': 0}] * len(tx.inputs),
                       'operators': [{'operator': to_hex(op), 'code': 0}
                                     for op in {x.operator for x in tx.outputs}]}}


class ChainOperatorTests(unittest.TestCase):
    def test_native_signature_and_transfer_match_reference_vector(self):
        tx, info = fixture_tx()
        self.assertEqual(info['txhash'], '0x00c07e69e2b9cd70076c9a2af5e3e74ff96e6fd45b83ef74d828cf16df27b275')
        sig = sign_prehash(KEY, eip712_digest(tx.hash()))
        recovered = PublicKey.from_signature_and_message(sig[:64] + bytes([sig[64] - 27]),
                                                         eip712_digest(tx.hash()), hasher=None)
        self.assertEqual(keccak256(recovered.format(compressed=False)[1:])[-20:], evm_address(KEY))
        self.assertEqual(tx.unlockers[0][-65:], sig)

    def test_amounts_are_exact_above_decimal_default_precision(self):
        eth = token_address('ETH')
        amount = '1234567890123456789.123456789012345678'
        self.assertEqual(amount_raw(eth, amount), 1234567890123456789123456789012345678)
        self.assertEqual(amount_raw(TOKEN, '1.000001'), 1000001)
        self.assertEqual(amount_raw(TOKEN, raw_amount='1'), 1)
        for value in ['0', '-1', 'NaN', 'Infinity', '1e6', '0.0000001']:
            with self.subTest(value=value), self.assertRaises(ChainError):
                amount_raw(TOKEN, value)
        with self.assertRaises(ChainError):
            amount_raw(TOKEN, raw_amount=str(2 ** 128))

    def test_deposit_command_and_pool_lock_layout(self):
        tx, info = build_deposit(KEY, hex_bytes(TOKEN, 20), b'\x11' * 32,
                                [(b'\x22' * 20, 2000000)], 1000000, SPOT, POOL, 7, 9)
        command, locked, change = tx.outputs
        self.assertEqual(command.address(), tx.inputs[0])
        self.assertEqual(command.data[1:5], keccak256(b'deposit_pool()')[:4])
        self.assertEqual(to_hex(locked.owner), '0xd39d743751cab1ac5dbb3377fd4be5bf5f2b6ffd')
        self.assertEqual(locked.index[2][0][-20:], SPOT)
        self.assertEqual(locked.index[3][0][-20:], POOL)
        self.assertEqual(change.owner, hex_bytes(OWNER, 20))
        self.assertEqual(info['amount_raw'], '1000000')
        self.assertEqual(len(tx.unlockers), len(tx.inputs))

    def test_rpc_ids_headers_and_pagination(self):
        replies = [dict(jsonrpc='2.0', id=1, result={'leaves': [{'address': '0x' + '11' * 20}]}),
                   dict(jsonrpc='2.0', id=2, result={'leaves': []})]
        with patch('urllib.request.urlopen', side_effect=[io.BytesIO(json.dumps(x).encode()) for x in replies]) as send:
            records = list(Rpc(owner=OWNER).leaves(OWNER, 1))
        self.assertEqual(len(records), 1)
        second = send.call_args_list[1].args[0]
        self.assertEqual(json.loads(second.data)['params']['cursor'], '0x' + '11' * 20)
        self.assertEqual(second.headers['X-app-key'], OWNER)

    def test_repeated_pagination_is_an_error_not_double_balance(self):
        item = {'address': '0x' + '11' * 20}
        with patch.object(Rpc, 'call', return_value={'leaves': [item]}):
            with self.assertRaisesRegex(ChainError, 'Repeated leaf'):
                list(Rpc().leaves(OWNER, 1))

    def test_simulation_requires_success_of_every_unlocker_and_operator(self):
        tx, _ = fixture_tx()
        good = successful_simulation(tx)
        with patch.object(Rpc, 'call', return_value=good):
            self.assertEqual(simulate(Rpc(), tx), good)
        for bad in [{}, {'result': {}}, {'result': {'status': 'Success'}},
                    {'result': {'unlockers': [{'code': 1}], 'operators': good['result']['operators']}},
                    {'result': {'unlockers': [{'code': 0}], 'operators': []}}]:
            with patch.object(Rpc, 'call', return_value=bad):
                with self.assertRaises(ChainError):
                    simulate(Rpc(), tx)

    def test_default_dry_run_never_calls_submit(self):
        tx, info = fixture_tx()
        with patch.object(Rpc, 'call', return_value=successful_simulation(tx)) as call:
            result = execute_prepared(Rpc(), tx, info)
        self.assertEqual(result['status'], 'dry_run')
        self.assertFalse(result['submitted'])
        self.assertEqual([x.args[0] for x in call.call_args_list], ['transaction_debugTransaction'])

    def test_unknown_submission_is_never_retried(self):
        tx, info = fixture_tx()
        with patch.object(Rpc, 'call', side_effect=[successful_simulation(tx), TimeoutError('lost response')]) as call:
            with self.assertRaisesRegex(ChainError, 'Submission status unknown') as error:
                execute_prepared(Rpc(), tx, info, send=True)
        self.assertIn(info['txhash'], str(error.exception))
        self.assertEqual(call.call_count, 2)

    def test_submitted_hash_must_match_local_hash(self):
        tx, info = fixture_tx()
        with patch.object(Rpc, 'call', side_effect=[successful_simulation(tx), {'hash': '0x' + 'ff' * 32}]):
            with self.assertRaisesRegex(ChainError, 'hash mismatch'):
                execute_prepared(Rpc(), tx, info, send=True)

    def test_finality_read_failure_retains_submitted_hash(self):
        tx, info = fixture_tx()
        with patch.object(Rpc, 'call', side_effect=[successful_simulation(tx),
                                                   {'hash': info['txhash']}, OSError('receipt unavailable')]) as call:
            with self.assertRaisesRegex(ChainError, 'was submitted') as error:
                execute_prepared(Rpc(), tx, info, send=True)
        self.assertIn(info['txhash'], str(error.exception))
        self.assertEqual([x.args[0] for x in call.call_args_list].count('mempool_sendTransaction'), 1)

    def test_spend_selection_rejects_duplicates_and_skips_pending_locked_leaves(self):
        def record(nonce, pending=False, locked=False):
            leaf = asset_token(hex_bytes(OWNER, 20), hex_bytes(TOKEN, 20), b'\x11' * 32, 1000000,
                               nonce, lock_to=SPOT if locked else None)
            return {'address': to_hex(leaf.address()), 'leaf': leaf.to_json(),
                    'info': {'deleted_txhash': None, 'pending': pending}}
        records = [record(1), record(2, pending=True), record(3, locked=True)]
        with patch.object(Rpc, 'leaves', return_value=iter(records)):
            result = select_funding(Rpc(), OWNER, TOKEN, 1000000)
        self.assertEqual(len(result), 1)
        with patch.object(Rpc, 'leaves', return_value=iter(records)):
            with self.assertRaisesRegex(ChainError, 'Duplicate'):
                select_funding(Rpc(), OWNER, TOKEN, 1000000, [records[0]['address']] * 2)


if __name__ == '__main__':
    unittest.main()
