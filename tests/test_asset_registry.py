import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_registry import ASSETS, TOKENS, asset_label, format_asset_text, format_asset_values, load_assets
from chain_balances import print_balances
from mm_desk import RpcError, Shell

USDT = '0xaca71f1748aa4c3291bbccd905b71224cb1b602f'
BTC = '0x5aa2fbdff776e63a5176dacbe1d0e098369566ea'
LABEL = f'USDT（{USDT}）'


class AssetDisplayTests(unittest.TestCase):
    def test_user_reported_business_rejection(self):
        result = {'code': 'insufficient_balance',
                  'detail': f'needs 6000000 of {USDT}, available 0', 'status': 'rejected'}
        original = copy.deepcopy(result)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(Shell.display(result))
        header, body = output.getvalue().split('\n', 1)
        self.assertIn('Business rejection:', header)
        self.assertEqual(json.loads(body)['detail'], f'needs 6000000 of {LABEL}, available 0')
        self.assertEqual(result, original)

    def test_nested_fields_dictionary_keys_and_symbol_fields(self):
        value = {'rows': [{'token': USDT, 'amount': '6000000'}, {'operator': BTC}],
                 'by_token': {USDT: {'token': 'usdt'}}, 'owner': '0x' + '11' * 20}
        original = copy.deepcopy(value)
        displayed = format_asset_values(value)
        self.assertEqual(displayed['rows'][0]['token'], LABEL)
        self.assertEqual(displayed['rows'][1]['operator'], f'BTC（{BTC}）')
        self.assertEqual(displayed['by_token'][LABEL]['token'], LABEL)
        self.assertEqual(displayed['owner'], value['owner'])
        self.assertEqual(value, original)

    def test_case_insensitive_lookup_and_idempotent_formatting(self):
        address = '0x' + USDT[2:].upper()
        self.assertEqual(asset_label(address), f'USDT（{address}）')
        message = f'{USDT}; {BTC}; {USDT}'
        displayed = format_asset_text(message)
        self.assertEqual(format_asset_text(displayed), displayed)
        self.assertEqual(displayed.count('USDT（'), 2)

    def test_unknown_addresses_and_long_hex_values_are_unchanged(self):
        for text in ['0x' + '11' * 20, USDT + 'ab' * 12, 'prefix_' + USDT, USDT + '_suffix']:
            self.assertEqual(format_asset_text(text), text)

    def test_rpc_error_display_keeps_original_error_data(self):
        data = {'code': -32010, 'message': f'Failed for {USDT}', 'data': {'token': BTC}}
        original = copy.deepcopy(data)
        error = RpcError(data)
        self.assertIn(LABEL, str(error))
        self.assertIn(f'BTC（{BTC}）', str(error))
        self.assertIs(error.error, data)
        self.assertEqual(data, original)

    def test_chain_table_uses_name_and_complete_address(self):
        row = {'symbol': 'USDT', 'token': USDT, 'raw': '6000000', 'balance': '6', 'decimals': 6}
        result = {'owner': '0x' + '11' * 20, 'rpc_url': 'https://example.invalid',
                  'balances': [row], 'leaf_count': 1, 'balance_leaf_count': 1, 'notes': []}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            print_balances(result)
        self.assertIn(LABEL, output.getvalue())
        self.assertIn('6000000', output.getvalue())
        self.assertEqual(row['token'], USDT)

    def test_registry_file_drives_all_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'assets.json'
            path.write_text(json.dumps({USDT.upper(): {'name': 'Tether USD', 'decimals': 6}}))
            self.assertEqual(load_assets(path), {USDT: ('Tether USD', 6)})
        self.assertEqual(ASSETS[USDT], ('USDT', 6))
        self.assertEqual(TOKENS['USDT'], USDT)

    def test_invalid_metadata_fails_without_silent_fallback(self):
        invalid = [{USDT: {'name': '', 'decimals': 6}},
                   {USDT: {'name': 'USDT', 'decimals': True}},
                   {USDT: {'name': 'USDT', 'decimals': -1}},
                   {'invalid-address': {'name': 'USDT', 'decimals': 6}},
                   {USDT: {'name': 'USDT', 'decimals': 6}, BTC: {'name': 'usdt', 'decimals': 8}}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'assets.json'
            for data in invalid:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        load_assets(path)


class AssetRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_display_does_not_change_withdrawal_parameters_or_response(self):
        response = {'status': 'accepted', 'token': USDT, 'amount': '1'}
        client = type('ClientFixture', (), {})()
        client.call = AsyncMock(return_value=response)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            await Shell(client).execute('withdraw --token USDT --amount 1 --to 0x' + '11' * 20)
        method, params = client.call.call_args.args
        self.assertEqual(method, 'mm_withdraw')
        self.assertEqual(params['token'], USDT)
        self.assertEqual(params['to'], '0x' + '11' * 20)
        self.assertEqual(response['token'], USDT)
        self.assertEqual(json.loads(output.getvalue())['token'], LABEL)


if __name__ == '__main__':
    unittest.main()
