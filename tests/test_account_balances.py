import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from account_keys import (configured_owner, derive_key, keccak256, owner_from_chain_key,
                          owner_from_master, resolve_owner)
from chain_balances import DEFAULT_RPC, summarize
from demo_server import DEMO_SEED, start_demo
from mm_desk import Client, DeskError, Shell, public_hex

MASTER = bytes(range(32))  # Public fixture only.
SEED = derive_key(MASTER, b'desk/ed25519')
OWNER = '0x' + '11' * 20
OTHER = '0x' + '22' * 20


class AccountKeyTests(unittest.TestCase):
    def test_public_mapping_scoped_to_endpoint_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.json'
            path.write_text(json.dumps({'wss://desk.example': {public_hex(SEED): OWNER}}))
            self.assertEqual(configured_owner('wss://desk.example/', public_hex(SEED).upper(), path),
                             (OWNER, 'accounts.json public-key mapping'))
            self.assertEqual(configured_owner('wss://other.example', public_hex(SEED), path), (None, None))
            self.assertEqual(configured_owner('wss://desk.example/path', public_hex(SEED), path), (None, None))
            self.assertEqual(configured_owner('ws://desk.example', public_hex(SEED), path), (None, None))
            self.assertEqual(configured_owner('wss://desk.example', public_hex(DEMO_SEED), path), (None, None))

    def test_public_mapping_rejects_invalid_or_ambiguous_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.json'
            for data in [[], {'https://desk.example': {}}, {'wss://desk.example': []},
                         {'wss://desk.example': {'invalid-key': OWNER}},
                         {'wss://desk.example': {public_hex(SEED): 'invalid-owner'}},
                         {'wss://desk.example': {}, 'wss://desk.example/': {}},
                         {'wss://user:secret@desk.example': {}}]:
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    configured_owner('wss://desk.example', public_hex(SEED), path)

    def test_owner_vectors_match_mm_keys_reference(self):
        self.assertEqual(owner_from_chain_key((1).to_bytes(32, 'big')),
                         '0x4ad54db109f94b7bc544978d36173821c1215e40')
        self.assertEqual(owner_from_master(MASTER),
                         '0x6f43ea79db653390c0c8b4404d4cc8aa112a4f7d')

    def test_keccak_ethereum_vector(self):
        self.assertEqual(keccak256(b'').hex(),
                         'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470')

    def test_matching_sibling_and_mismatched_master(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.desk.seed'
            master = path.with_name('fixture.master.seed')
            self.assertEqual(resolve_owner(SEED, seed_file=path), (None, None))
            master.write_text(MASTER.hex())
            owner, source = resolve_owner(SEED, seed_file=path)
            self.assertEqual(owner, owner_from_master(MASTER))
            self.assertEqual(source, 'matching master seed')
            self.assertEqual(resolve_owner(SEED, master_file=master), (owner, source))
            with self.assertRaisesRegex(ValueError, 'does not match'):
                resolve_owner(DEMO_SEED, seed_file=path)
            self.assertEqual(resolve_owner(DEMO_SEED, seed_file=path, owner=OTHER),
                             (OTHER, 'explicit login owner'))

    def test_explicit_owner_validated_and_no_desk_only_derivation(self):
        self.assertEqual(resolve_owner(SEED), (None, None))
        self.assertEqual(resolve_owner(SEED, owner='0X' + 'AB' * 20)[0], '0x' + 'ab' * 20)
        with self.assertRaisesRegex(ValueError, '20-byte address'):
            resolve_owner(SEED, owner='not-an-address')


class AccountBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.profile = Path(directory.name) / 'accounts.json'
        profile_patch = patch('account_keys.ACCOUNTS_PATH', self.profile)
        profile_patch.start()
        self.addCleanup(profile_patch.stop)
        self.server, self.url, self.desk = await start_demo()
        self.client = Client(timeout=1, proxy=False)
        self.shell = Shell(self.client)
        await self.client.connect(self.url)
        await self.client.login(DEMO_SEED, OWNER, 'explicit login owner')
        self.chain = summarize({'leaves': []}, OWNER, DEFAULT_RPC, 1000)

    def configure_owner(self, seed=DEMO_SEED, owner=OWNER):
        self.profile.write_text(json.dumps({self.url: {public_hex(seed): owner}}))

    async def test_pasted_seed_login_then_no_address_query(self):
        self.configure_owner()
        with patch('mm_desk.sys.stdin.isatty', return_value=True), \
             patch('mm_desk.read_console', new_callable=AsyncMock, return_value=DEMO_SEED.hex()) as prompt, \
             patch('mm_desk.fetch_balances', return_value=self.chain) as fetch:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                await self.shell.execute('login')
            login = json.loads(output.getvalue())
            self.assertEqual(login['chain_owner'], OWNER)
            self.assertEqual(login['owner_source'], 'accounts.json public-key mapping')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                await self.shell.execute('check-balance --json')
            prompt.assert_awaited_once_with('Desk seed (input hidden): ', secret=True)
            fetch.assert_called_once_with(OWNER, DEFAULT_RPC, 1000, 1)
            result = json.loads(output.getvalue())
            self.assertIn('balances', result['desk'])
            self.assertEqual(result['chain']['owner'], OWNER)

    async def test_raw_login_uses_public_mapping(self):
        self.configure_owner()
        for raw in (False, True):
            challenge = await self.client.call('mm_challenge')
            signature = Ed25519PrivateKey.from_private_bytes(DEMO_SEED).sign(
                b'mm_pool_desk/login:' + bytes.fromhex(challenge['challenge'])).hex()
            command = ('rpc mm_login ' + json.dumps({'key_id': public_hex(DEMO_SEED), 'signature': signature})
                       if raw else f'login-raw --key-id {public_hex(DEMO_SEED)} --signature {signature}')
            with contextlib.redirect_stdout(io.StringIO()):
                await self.shell.execute(command)
            self.assertEqual(self.client.chain_owner, OWNER)

    async def test_mapping_never_carries_over_to_failed_or_other_key_login(self):
        self.configure_owner()
        await self.client.login(DEMO_SEED)
        self.assertEqual(self.client.chain_owner, OWNER)
        with self.assertRaises(DeskError):
            await self.client.call('mm_login', {'key_id': public_hex(DEMO_SEED), 'signature': '00' * 64})
        self.assertIsNone(self.client.chain_owner)
        self.desk.public = public_hex(SEED)
        await self.client.login(SEED)
        self.assertIsNone(self.client.chain_owner)

    async def asyncTearDown(self):
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_both_default_without_prompt_in_interactive_and_command_modes(self):
        connection, session = self.client.ws, self.client.session
        for tty, command in ((True, 'check-balance'), (False, 'chain-balances')):
            with patch('mm_desk.sys.stdin.isatty', return_value=tty), \
                 patch('mm_desk.read_console') as prompt, \
                 patch('mm_desk.fetch_balances', return_value=self.chain) as fetch, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                await self.shell.execute(command + ' --json')
            result = json.loads(output.getvalue())
            self.assertEqual(result['mm_id'], 1)
            self.assertEqual(result['desk']['source'], 'mm_balances')
            self.assertIsInstance(result['desk']['balances'], list)
            self.assertEqual(result['chain']['owner'], OWNER)
            fetch.assert_called_once_with(OWNER, DEFAULT_RPC, 1000, 1)
            prompt.assert_not_called()
            self.assertIs(self.client.session, session)
            self.assertIs(self.client.ws, connection)

    async def test_explicit_address_does_not_replace_login_owner(self):
        with patch('mm_desk.fetch_balances', return_value=self.chain) as fetch, \
             contextlib.redirect_stdout(io.StringIO()):
            await self.shell.execute('check-balance ' + OTHER + ' --json')
            await self.shell.execute('check-balance --json')
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [OTHER, OWNER])
        self.assertEqual(self.client.chain_owner, OWNER)

    async def test_one_source_failure_preserves_other_source(self):
        with patch('mm_desk.fetch_balances', side_effect=OSError('chain unavailable')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(DeskError, 'incomplete'):
                await self.shell.execute('check-balance --json')
        result = json.loads(output.getvalue())
        self.assertIn('balances', result['desk'])
        self.assertEqual(result['chain']['error'], 'chain unavailable')
        with patch.object(self.client, 'call', side_effect=DeskError('desk unavailable')), \
             patch('mm_desk.fetch_balances', return_value=self.chain), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(DeskError):
                await self.shell.execute('check-balance --json')
        result = json.loads(output.getvalue())
        self.assertEqual(result['desk']['error'], 'desk unavailable')
        self.assertEqual(result['chain']['owner'], OWNER)

    async def test_missing_owner_still_displays_desk_without_guessing(self):
        await self.client.login(DEMO_SEED)
        with patch('mm_desk.fetch_balances') as fetch, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(DeskError):
                await self.shell.execute('check-balance --json')
        result = json.loads(output.getvalue())
        self.assertIn('balances', result['desk'])
        self.assertIsNone(result['chain']['owner'])
        self.assertIn('Chain owner unavailable', result['chain']['error'])
        fetch.assert_not_called()

    async def test_owner_is_cleared_on_relogin_failure_raw_login_and_disconnect(self):
        await self.client.login(DEMO_SEED, OTHER)
        self.assertEqual(self.client.chain_owner, OTHER)
        with self.assertRaises(DeskError):
            await self.client.login(bytes([9]) * 32, OWNER)
        self.assertIsNone(self.client.chain_owner)
        self.assertIsNone(self.client.session)
        await self.client.login(DEMO_SEED, OWNER)
        with self.assertRaises(DeskError):
            await self.client.call('mm_login', {'key_id': public_hex(DEMO_SEED), 'signature': '00' * 64})
        self.assertIsNone(self.client.chain_owner)
        await self.client.login(DEMO_SEED, OWNER)
        await self.client.close()
        self.assertIsNone(self.client.chain_owner)
        self.assertIsNone(self.client.owner_source)

    async def test_master_and_sibling_login_bind_owner(self):
        self.desk.public = public_hex(SEED)
        self.configure_owner(SEED, OTHER)
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / 'fixture.master.seed'
            seed = Path(directory) / 'fixture.desk.seed'
            master.write_text(MASTER.hex())
            seed.write_text(SEED.hex())
            for option, path in (('--master-file', master), ('--seed-file', seed)):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    await self.shell.execute(f'login {option} {path}')
                self.assertEqual(self.client.chain_owner, owner_from_master(MASTER))
                self.assertEqual(json.loads(output.getvalue())['owner_source'], 'matching master seed')
            with contextlib.redirect_stdout(io.StringIO()):
                await self.shell.execute(f'login --seed-file {seed} --owner {OTHER}')
            self.assertEqual(self.client.chain_owner, OTHER)


if __name__ == '__main__':
    unittest.main()
