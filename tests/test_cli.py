import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mm_desk import (BY_NAME, Client, DeskError, HelpShown, RpcError, Shell,
                     LatencyLog, default_latency_log, arguments, hex_bytes, load_seed,
                     now_ms, prepare_params, show_manual)
from demo_server import DEMO_SEED, DemoDesk, DemoError, start_demo

ORDER = dict(book='btc_usdt', side='buy', price='900719925474099312345', quantity='1000', tif='gtc')


class ValidationTests(unittest.TestCase):
    def test_large_amounts_remain_exact_strings(self):
        p = prepare_params('mm_placeOrder', ORDER)
        self.assertEqual(p['price'], '900719925474099312345')
        self.assertIsInstance(p['ts'], int)
        self.assertNotIn('ts', ORDER)

    def test_reject_invalid_types_and_extra_fields(self):
        for patch in ({'price': 1.2}, {'quantity': '0.1'}, {'quantity': '-1'},
                      {'post_only': 'true'}, {'side': 'other'}, {'ts': True}, {'typo': 1}):
            with self.subTest(patch=patch), self.assertRaises(DeskError):
                prepare_params('mm_placeOrder', dict(ORDER, **patch))

    def test_modify_total_and_remaining_are_exclusive(self):
        for p in ({'order_id': 1}, {'order_id': 1, 'quantity': '3', 'remaining': '2'}):
            with self.assertRaises(DeskError):
                prepare_params('mm_modifyOrder', p)
        self.assertEqual(prepare_params('mm_modifyOrder', {'order_id': 1, 'remaining': '0'})['remaining'], '0')

    def test_batch_limits_and_each_timestamp(self):
        for orders in ([], [ORDER] * 51, 'wrong', [None]):
            with self.assertRaises(DeskError):
                prepare_params('mm_placeOrders', {'orders': orders})
        result = prepare_params('mm_placeOrders', {'orders': [ORDER] * 50})
        self.assertEqual(len(result['orders']), 50)
        self.assertTrue(all(abs(x['ts'] - now_ms()) < 1000 for x in result['orders']))

    def test_time_and_schedule_validation(self):
        with self.assertRaises(DeskError):
            prepare_params('mm_listOrders', {'from': 100, 'to': 99})
        p = dict(book='btc_usdt', side='buy', quantity='10', limit_price='10',
                 start=1000, end=2000, interval_ms=0, kind='twap')
        with self.assertRaises(DeskError):
            prepare_params('mm_placeAlgo', p)

    def test_master_derivation_known_vector(self):
        # Independently computed using cryptography's RFC 5869 implementation.
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        expected = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'bbm-mm-keys/v1',
                        info=b'desk/ed25519').derive(DEMO_SEED)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.seed'
            path.write_text('0x' + DEMO_SEED.hex() + '\n')
            self.assertEqual(load_seed(path), DEMO_SEED)
            self.assertEqual(load_seed(path, master=True), expected)

    def test_invalid_seed_errors_do_not_disclose_content(self):
        private_text = 'DO_NOT_PRINT_THIS_SECRET'
        with self.assertRaises(DeskError) as cm:
            hex_bytes(private_text, 32, 'seed')
        self.assertNotIn(private_text, str(cm.exception))

    def test_help_and_defaults(self):
        self.assertEqual(arguments([]).url, 'wss://desk.testnet.curated.finance')
        self.assertTrue(arguments(['--demo']).demo)

    def test_latency_log_uses_xdg_state_and_explicit_override(self):
        with patch.dict('os.environ', {'XDG_STATE_HOME': '/tmp/mm-state'}):
            self.assertEqual(default_latency_log(), Path('/tmp/mm-state/mm-desk/latency.jsonl'))
            self.assertEqual(arguments(['--latency-log', '/tmp/custom.jsonl']).latency_log,
                             Path('/tmp/custom.jsonl'))
        with patch.dict('os.environ', {'XDG_STATE_HOME': 'relative-invalid'}):
            self.assertEqual(default_latency_log(), Path.home() / '.local/state/mm-desk/latency.jsonl')


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server, self.url, self.desk = await start_demo(DemoDesk(noise=True))
        self.client = Client(timeout=1, proxy=False)
        await self.client.connect(self.url)
        await self.client.login(DEMO_SEED)

    async def asyncTearDown(self):
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()

    @contextlib.contextmanager
    def timing_log(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as output:
            path = Path(folder) / 'state' / 'latency.jsonl'
            self.client.latency_log = LatencyLog(path)
            try:
                yield path, output
            finally:
                self.client.latency_log = None

    async def test_order_timings_cover_aliases_raw_calls_batch_and_algos(self):
        with self.timing_log() as (path, output), contextlib.redirect_stdout(io.StringIO()) as stdout:
            shell = Shell(self.client)
            await shell.execute('place --book btc_usdt --side buy --price 6000000 --quantity 10000 --tif gtc')
            oid = next(iter(self.desk.orders))
            await shell.execute(f'mm_modifyOrder --order-id {oid} --price 6100000')
            await shell.execute('rpc mm_cancelOrder ' + json.dumps({'order_id': oid}))
            await self.client.call('mm_placeOrders', {'orders': [ORDER, ORDER]})
            await self.client.call('mm_cancelAll')
            await self.client.call('mm_cancelAll')  # Empty success still has one RTT.
            algo = await self.client.call('mm_placeAlgo', dict(book='btc_usdt', side='buy', quantity='100',
                       limit_price='100', end=now_ms() + 60000, interval_ms=1000, kind='twap'))
            await self.client.call('mm_cancelAlgo', {'parent_id': algo['parent_id']})
            await self.client.call('mm_balances')
            await self.client.login(DEMO_SEED)  # Credentials never enter the timing log.
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 8)
            self.assertEqual(len({row['request_id'] for row in rows}), 8)
            self.assertEqual(rows[0]['results'][0]['order_id'], oid)
            self.assertEqual(rows[1]['request']['order_id'], oid)
            self.assertEqual(rows[3]['request']['count'], 2)
            self.assertEqual(len(rows[3]['results']), 2)
            self.assertEqual(rows[5]['outcome'], 'success')
            self.assertTrue(all(row['rtt_ms'] > 0 for row in rows))
            self.assertTrue(all(row['elapsed_ms'] == row['rtt_ms'] for row in rows))
            self.assertTrue(all(row['mm_id'] == 1 for row in rows))
            self.assertEqual(output.getvalue().count('TTL (RTT):'), 8)
            self.assertNotIn('TTL', stdout.getvalue())
            self.assertNotIn('signature', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            first_run = rows[0]['run_id']
            self.client.latency_log = LatencyLog(path)
            await self.client.call('mm_cancelAll')
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 9)
            self.assertNotEqual(rows[-1]['run_id'], first_run)

    async def test_rejection_rpc_error_and_local_errors_have_distinct_timings(self):
        with self.timing_log() as (path, output):
            with self.assertRaises(DeskError):
                await self.client.call('mm_placeOrder', dict(ORDER, quantity='invalid'))
            self.assertFalse(path.exists())
            order = await self.client.call('mm_placeOrder', ORDER)
            await self.client.call('mm_cancelOrder', {'order_id': order['order_id']})
            result = await self.client.call('mm_cancelOrder', {'order_id': order['order_id']})
            self.assertEqual(result['status'], 'rejected')
            with self.assertRaises(RpcError):
                await self.client.call('mm_placeOrder', dict(ORDER, ts=1))
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(rows[-2]['outcome'], 'rejected')
            self.assertEqual(rows[-2]['results'][0]['code'], 'already_closed')
            self.assertEqual(rows[-1]['outcome'], 'rpc_error')
            self.assertEqual(rows[-1]['rpc_error_code'], -32002)
            self.assertIsNotNone(rows[-1]['rtt_ms'])
            self.assertEqual(output.getvalue().count('TTL (RTT):'), 4)

    async def test_timeout_is_wait_time_and_late_response_does_not_duplicate_log(self):
        original = self.desk.dispatch
        async def delayed(name, params, session):
            result = await original(name, params, session)
            await asyncio.sleep(0.08)
            return result
        self.desk.dispatch = delayed
        self.client.timeout = 0.02
        with self.timing_log() as (path, output):
            with self.assertRaisesRegex(DeskError, 'Execution status unknown'):
                await self.client.call('mm_placeOrder', ORDER)
            await asyncio.sleep(0.1)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['outcome'], 'timeout')
            self.assertIsNone(rows[0]['rtt_ms'])
            self.assertGreaterEqual(rows[0]['elapsed_ms'], 20)
            self.assertIn('unavailable (waited', output.getvalue())
            self.assertEqual(sum(name == 'mm_placeOrder' for name, _ in self.desk.calls), 1)
            self.assertEqual(self.client.response_times, {})

    async def test_logging_failure_does_not_change_order_result_or_retry(self):
        with self.timing_log() as (_, output), patch('mm_desk.os.open', side_effect=PermissionError('read-only')):
            result = await self.client.call('mm_placeOrder', ORDER)
            self.assertEqual(result['status'], 'accepted')
            self.assertIn('cannot write latency log', output.getvalue())
            self.assertIn('TTL (RTT):', output.getvalue())
            self.assertEqual(sum(name == 'mm_placeOrder' for name, _ in self.desk.calls), 1)

    async def test_rtt_excludes_log_writes_and_redacts_endpoint_credentials(self):
        with self.timing_log() as (path, _):
            original = self.client.latency_log.record
            def slow_log(event):
                time.sleep(0.06)
                original(event)
            self.client.url = 'wss://user:secret@desk.example/ws?token=secret#secret'
            start = time.perf_counter()
            with patch.object(self.client.latency_log, 'record', side_effect=slow_log):
                await self.client.call('mm_placeOrder', ORDER)
            elapsed = (time.perf_counter() - start) * 1000
            row = json.loads(path.read_text())
            self.assertGreaterEqual(elapsed - row['rtt_ms'], 55)
            self.assertEqual(row['endpoint'], 'wss://desk.example/ws')
            self.assertNotIn('secret', path.read_text())

    async def test_connection_failure_prints_and_records_elapsed_without_rtt(self):
        with self.timing_log() as (path, output):
            with patch.object(self.client.ws, 'send', new_callable=AsyncMock, side_effect=OSError('disconnected')):
                with self.assertRaisesRegex(DeskError, 'connection error'):
                    await self.client.call('mm_placeOrder', ORDER)
            row = json.loads(path.read_text())
            self.assertEqual(row['outcome'], 'connection_error')
            self.assertIsNone(row['rtt_ms'])
            self.assertIn('unavailable', output.getvalue())

    async def test_native_manual_keeps_connection_responsive(self):
        async def read_manual():
            # A request must complete while the external viewer remains open.
            self.assertIsInstance(await self.client.call('mm_balances'), list)
            return 0
        process = type('Viewer', (), {'wait': AsyncMock(side_effect=read_manual)})()
        with patch('mm_desk.shutil.which', return_value='/usr/bin/man'), \
             patch('mm_desk.asyncio.create_subprocess_exec', new_callable=AsyncMock,
                   return_value=process) as spawn:
            await Shell(self.client).execute('manual')
        spawn.assert_awaited_once_with('/usr/bin/man', str(Path(__file__).resolve().parents[1] / 'mm-desk.1'))
        self.assertTrue(self.client.connected)
        self.assertIsNotNone(self.client.session)

    async def test_missing_manual_viewer_reports_error_without_dumping_markdown(self):
        with patch('mm_desk.shutil.which', return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(DeskError, 'man\\(1\\) is not installed'):
                await show_manual()
        self.assertEqual(output.getvalue(), '')

    async def test_manual_help_and_bad_arguments_do_not_open_viewer(self):
        with patch('mm_desk.asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(HelpShown):
                await Shell(self.client).execute('manual --help')
            with self.assertRaises(DeskError):
                await Shell(self.client).execute('manual invented-topic')
        self.assertIn('usage: manual', output.getvalue())
        spawn.assert_not_awaited()

    async def test_all_23_methods_end_to_end(self):
        c = self.client.call
        self.assertEqual(len(BY_NAME), 23)
        self.assertTrue((await c('mm_setDeadman', {'timeout_ms': 10000}))['armed'])
        self.assertFalse((await c('mm_setDeadman', {'timeout_ms': 0}))['armed'])
        self.assertIn('token', await c('mm_gatewayToken'))
        order = await c('mm_placeOrder', ORDER)
        oid, rid = order['order_id'], order['pool_request_id']
        self.assertEqual((await c('mm_getOrder', {'order_id': oid}))['price'], ORDER['price'])
        self.assertEqual((await c('mm_getRequest', {'pool_request_id': rid}))['state'], 'accepted')
        await c('mm_modifyOrder', {'order_id': oid, 'remaining': '500'})
        self.assertEqual((await c('mm_getOrder', {'order_id': oid}))['remaining'], '500')
        self.assertEqual(len(await c('mm_openOrders', {'book': 'btc_usdt'})), 1)
        self.assertEqual(len(await c('mm_listOrders', {'state': 'open', 'limit': 5})), 1)
        self.assertEqual(len(await c('mm_listRequests', {'book': 'btc_usdt'})), 2)
        await c('mm_cancelOrder', {'order_id': oid})
        batch = await c('mm_placeOrders', {'orders': [ORDER, dict(ORDER, side='sell')]})
        self.assertEqual(len(batch), 2)
        self.assertEqual(len(await c('mm_cancelAll', {'book': 'btc_usdt'})), 2)
        self.assertEqual(await c('mm_openOrders'), [])
        self.assertIsInstance(await c('mm_balances'), list)
        self.assertEqual(await c('mm_deposits'), [])
        await c('mm_withdraw', {'token': 'usdt', 'amount': '1', 'to': '0x' + '11' * 20})
        self.assertEqual(len(await c('mm_withdrawals')), 1)
        algo = await c('mm_placeAlgo', dict(book='btc_usdt', side='buy', quantity='100',
                       limit_price='100', end=now_ms() + 60000, interval_ms=1000, kind='twap'))
        parent = {'parent_id': algo['parent_id']}
        self.assertEqual((await c('mm_pauseAlgo', parent))['state'], 'paused')
        self.assertEqual((await c('mm_resumeAlgo', parent))['state'], 'running')
        self.assertEqual((await c('mm_getAlgo', parent))['state'], 'running')
        self.assertEqual((await c('mm_cancelAlgo', parent))['state'], 'cancelled')
        self.assertEqual(set(BY_NAME), {name for name, _ in self.desk.calls})
        self.assertGreater(self.client.ignored, 0)

    async def test_real_signature_rejects_wrong_key(self):
        with self.assertRaises(RpcError) as cm:
            await self.client.login(bytes([9]) * 32)
        self.assertEqual(cm.exception.code, -32001)
        self.assertIsNone(self.client.session)

    async def test_scope_check_prevents_sending(self):
        self.client.session['scopes'] = ['read']
        count = len(self.desk.calls)
        with self.assertRaisesRegex(DeskError, 'trade'):
            await self.client.call('mm_placeOrder', ORDER)
        self.assertEqual(len(self.desk.calls), count)
        self.assertIsInstance(await self.client.call('mm_balances'), list)

    async def test_business_rejection_is_result_not_rpc_error(self):
        item = await self.client.call('mm_placeOrder', ORDER)
        await self.client.call('mm_cancelOrder', {'order_id': item['order_id']})
        result = await self.client.call('mm_cancelOrder', {'order_id': item['order_id']})
        self.assertEqual(result['status'], 'rejected')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(Shell.display(result))

    async def test_stale_timestamp_and_not_found_codes(self):
        for name, params, code in [('mm_placeOrder', dict(ORDER, ts=1), -32002),
                                    ('mm_getOrder', {'order_id': 9999}, -32011)]:
            with self.subTest(name=name), self.assertRaises(RpcError) as cm:
                await self.client.call(name, params)
            self.assertEqual(cm.exception.code, code)

    async def test_timeout_does_not_resubmit_order(self):
        original = self.desk.dispatch
        async def delayed(name, p, session):
            result = await original(name, p, session)
            if name == 'mm_placeOrder':
                await asyncio.sleep(0.1)
            return result
        self.desk.dispatch = delayed
        self.client.timeout = 0.02
        with self.assertRaisesRegex(DeskError, 'Execution status unknown'):
            await self.client.call('mm_placeOrder', ORDER)
        await asyncio.sleep(0.15)
        self.assertEqual(len(self.desk.orders), 1)
        self.assertEqual(sum(name == 'mm_placeOrder' for name, _ in self.desk.calls), 1)
        self.client.timeout = 1
        self.assertEqual(len(await self.client.call('mm_openOrders')), 1)

    async def test_disconnect_clears_auth(self):
        for connection in list(self.server.connections):
            await connection.close()
        await asyncio.sleep(0.02)
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client.session)
        await self.client.connect(self.url)
        with self.assertRaisesRegex(DeskError, 'Not logged in'):
            await self.client.call('mm_balances')

    async def test_shell_parsing_json_flags_batch_and_errors(self):
        shell = Shell(self.client)
        with contextlib.redirect_stdout(io.StringIO()):
            await shell.execute('place --book btc_usdt --side buy --price 100 --quantity 20 --tif gtc --post-only')
            await shell.execute('rpc mm_placeOrder ' + json.dumps(ORDER))
            await shell.execute('mm_balances')
            with self.assertRaises(HelpShown):
                await shell.execute('place --help')
            with self.assertRaises(DeskError):
                await shell.execute('place --side buy')
            with self.assertRaises(DeskError):
                await shell.execute('rpc mm_balances []')
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'orders.json'
                path.write_text(json.dumps([ORDER, ORDER]))
                await shell.execute('batch --orders @' + str(path))
        self.assertEqual(len(self.desk.orders), 4)
        self.assertTrue(next(iter(self.desk.orders.values()))['post_only'])


if __name__ == '__main__':
    unittest.main()
