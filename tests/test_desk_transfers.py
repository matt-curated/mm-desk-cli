import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chain_ops import ChainError
from chain_protocol import build_deposit, hex_bytes
from demo_server import DEMO_SEED, start_demo
from mm_desk import Client, DeskError, Shell
from test_chain_operator import KEY, OWNER, TOKEN, SPOT, POOL, successful_simulation

NETWORK = {'chain_id': 111111111111, 'rpc_url': 'https://example.invalid',
           'pool_owner': '0x' + POOL.hex(), 'spot_contract': '0x' + SPOT.hex()}


class DeskTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.key_file = Path(self.directory.name) / 'chain.key'
        self.key_file.write_text(KEY.hex())
        self.server, self.url, self.desk = await start_demo()
        self.client = Client(timeout=1, proxy=False)
        await self.client.connect(self.url)
        await self.client.login(DEMO_SEED, OWNER)
        self.shell = Shell(self.client)
        self.config = patch('desk_transfers.network_for', return_value=NETWORK)
        self.config.start()

    async def asyncTearDown(self):
        self.config.stop()
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()
        self.directory.cleanup()

    async def test_withdrawal_preview_does_not_submit(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            await self.shell.execute('transfer from-desk --token USDT --amount 1 --json')
        result = json.loads(output.getvalue())
        self.assertEqual(result['params'], {'token': f'USDT（{TOKEN}）', 'amount': '1000000', 'to': OWNER})
        self.assertFalse(result['submitted'])
        self.assertNotIn('mm_withdraw', [name for name, _ in self.desk.calls])

    async def test_withdrawal_uses_existing_scope_enforcement(self):
        self.client.session['scopes'] = ['read']
        with self.assertRaisesRegex(DeskError, 'withdraw scope'):
            await self.shell.execute('transfer from-desk --token USDT --raw-amount 1 --yes --no-wait')
        self.assertNotIn('mm_withdraw', [name for name, _ in self.desk.calls])

    async def test_withdrawal_rejects_other_recipient(self):
        with self.assertRaisesRegex(ChainError, 'must return'):
            await self.shell.execute('transfer from-desk --token USDT --amount 1 --to 0x' + '99' * 20 + ' --yes')

    async def test_accepted_withdrawal_returns_request_identifier(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            await self.shell.execute('transfer from-desk --token USDT --amount 1 --yes --no-wait')
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'accepted')
        self.assertIn('pool_request_id', result)
        sent = [params for name, params in self.desk.calls if name == 'mm_withdraw']
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['token'], TOKEN)
        self.assertEqual(sent[0]['to'], OWNER)
        self.assertEqual(sent[0]['amount'], '1000000')

    async def test_deposit_checks_login_owner_before_querying_chain(self):
        self.client.chain_owner = '0x' + '99' * 20
        with patch('desk_transfers.prepare_deposit') as prepare:
            with self.assertRaisesRegex(ChainError, 'does not match'):
                await self.shell.execute(f'transfer to-desk --key-file {self.key_file} --token USDT --amount 1')
        prepare.assert_not_called()

    async def test_deposit_dry_run_never_broadcasts(self):
        tx, info = build_deposit(KEY, hex_bytes(TOKEN, 20), b'\x11' * 32,
                                [(b'\x22' * 20, 2000000)], 1000000, SPOT, POOL, 7, 9)
        with patch('desk_transfers.prepare_deposit', return_value=(tx, info)), \
             patch('chain_ops.Rpc.call', return_value=successful_simulation(tx)) as rpc, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            await self.shell.execute(f'transfer to-desk --key-file {self.key_file} --token USDT --amount 1')
        self.assertEqual(json.loads(output.getvalue())['status'], 'dry_run')
        self.assertEqual([c.args[0] for c in rpc.call_args_list], ['transaction_debugTransaction'])

    async def test_finalized_deposit_waits_for_its_exact_txhash(self):
        from desk_transfers import wait_deposit
        import time
        calls = AsyncMock(side_effect=[[{'txhash': '0xother'}], [{'txhash': '0xours', 'state': 'credited'}]])
        with patch.object(self.client, 'call', calls), patch('desk_transfers.asyncio.sleep', new_callable=AsyncMock):
            result = await wait_deposit(self.client, '0xours', time.monotonic() + 1)
        self.assertEqual(result['txhash'], '0xours')
        self.assertEqual(calls.await_count, 2)

    async def test_credit_read_failure_retains_finalized_deposit_hash(self):
        tx, info = build_deposit(KEY, hex_bytes(TOKEN, 20), b'\x11' * 32,
                                [(b'\x22' * 20, 2000000)], 1000000, SPOT, POOL, 7, 9)
        with patch('desk_transfers.prepare_deposit', return_value=(tx, info)), \
             patch('chain_ops.Rpc.call', side_effect=[successful_simulation(tx), {'hash': info['txhash']},
                                                     {'tx_status': 'Finalized'}]) as rpc, \
             patch.object(self.client, 'call', side_effect=DeskError('connection lost')), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ChainError, 'finalized; desk credit check failed') as error:
                await self.shell.execute(f'transfer to-desk --key-file {self.key_file} --token USDT --amount 1 --yes')
        self.assertIn(info['txhash'], str(error.exception))
        self.assertEqual([x.args[0] for x in rpc.call_args_list].count('mempool_sendTransaction'), 1)

    async def test_withdrawal_wait_matches_request_and_finalized_payout(self):
        from desk_transfers import wait_withdrawal
        from chain_ops import RpcError
        import time
        rows = [{'pool_request_id': 999, 'payout': {'txhash': '0xwrong'}},
                {'pool_request_id': 123, 'payout': {'txhash': '0xours'}}]
        with patch.object(self.client, 'call', new_callable=AsyncMock, return_value=rows) as calls, \
             patch('desk_transfers.receipt', side_effect=[RpcError({'code': -32011, 'message': 'not found'}),
                                                        {'tx_status': 'Finalized'}]) as fetch, \
             patch('desk_transfers.asyncio.sleep', new_callable=AsyncMock):
            result = await wait_withdrawal(self.client, object(), 123, time.monotonic() + 1)
        self.assertEqual(result['withdrawal']['pool_request_id'], 123)
        self.assertEqual(fetch.call_args.args[1], '0xours')
        self.assertEqual(calls.await_count, 2)
        self.assertTrue(all(call.args == ('mm_withdrawals',) for call in calls.call_args_list))


if __name__ == '__main__':
    unittest.main()
