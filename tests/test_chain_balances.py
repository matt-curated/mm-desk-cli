import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chain_balances import (ASSETS, DEFAULT_RPC, decimal_string,
                            fetch_balances, summarize)
from mm_desk import Client, DeskError, Shell, BY_NAME, cli_value
from demo_server import DEMO_SEED, start_demo

TOKEN = '0xaca71f1748aa4c3291bbccd905b71224cb1b602f'
TEST_OWNER = '0x' + '11' * 20
OTHER_OWNER = '0x' + '22' * 20


def leaf(raw, token=TOKEN, deleted=None, kind=3):
    return {'leaf': {'operator': token, 'index': [{'value': '0x00'}, {'value': hex(kind)}],
                     'data': '0x' + '00' * 32 + f'{raw:064x}' + 'ff' * 32},
            'info': {'deleted_txhash': deleted}}


class ChainTests(unittest.TestCase):
    def test_abi_amount_aggregation_and_deleted_filter(self):
        result = summarize({'leaves': [leaf(1200000), leaf(3400000), leaf(999, deleted='0xdead'),
                                       leaf(999, kind=2)]}, TEST_OWNER, DEFAULT_RPC, 1000)
        self.assertEqual(result['balances'][0]['raw'], '4600000')
        self.assertEqual(result['balances'][0]['balance'], '4.6')
        self.assertEqual(result['balance_leaf_count'], 2)
        self.assertFalse(result['possibly_partial'])

    def test_no_large_integer_or_decimal_rounding(self):
        raw = 10 ** 60 + 123456789012345678
        self.assertEqual(decimal_string(raw, 18), str(10 ** 42) + '.123456789012345678')
        self.assertEqual(decimal_string(1, 18), '0.000000000000000001')
        self.assertEqual(decimal_string(100, 0), '100')
        self.assertEqual(decimal_string(0, 8), '0')

    def test_unknown_tokens_keep_raw_without_guessing_precision(self):
        result = summarize({'leaves': [leaf(123, token='0x' + '11' * 20)]}, TEST_OWNER, DEFAULT_RPC, 1000)
        row = result['balances'][0]
        self.assertEqual(row['raw'], '123')
        self.assertIsNone(row['decimals'])
        self.assertIsNone(row['balance'])
        self.assertTrue(result['notes'])

    def test_full_page_and_malformed_leaves_mark_partial(self):
        for result, limit in [({'leaves': [leaf(1)]}, 1), ({'leaves': [{}]}, 1000),
                               ({'leaves': [], 'has_more': True}, 1000)]:
            self.assertTrue(summarize(result, TEST_OWNER, DEFAULT_RPC, limit)['possibly_partial'])
        with self.assertRaises(ValueError):
            summarize({'wrong': []}, TEST_OWNER, DEFAULT_RPC, 1000)

    def test_http_payload_headers_and_json_precision(self):
        reply = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'leaves': [leaf(10**18)]}}).encode()
        with patch('urllib.request.urlopen', return_value=io.BytesIO(reply)) as send:
            result = fetch_balances(TEST_OWNER)
        request = send.call_args.args[0]
        self.assertEqual(json.loads(request.data)['method'], 'leaf_getLeafByOwner')
        self.assertEqual(json.loads(request.data)['params'], {'owner': TEST_OWNER, 'limit': 1000})
        self.assertEqual(request.headers['User-agent'], 'Mozilla/5.0')
        self.assertEqual(result['balances'][0]['balance'], '1000000000000')

    def test_rpc_errors_and_response_id_checked(self):
        for reply in ({'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32602, 'message': 'bad owner'}},
                      {'jsonrpc': '2.0', 'id': 2, 'result': {'leaves': []}}):
            with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(reply).encode())):
                with self.assertRaises(ValueError):
                    fetch_balances(TEST_OWNER)

    def test_bad_owner_prevents_network_request(self):
        with patch('urllib.request.urlopen') as send:
            with self.assertRaises(ValueError):
                fetch_balances(owner='not-an-address')
            send.assert_not_called()

    def test_withdraw_symbol_maps_to_known_operator(self):
        field = next(f for f in BY_NAME['mm_withdraw'].fields if f.name == 'token')
        self.assertEqual(cli_value(field, 'usdt'), TOKEN)


class ChainShellTests(unittest.IsolatedAsyncioTestCase):
    async def test_chain_query_works_without_websocket_or_login(self):
        shell = Shell(Client())
        result = summarize({'leaves': [leaf(1000000)]}, TEST_OWNER, DEFAULT_RPC, 1000)
        with patch('mm_desk.fetch_balances', return_value=result) as fetch:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                await shell.execute('chain-balances --owner ' + TEST_OWNER + ' --json')
        self.assertFalse(shell.client.connected)
        self.assertEqual(json.loads(output.getvalue())['balances'][0]['balance'], '1')
        fetch.assert_called_once()

    async def test_each_query_uses_its_supplied_address(self):
        shell = Shell(Client())
        queries = [('check-balance', TEST_OWNER), ('chain-balances', OTHER_OWNER),
                   ('chain-balances --owner', TEST_OWNER), ('check-balance --owner', OTHER_OWNER)]
        def respond(request, **kwargs):
            owner = json.loads(request.data)['params']['owner']
            raw = 1000000 if owner == TEST_OWNER else 2000000
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': 1,
                                          'result': {'leaves': [leaf(raw)]}}).encode())
        with patch('urllib.request.urlopen', side_effect=respond) as send:
            for command, owner in queries:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    await shell.execute(f'{command} {owner} --json')
                result = json.loads(output.getvalue())
                self.assertEqual(result['owner'], owner)
                self.assertEqual(result['balances'][0]['balance'], '1' if owner == TEST_OWNER else '2')
                self.assertEqual(json.loads(send.call_args.args[0].data)['params']['owner'], owner)
        self.assertEqual(send.call_count, 4)

    async def test_prompt_requests_an_address_each_time(self):
        shell = Shell(Client())
        with patch('mm_desk.sys.stdin.isatty', return_value=True), \
             patch('mm_desk.read_console', new_callable=AsyncMock, side_effect=[TEST_OWNER, OTHER_OWNER]) as prompt, \
             patch('mm_desk.fetch_balances', return_value={'balances': []}) as fetch, \
             contextlib.redirect_stdout(io.StringIO()):
            await shell.execute('check-balance --json')
            await shell.execute('chain-balances --json')
        self.assertEqual(prompt.await_count, 2)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [TEST_OWNER, OTHER_OWNER])

    async def test_missing_address_never_falls_back_in_noninteractive_mode(self):
        shell = Shell(Client())
        with patch('mm_desk.sys.stdin.isatty', return_value=False), patch('mm_desk.fetch_balances') as fetch:
            for command in ('check-balance', 'chain-balances --json'):
                with self.assertRaisesRegex(DeskError, 'owner address is required'):
                    await shell.execute(command)
            fetch.assert_not_called()

    async def test_conflicting_address_inputs_are_rejected(self):
        shell = Shell(Client())
        with patch('mm_desk.fetch_balances') as fetch:
            with self.assertRaises(DeskError):
                await shell.execute(f'check-balance {TEST_OWNER} --owner {OTHER_OWNER}')
            fetch.assert_not_called()

    async def test_chain_query_preserves_connected_and_authenticated_sessions(self):
        server, url, _ = await start_demo()
        client = Client(timeout=1, proxy=False)
        shell = Shell(client)
        try:
            await client.connect(url)
            reply = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'leaves': [leaf(1000000)]}}).encode()
            for authenticated in (False, True):
                if authenticated:
                    await client.login(DEMO_SEED)
                connection, session = client.ws, client.session
                with patch('urllib.request.urlopen', return_value=io.BytesIO(reply)) as send, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    await shell.execute(f'check-balance {OTHER_OWNER} --json')
                self.assertEqual(json.loads(output.getvalue())['owner'], OTHER_OWNER)
                self.assertEqual(json.loads(send.call_args.args[0].data)['params']['owner'], OTHER_OWNER)
                self.assertTrue(client.connected)
                self.assertIs(client.ws, connection)
                self.assertIs(client.session, session)
                if authenticated:
                    self.assertIsInstance(await client.call('mm_balances'), list)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()


if __name__ == '__main__':
    unittest.main()
