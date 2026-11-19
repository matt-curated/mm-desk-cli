#!/usr/bin/env python3
"""Interactive mm_pool_desk JSON-RPC client. Python 3.9+. No secrets are persisted."""
import argparse
import asyncio
import contextlib
import getpass
import json
import math
import os
import re
import shlex
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
try:
    from asset_registry import TOKENS, format_asset_text, format_asset_values
    from chain_balances import DEFAULT_RPC, fetch_balances, print_balances
except (OSError, ValueError) as exc:
    sys.exit('Error loading assets.json: ' + str(exc))

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import WebSocketException
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from account_keys import configured_owner, derive_key, read_seed, resolve_owner, validate_owner
    from desk_transfers import transfer_parser, transfer as desk_transfer
except ImportError:
    sys.exit('Missing dependencies. Install them with the current Python interpreter:\n'
             + shlex.quote(sys.executable) + ' -m pip install -r '
             + shlex.quote(str(Path(__file__).with_name('requirements.txt'))))

VERSION = '1.0.0'
DEFAULT_URL = 'wss://desk.testnet.curated.finance'
DOMAIN = b'mm_pool_desk/login:'
ERROR_NAMES = {-32001: 'unauthorized', -32002: 'stale_timestamp',
               -32003: 'rate_limited', -32004: 'not_implemented',
               -32005: 'not_ready', -32010: 'internal', -32011: 'not_found'}


class DeskError(Exception):
    pass


class RpcError(DeskError):
    def __init__(self, error):
        self.error = error
        self.code = error.get('code') if isinstance(error, dict) else None
        label = ERROR_NAMES.get(self.code, 'rpc_error')
        super().__init__(f'{label}: {json.dumps(format_asset_values(error), ensure_ascii=False)}')


class HelpShown(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DeskError(f'{message}\nUse help {self.prog} to view the options.')

    def exit(self, status=0, message=None):
        if status:
            raise DeskError(message or 'Invalid arguments')
        raise HelpShown()


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    required: bool


@dataclass(frozen=True)
class Method:
    alias: str
    name: str
    scope: str
    description: str
    fields: tuple
    mutating: bool = False


def method(alias, name, scope, description, spec='', mutating=False):
    fields = []
    for item in spec.split():
        name_part, kind = item.split(':')
        fields.append(Field(name_part.rstrip('?'), kind, not name_part.endswith('?')))
    return Method(alias, name, scope, description, tuple(fields), mutating)


ORDER = 'book:str side:buy|sell price:amount quantity:amount tif:gtc|ioc post_only?:bool ts?:uint'
HISTORY = 'book?:str from?:uint to?:uint state?:str limit?:uint offset?:uint'
METHODS = [
    method('challenge', 'mm_challenge', 'none', 'Get a login challenge'),
    method('login-raw', 'mm_login', 'none', 'Authenticate with a public key and signature (normally use login)',
           'key_id:hex32 signature:hex64'),
    method('deadman', 'mm_setDeadman', 'trade', 'Set the deadman timeout; 0 disables it', 'timeout_ms:uint', True),
    method('gateway-token', 'mm_gatewayToken', 'any', 'Get a market-data gateway JWT'),
    method('place', 'mm_placeOrder', 'trade', 'Place an order; price and quantity are raw on-chain integers', ORDER, True),
    method('batch', 'mm_placeOrders', 'trade', 'Place 1-50 orders; --orders @file.json', 'orders:orders', True),
    method('modify', 'mm_modifyOrder', 'trade', 'Modify an order; quantity is the new total, remaining is the new remainder',
           'order_id:id price?:amount quantity?:amount remaining?:nonnegative ts?:uint', True),
    method('cancel', 'mm_cancelOrder', 'trade', 'Cancel one order', 'order_id:id ts?:uint', True),
    method('cancel-all', 'mm_cancelAll', 'trade', 'Cancel all orders, optionally limited to one book', 'book?:str ts?:uint', True),
    method('open-orders', 'mm_openOrders', 'any', 'List live orders', 'book?:str'),
    method('order', 'mm_getOrder', 'any', 'Get an order and its fills', 'order_id:id'),
    method('orders', 'mm_listOrders', 'any', 'List order history; from is inclusive and to exclusive, in Unix milliseconds', HISTORY),
    method('request', 'mm_getRequest', 'any', 'Get a request result', 'pool_request_id:id'),
    method('requests', 'mm_listRequests', 'any', 'List request history', HISTORY),
    method('balances', 'mm_balances', 'any', 'Get desk balances'),
    method('deposits', 'mm_deposits', 'any', 'List deposit records'),
    method('withdraw', 'mm_withdraw', 'withdraw', 'Withdraw to your own Cura account address',
           'token:token amount:amount to:str ts?:uint', True),
    method('withdrawals', 'mm_withdrawals', 'any', 'List withdrawal records'),
    method('algo-place', 'mm_placeAlgo', 'trade', 'Create a TWAP / VWAP schedule; times are Unix milliseconds',
           'book:str side:buy|sell quantity:amount limit_price:amount start?:uint end:uint interval_ms:uint kind:twap|vwap ts?:uint', True),
    method('algo-cancel', 'mm_cancelAlgo', 'trade', 'Cancel an algorithmic order', 'parent_id:id ts?:uint', True),
    method('algo-pause', 'mm_pauseAlgo', 'trade', 'Pause an algorithmic order', 'parent_id:id ts?:uint', True),
    method('algo-resume', 'mm_resumeAlgo', 'trade', 'Resume an algorithmic order', 'parent_id:id ts?:uint', True),
    method('algo', 'mm_getAlgo', 'any', 'Get an algorithmic order and its slices', 'parent_id:id'),
]
BY_NAME = {m.name: m for m in METHODS}
BY_COMMAND = {key: m for m in METHODS for key in (m.name, m.alias)}
TIMED_METHODS = {'mm_placeOrder', 'mm_placeOrders', 'mm_modifyOrder', 'mm_cancelOrder',
                 'mm_cancelAll', 'mm_placeAlgo', 'mm_cancelAlgo'}


def default_latency_log():
    state = Path(os.environ.get('XDG_STATE_HOME', ''))
    if not state.is_absolute():
        state = Path.home() / '.local' / 'state'
    return state / 'mm-desk' / 'latency.jsonl'


class LatencyLog:
    """Append request metadata only; never serialize credentials or full payloads."""
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.run_id = str(uuid4())

    def record(self, event):
        event = dict(event, run_id=self.run_id)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n')
        except OSError as exc:
            # A logging failure must not change a submitted order's result or cause retries.
            with contextlib.suppress(OSError):
                print(f'Warning: cannot write latency log {self.path}: {exc}', file=sys.stderr)
        rtt = event['rtt_ms']
        timing = f'{rtt:.3f} ms' if rtt is not None else f'unavailable (waited {event["elapsed_ms"]:.3f} ms)'
        with contextlib.suppress(OSError):
            print(f'TTL (RTT): {timing} | {BY_NAME[event["method"]].alias} | '
                  f'request={event["request_id"]} | {event["outcome"]}', file=sys.stderr, flush=True)


def timing_result(result):
    rows = result if isinstance(result, list) else [result]
    statuses = {row.get('status') for row in rows
                if isinstance(row, dict) and isinstance(row.get('status'), str)}
    if 'rejected' in statuses:
        outcome = 'rejected' if statuses == {'rejected'} else 'mixed'
    else:
        outcome = 'accepted' if statuses == {'accepted'} else 'success'
    # Only public order/request references and outcome codes are persisted.
    refs = [{key: row[key] for key in ('order_id', 'pool_request_id', 'parent_id', 'status', 'code')
             if key in row} for row in rows if isinstance(row, dict)]
    return outcome, refs


def now_ms():
    return time.time_ns() // 1_000_000


def hex_bytes(value, length, label):
    if not isinstance(value, str):
        raise DeskError(f'{label} must be a hexadecimal string')
    value = value.strip()
    if value.startswith(('0x', '0X')):
        value = value[2:]
    if not re.fullmatch(r'[0-9a-fA-F]{%d}' % (length * 2), value):
        raise DeskError(f'{label} must be {length} bytes ({length * 2} hexadecimal characters)')
    return bytes.fromhex(value)


def load_seed(filename, master=False):
    seed = read_seed(filename)
    return derive_key(seed, b'desk/ed25519') if master else seed


def public_hex(seed):
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def check_field(field, value):
    kind, name = field.kind, field.name
    if kind in ('amount', 'nonnegative'):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
            raise DeskError(f'{name} must be a decimal string of a raw on-chain integer; fractions and floats are not accepted')
        if kind == 'amount' and int(value) <= 0:
            raise DeskError(f'{name} must be greater than 0')
    elif kind == 'uint':
        if type(value) is not int or value < 0:
            raise DeskError(f'{name} must be a nonnegative integer')
    elif kind == 'bool':
        if type(value) is not bool:
            raise DeskError(f'{name} must be true or false')
    elif kind == 'id':
        if not ((type(value) is int and value >= 0) or (isinstance(value, str) and value.strip())):
            raise DeskError(f'{name} must be an integer ID or a nonempty string ID')
    elif kind.startswith('hex'):
        hex_bytes(value, int(kind[3:]), name)
    elif '|' in kind:
        if value not in kind.split('|'):
            raise DeskError(f'{name} must be one of {kind}')
    elif kind in ('str', 'token'):
        if not isinstance(value, str) or not value.strip():
            raise DeskError(f'{name} must be a nonempty string')
    return value


def prepare_params(name, params):
    """Validate known schemas, preserving explicit JSON ID types and integer precision."""
    if not isinstance(params, dict):
        raise DeskError('params must be a JSON object')
    params = dict(params)
    spec = BY_NAME.get(name)
    if spec is None:
        return params
    allowed = {f.name for f in spec.fields}
    extra = params.keys() - allowed
    if extra:
        raise DeskError('Unknown parameters: ' + ', '.join(sorted(extra)))
    if 'ts' in allowed and 'ts' not in params:
        params['ts'] = now_ms()
    for field in spec.fields:
        if field.name not in params:
            if field.required:
                raise DeskError(f'Missing parameter: {field.name}')
            continue
        check_field(field, params[field.name])
    if name == 'mm_placeOrders':
        orders = params['orders']
        if not isinstance(orders, list) or not 1 <= len(orders) <= 50:
            raise DeskError('orders must be an array of 1-50 orders')
        params['orders'] = [prepare_params('mm_placeOrder', order) for order in orders]
    if name == 'mm_modifyOrder':
        if 'quantity' in params and 'remaining' in params:
            raise DeskError('quantity (new total) and remaining (new remainder) cannot be used together')
        if not any(k in params for k in ('price', 'quantity', 'remaining')):
            raise DeskError('An order modification requires at least one of price, quantity, or remaining')
    if name in ('mm_listOrders', 'mm_listRequests'):
        if 'from' in params and 'to' in params and params['from'] >= params['to']:
            raise DeskError('to must be greater than from (the interval is [from, to))')
    if name == 'mm_placeAlgo':
        if params['interval_ms'] <= 0:
            raise DeskError('interval_ms must be greater than 0')
        if params['end'] <= params.get('start', params['ts']):
            raise DeskError('end must be later than start (or ts when start is omitted)')
    return params


def json_text(text):
    if text.startswith('@'):
        text = Path(text[1:]).expanduser().read_text(encoding='utf-8')
    return json.loads(text)


def cli_value(field, text):
    if field.kind == 'token':
        return TOKENS.get(text.upper(), text)
    if field.kind == 'uint':
        if not re.fullmatch(r'[0-9]+', text):
            raise DeskError(f'{field.name} must be a nonnegative integer')
        return int(text)
    if field.kind == 'id':
        return int(text) if re.fullmatch(r'0|[1-9][0-9]*', text) else text
    if field.kind == 'orders':
        value = json_text(text)
        return value.get('orders', value) if isinstance(value, dict) else value
    return text


def method_parser(spec):
    p = Parser(prog=spec.alias, description=spec.description)
    for field in spec.fields:
        flag = '--' + field.name.replace('_', '-')
        kw = dict(dest=field.name, required=field.required, default=None)
        if field.kind == 'bool':
            p.add_argument(flag, action=argparse.BooleanOptionalAction, **kw)
        else:
            p.add_argument(flag, metavar=field.kind.upper(),
                           type=lambda value, f=field: cli_value(f, value), **kw)
    return p


def safe_url(url):
    if not url:
        return None
    p = urlsplit(url)
    return p.scheme + '://' + p.netloc.rsplit('@', 1)[-1] + p.path


async def read_console(prompt, secret=False):
    # A daemon input thread lets Ctrl-C exit while the terminal is waiting for a line.
    # asyncio.to_thread would make asyncio.run wait for a blocked executor at exit.
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    def deliver(value, error):
        if not future.done():
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)
    def read():
        try:
            value, error = (getpass.getpass(prompt) if secret else input(prompt)), None
        except BaseException as exc:
            value, error = None, exc
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(deliver, value, error)
    threading.Thread(target=read, daemon=True).start()
    return await future


class Client:
    def __init__(self, timeout=15.0, proxy=True, latency_log=None):
        self.timeout = timeout
        self.proxy = proxy
        self.ws = None
        self.reader = None
        self.url = None
        self.session = None
        self.chain_owner = None
        self.owner_source = None
        self.pending = {}
        self.sequence = 0
        self.ignored = 0
        self.latency_log = latency_log
        self.response_times = {}

    @property
    def connected(self):
        return self.ws is not None and self.reader is not None and not self.reader.done()

    def clear_session(self):
        self.session = None
        self.chain_owner = None
        self.owner_source = None

    async def connect(self, url):
        parsed = urlsplit(url)
        if parsed.scheme not in ('ws', 'wss') or not parsed.hostname or parsed.fragment:
            raise DeskError('The address must be a valid ws:// or wss:// URL without a fragment')
        await self.close()
        self.ws = await ws_connect(url, ping_interval=15, ping_timeout=20,
                                   open_timeout=self.timeout, close_timeout=3,
                                   max_size=16 * 1024 * 1024,
                                   proxy=True if self.proxy else None)
        self.url = url
        self.clear_session()
        self.reader = asyncio.create_task(self._receive())

    async def _receive(self):
        failure = DeskError('WebSocket disconnected. Use connect, then login again.')
        try:
            async for data in self.ws:
                received_ns = time.perf_counter_ns()
                try:
                    response = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    raise DeskError('The server returned invalid JSON')
                if not isinstance(response, dict):
                    raise DeskError('The server returned a JSON-RPC message that is not an object')
                rid = response.get('id')
                # Do not let bool or 1.0 match integer request ID 1.
                future = self.pending.get(rid) if type(rid) is int else None
                if future is None or future.done():
                    self.ignored += 1
                    continue
                if rid in self.response_times:
                    self.response_times[rid] = received_ns
                if response.get('jsonrpc') != '2.0' or (('error' in response) == ('result' in response)):
                    future.set_exception(DeskError('The server returned an invalid JSON-RPC response'))
                elif 'error' in response:
                    future.set_exception(RpcError(response['error']))
                else:
                    future.set_result(response['result'])
        except asyncio.CancelledError:
            raise
        except DeskError as exc:
            failure = exc
        except Exception as exc:
            failure = DeskError(f'WebSocket connection lost ({type(exc).__name__}). Reconnect and log in again.')
        finally:
            self.clear_session()
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(failure)

    async def close(self):
        self.clear_session()
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader
        self.ws = None
        self.reader = None

    async def call(self, name, params=None):
        params = prepare_params(name, {} if params is None else params)
        if not self.connected:
            raise DeskError('Not connected. Use connect ws://... or connect wss://... first.')
        spec = BY_NAME.get(name)
        if name not in ('mm_challenge', 'mm_login'):
            if self.session is None:
                raise DeskError('Not logged in. Use login --seed-file PATH first.')
            if spec and spec.scope not in ('none', 'any') and spec.scope not in self.session.get('scopes', []):
                raise DeskError(f'The current key lacks the {spec.scope} scope')
        if name == 'mm_login':
            self.clear_session()
            owner, owner_source = configured_owner(self.url, params['key_id'])
        self.sequence += 1
        rid = self.sequence
        payload = json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': name, 'params': params},
                             ensure_ascii=False)
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        timed = self.latency_log is not None and name in TIMED_METHODS
        if timed:
            self.response_times[rid] = None
            event = dict(timestamp=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                         endpoint=safe_url(self.url), mm_id=self.session.get('mm_id'),
                         request_id=rid, method=name,
                         request={k: params[k] for k in ('book', 'order_id', 'parent_id') if k in params})
            if name == 'mm_placeOrders':
                event['request']['count'] = len(params['orders'])
        outcome, refs, error_code = 'connection_error', [], None
        sent = False
        started_ns = time.perf_counter_ns()
        try:
            # Mark potentially sent before send(): cancellation can occur after bytes leave.
            sent = True
            await asyncio.wait_for(self.ws.send(payload), timeout=self.timeout)
            result = await asyncio.wait_for(future, timeout=self.timeout)
            if timed:
                outcome, refs = timing_result(result)
            if name == 'mm_login':
                if not isinstance(result, dict) or 'mm_id' not in result or not isinstance(result.get('scopes'), list):
                    raise DeskError('The login response is missing mm_id or scopes')
                self.session = result
                self.chain_owner, self.owner_source = owner, owner_source
            return result
        except RpcError as exc:
            outcome, error_code = 'rpc_error', exc.code
            if exc.code == -32001:
                self.clear_session()
            raise
        except (asyncio.TimeoutError, asyncio.CancelledError, DeskError) as exc:
            outcome = ('timeout' if isinstance(exc, asyncio.TimeoutError) else
                       'cancelled' if isinstance(exc, asyncio.CancelledError) else
                       'protocol_error' if self.response_times.get(rid) is not None else 'connection_error')
            if sent and (spec is None or spec.mutating):
                raise DeskError(f'Execution status unknown for {name} request {rid}; no automatic retry. '
                                'Check orders / requests / withdrawals / algo before deciding what to do next.') from exc
            if isinstance(exc, asyncio.TimeoutError):
                raise DeskError(f'{name} request {rid} timed out; no automatic retry.') from exc
            raise
        except Exception as exc:
            suffix = 'Execution status unknown; check order/request records first.' if spec is None or spec.mutating else 'Please reconnect.'
            raise DeskError(f'{name} connection error ({type(exc).__name__}); no automatic retry. {suffix}') from exc
        finally:
            finished_ns = time.perf_counter_ns()
            received_ns = self.response_times.pop(rid, None)
            self.pending.pop(rid, None)
            if not future.done():
                future.cancel()
            if timed:
                event.update(outcome=outcome, results=refs,
                             rtt_ms=round((received_ns - started_ns) / 1_000_000, 6) if received_ns is not None else None,
                             elapsed_ms=round(((received_ns if received_ns is not None else finished_ns) - started_ns) / 1_000_000, 6))
                if error_code is not None:
                    event['rpc_error_code'] = error_code
                await asyncio.to_thread(self.latency_log.record, event)

    async def login(self, seed, owner=None, owner_source=None):
        self.clear_session()
        owner = validate_owner(owner) if owner is not None else None
        challenge = await self.call('mm_challenge')
        if not isinstance(challenge, dict) or not isinstance(challenge.get('challenge'), str):
            raise DeskError('Invalid challenge response format')
        raw = challenge['challenge']
        if raw.startswith('0x'):
            raw = raw[2:]
        if not raw or len(raw) % 2 or not re.fullmatch('[0-9a-fA-F]+', raw):
            raise DeskError('challenge is not a valid hexadecimal string')
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        signature = private_key.sign(DOMAIN + bytes.fromhex(raw)).hex()
        result = await self.call('mm_login', {'key_id': public_hex(seed), 'signature': signature})
        if self.session is not None and owner is not None:
            self.chain_owner = owner
            self.owner_source = owner_source
        return result


class Shell:
    def __init__(self, client, demo=False):
        self.client = client
        self.demo = demo
        self.running = True

    def help(self, command=None):
        if command == 'transfer':
            transfer_parser(Parser).print_help()
            return
        if command in ('chain-balances', 'check-balance'):
            self.chain_parser(command).print_help()
            return
        if command in BY_COMMAND:
            method_parser(BY_COMMAND[command]).print_help()
            return
        if command in ('login', 'pubkey'):
            self.seed_parser(command).print_help()
            return
        if command == 'connect':
            print('connect ws://host:port/path or connect wss://host/path; omit the URL to reconnect to the previous address.')
            return
        if command == 'rpc':
            print('rpc METHOD {JSON object} or rpc METHOD @params.json\n'
                  'Known methods validate parameters and fill ts; unknown methods are sent as provided. No automatic retries.')
            return
        if command == 'manual':
            print('Usage: manual\nOpen mm-desk(1) using the system man viewer and pager.\n'
                  'Use help COMMAND for command options.')
            return
        if command:
            raise DeskError('Unknown help topic: ' + command)
        print('\nMM Desk CLI ' + VERSION + ('  [local demo; no connection to a live desk]' if self.demo else ''))
        print('Local commands: connect [URL] | disconnect | login | pubkey | status | now | help [COMMAND] | manual | quit')
        print('Chain query: check-balance [ADDRESS] (alias: chain-balances); --owner ADDRESS is also accepted.')
        print('  With an address: query that chain owner in any connection state.')
        print('  Without an address: show both balances for the current login; prompt for an address before login.')
        print('Transfers: transfer to-desk | transfer from-desk; use --help for options.')
        print('Order requests print TTL (RTT) in milliseconds and append a JSONL latency log.')
        print('API commands (full mm_* method names are also accepted):')
        for spec in METHODS:
            print(f'  {spec.alias:16s} {spec.name:20s} {spec.description}')
        print('  rpc METHOD JSON / @file.json    Raw JSON call')
        print('\nUse help COMMAND for options; manual opens mm-desk(1).')

    @staticmethod
    def chain_parser(command='chain-balances'):
        parser = Parser(prog=command, description='With an address, query that chain owner in any connection state. Without an address after login, show both desk and linked chain balances.')
        address = parser.add_mutually_exclusive_group()
        address.add_argument('address', nargs='?', metavar='ADDRESS', help='Chain owner address; omit for both balances of the current login, or an address prompt before login')
        address.add_argument('--owner', metavar='ADDRESS', help='Alternative to the positional address')
        parser.add_argument('--rpc-url', default=DEFAULT_RPC, help='HTTP JSON-RPC endpoint')
        parser.add_argument('--limit', type=int, default=1000, help='Maximum leaves per request, default 1000; no automatic pagination')
        parser.add_argument('--json', action='store_true', help='Print JSON containing raw, decimals, and balance')
        return parser

    @staticmethod
    def seed_parser(command):
        parser = Parser(prog=command, description='Files are read locally; without a file option, enter the desk seed with input hidden.')
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--seed-file', help='Hexadecimal text file containing a 32-byte Ed25519 desk seed')
        group.add_argument('--master-file', help='mm-keys.py master seed; derive the desk seed with the same HKDF')
        if command == 'login':
            parser.add_argument('--owner', help='Chain owner for this login; otherwise use a matching master or the accounts.json public-key mapping')
        return parser

    async def get_seed(self, command, args):
        opts = self.seed_parser(command).parse_args(args)
        if opts.seed_file or opts.master_file:
            return load_seed(opts.seed_file or opts.master_file, bool(opts.master_file)), opts
        if not sys.stdin.isatty():
            raise DeskError('Non-interactive mode requires --seed-file or --master-file')
        value = await read_console('Desk seed (input hidden): ', secret=True)
        return hex_bytes(value, 32, 'desk seed'), opts

    async def login(self, seed, opts):
        self.client.clear_session()
        owner, source = resolve_owner(seed, opts.seed_file, opts.master_file, opts.owner)
        result = await self.client.login(seed, owner, source)
        return self.display(dict(result, chain_owner=self.client.chain_owner,
                                 owner_source=self.client.owner_source))

    async def account_balances(self, opts):
        # Capture the account before either endpoint can invalidate the session.
        mm_id, owner = self.client.session['mm_id'], self.client.chain_owner
        async def chain_query():
            if owner is None:
                raise DeskError('Chain owner unavailable. Add this endpoint and public API key to accounts.json, or log in with --master-file, a matching sibling .master.seed, or --owner ADDRESS.')
            return await asyncio.to_thread(fetch_balances, owner, opts.rpc_url, opts.limit, self.client.timeout)

        desk, chain = await asyncio.gather(self.client.call('mm_balances'), chain_query(), return_exceptions=True)
        desk_failed, chain_failed = isinstance(desk, Exception), isinstance(chain, Exception)
        result = {'mm_id': mm_id,
                  'desk': {'source': 'mm_balances', **({'error': str(desk)} if desk_failed else {'balances': desk})},
                  'chain': {'source': 'leaf_getLeafByOwner', 'owner': owner, 'error': str(chain)} if chain_failed else chain}
        if opts.json:
            self.display(result)
        else:
            print(f'Desk balances | mm_balances | Account: {mm_id}')
            if not desk_failed and desk == []:
                print('Empty (no desk balances).')
            else:
                self.display(result['desk'])
            print()
            if chain_failed:
                print('Chain balances | leaf_getLeafByOwner')
                self.display(result['chain'])
            else:
                print_balances(chain)
        if desk_failed or chain_failed:
            raise DeskError('Balance query incomplete; see the per-source errors above.')
        return True

    @staticmethod
    def display(result):
        results = result if isinstance(result, list) else [result]
        rejected = [item for item in results if isinstance(item, dict) and item.get('status') == 'rejected']
        if rejected:
            print(f'Business rejection: {len(rejected)} item(s). Details follow:')
        print(json.dumps(format_asset_values(result), ensure_ascii=False, indent=2))
        return not rejected

    async def execute(self, line):
        line = line.strip()
        if not line or line.startswith('#'):
            return True
        # Parse raw JSON without shlex, which would remove JSON's quotes.
        if line.split(None, 1)[0] == 'rpc':
            parts = line.split(None, 2)
            if len(parts) < 2:
                raise DeskError('Usage: rpc METHOD {JSON} or rpc METHOD @file.json')
            result = await self.client.call(parts[1], json_text(parts[2]) if len(parts) == 3 else {})
            return self.display(result)
        words = shlex.split(line)
        command, args = words[0], words[1:]
        if command in ('quit', 'exit'):
            self.running = False
        elif command == 'manual':
            Parser(prog='manual', description='Open mm-desk(1) using the system man viewer.').parse_args(args)
            await show_manual()
        elif command == 'transfer':
            opts = transfer_parser(Parser).parse_args(args)
            if self.demo:
                raise DeskError('Desk transfers require a live desk session; the simulator does not settle chain transactions')
            return self.display(await desk_transfer(self.client, opts))
        elif command in ('chain-balances', 'check-balance'):
            opts = self.chain_parser(command).parse_args(args)
            owner = opts.address if opts.address is not None else opts.owner
            if owner is None and self.client.session is not None:
                return await self.account_balances(opts)
            if owner is None:
                if not sys.stdin.isatty():
                    raise DeskError('An owner address is required. Use check-balance ADDRESS or chain-balances --owner ADDRESS.')
                owner = await read_console('Owner address: ')
            owner = owner.strip()
            result = await asyncio.to_thread(fetch_balances, owner, opts.rpc_url, opts.limit, self.client.timeout)
            if opts.json:
                self.display(result)
            else:
                print_balances(result)
        elif command in ('help', '?', 'methods'):
            self.help(args[0] if args else None)
        elif command == 'connect':
            if len(args) > 1:
                raise DeskError('Usage: connect [URL]')
            url = args[0] if args else self.client.url
            if not url:
                raise DeskError('Provide a WebSocket URL')
            if self.demo and url != self.client.url:
                raise DeskError('Demo mode only connects to the local simulator; exit and use normal mode to connect to a live service')
            await self.client.connect(url)
            print('Connected: ' + safe_url(url) + '; use login to authenticate.')
        elif command == 'disconnect':
            await self.client.close()
            print('Disconnected.')
        elif command in ('login', 'pubkey'):
            seed, opts = await self.get_seed(command, args)
            try:
                if command == 'pubkey':
                    return self.display({'public_api_key': public_hex(seed)})
                return await self.login(seed, opts)
            finally:
                del seed
        elif command == 'status':
            self.display({'mode': 'demo' if self.demo else 'live', 'connected': self.client.connected,
                          'url': safe_url(self.client.url), 'session': self.client.session,
                          'chain_owner': self.client.chain_owner, 'owner_source': self.client.owner_source,
                          'ignored_messages': self.client.ignored})
        elif command == 'now':
            print(now_ms())
        elif command in BY_COMMAND:
            spec = BY_COMMAND[command]
            params = {k: v for k, v in vars(method_parser(spec).parse_args(args)).items() if v is not None}
            return self.display(await self.client.call(spec.name, params))
        else:
            raise DeskError('Unknown command: ' + command + '; enter help to list commands.')
        return True

    def completion(self):
        try:
            import readline
        except ImportError:
            return
        commands = sorted(set(BY_COMMAND) | {'connect', 'disconnect', 'login', 'pubkey', 'rpc',
                                            'status', 'now', 'help', 'methods', 'quit', 'exit', 'manual', 'chain-balances', 'check-balance', 'transfer'})
        def complete(text, state):
            buffer = readline.get_line_buffer()
            before = buffer[:readline.get_begidx()]
            if not before.strip():
                choices = [c + ' ' for c in commands if c.startswith(text)]
            else:
                cmd = before.split()[0]
                if cmd in ('help', 'rpc'):
                    choices = [c + ' ' for c in commands if c.startswith(text)]
                elif cmd == 'transfer':
                    options = ('to-desk', 'from-desk') if len(before.split()) == 1 else (
                        '--token', '--amount', '--raw-amount', '--key-file', '--master-file', '--to',
                        '--leaf', '--rpc-url', '--timeout', '--yes', '--dry-run', '--no-wait', '--wait-timeout', '--json')
                    choices = [c + ' ' for c in options if c.startswith(text)]
                elif cmd in BY_COMMAND and text.startswith('-'):
                    choices = ['--' + f.name.replace('_', '-') + ' ' for f in BY_COMMAND[cmd].fields
                               if ('--' + f.name.replace('_', '-')).startswith(text)]
                elif cmd in ('login', 'pubkey') and text.startswith('-'):
                    flags = ('--seed-file', '--master-file', '--owner') if cmd == 'login' else ('--seed-file', '--master-file')
                    choices = [f + ' ' for f in flags if f.startswith(text)]
                elif cmd in ('chain-balances', 'check-balance') and text.startswith('-'):
                    choices = [f + ' ' for f in ('--owner', '--rpc-url', '--limit', '--json') if f.startswith(text)]
                else:
                    choices = []
            return choices[state] if state < len(choices) else None
        readline.set_completer_delims(' \t\n')
        readline.set_completer(complete)
        readline.parse_and_bind('bind ^I rl_complete' if 'libedit' in (readline.__doc__ or '') else 'tab: complete')
        readline.set_history_length(200)
        # No read_history_file or write_history_file: history stays in this process.

    async def run(self):
        self.completion()
        print('MM Desk CLI ' + VERSION + (' [local demo]' if self.demo else '') + ' | help: commands | Tab: complete | quit: exit')
        if self.client.latency_log is not None:
            print('Latency log: ' + str(self.client.latency_log.path))
        while self.running:
            state = 'offline' if not self.client.connected else ('ready' if self.client.session else 'login')
            try:
                line = await read_console(f'desk[{state}]> ' if sys.stdin.isatty() else '')
                await self.execute(line)
            except EOFError:
                break
            except HelpShown:
                pass
            except (DeskError, ValueError, OSError, WebSocketException) as exc:
                print('Error: ' + format_asset_text(str(exc)), file=sys.stderr)


def positive_timeout(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('timeout must be a positive finite number')
    return value


def arguments(argv=None):
    parser = argparse.ArgumentParser(description='Interactive mm_pool_desk CLI: login, orders, queries, withdrawals, and TWAP / VWAP.')
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--manual', action='store_true', help='Open mm-desk(1) using the system man viewer, without connecting')
    parser.add_argument('--offline', action='store_true', help='Skip the startup desk connection; chain-balances and manual connect remain available')
    parser.add_argument('--url', default=os.environ.get('MM_DESK_URL'), help='WebSocket URL; default ' + DEFAULT_URL + '; can also be set with MM_DESK_URL')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--seed-file', help='Log in at startup with a desk seed file')
    group.add_argument('--master-file', help='Derive a desk seed from the master seed and log in at startup')
    parser.add_argument('--owner', help='Chain owner for automatic login; otherwise use a matching master or the accounts.json public-key mapping')
    parser.add_argument('--timeout', type=positive_timeout, default=15.0, help='Request timeout in seconds, default 15')
    parser.add_argument('--latency-log', type=Path, default=default_latency_log(), metavar='FILE',
                        help='Append order request timings as JSONL; default $XDG_STATE_HOME/mm-desk/latency.jsonl or ~/.local/state/mm-desk/latency.jsonl')
    parser.add_argument('--no-proxy', action='store_true', help='Disable automatic system / environment proxy use for WebSocket connections')
    parser.add_argument('--demo', action='store_true', help='Start the local simulator and log in with its demo key')
    parser.add_argument('-c', '--command', action='append', help='Execute one CLI command and exit; may be repeated')
    args = parser.parse_args(argv)
    if args.demo and (args.url or args.seed_file or args.master_file or args.offline or args.owner):
        parser.error('--demo cannot be combined with --url, MM_DESK_URL, --seed-file, --master-file, --owner, or --offline')
    if args.owner and not (args.seed_file or args.master_file):
        parser.error('--owner requires --seed-file or --master-file for automatic login')
    if args.offline and (args.seed_file or args.master_file):
        parser.error('--offline cannot be combined with automatic login options')
    if not args.demo and not args.url:
        args.url = DEFAULT_URL
    return args


async def async_main(args):
    client = Client(args.timeout, proxy=not args.no_proxy, latency_log=LatencyLog(args.latency_log))
    demo_server = None
    try:
        if args.demo:
            from demo_server import start_demo, DEMO_SEED
            demo_server, url, _ = await start_demo()
            client.proxy = False
            await client.connect(url)
            await client.login(DEMO_SEED)
            print('Local demo mode: test key only; no real funds, on-chain transactions, or actual matching.')
        elif args.url and not args.offline:
            await client.connect(args.url)
            print('Connected: ' + safe_url(args.url))
            if args.seed_file or args.master_file:
                seed = load_seed(args.seed_file or args.master_file, bool(args.master_file))
                try:
                    await Shell(client).login(seed, args)
                finally:
                    del seed
        shell = Shell(client, demo=args.demo)
        if args.command:
            for line in args.command:
                try:
                    accepted = await shell.execute(line)
                except HelpShown:
                    accepted = True
                if not accepted:
                    return 2
                if not shell.running:
                    break
        else:
            await shell.run()
        return 0
    finally:
        await client.close()
        if demo_server is not None:
            demo_server.close()
            await demo_server.wait_closed()


async def show_manual():
    viewer = shutil.which('man')
    if viewer is None:
        raise DeskError('man(1) is not installed. Install a system man viewer, or use help COMMAND for options.')
    page = Path(__file__).resolve().with_name('mm-desk.1')
    if not page.is_file():
        raise DeskError('The mm-desk.1 manual page is missing from the application directory.')
    sys.stdout.flush()
    sys.stderr.flush()
    # Inherit the terminal and MANPAGER/PAGER. Awaiting keeps WebSocket reads and
    # keepalives running while the user reads the standard system manual viewer.
    process = await asyncio.create_subprocess_exec(viewer, str(page))
    try:
        status = await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()
        raise
    if status:
        raise DeskError(f'man(1) exited with status {status}')


def main(argv=None):
    try:
        args = arguments(argv)
        if args.manual:
            asyncio.run(show_manual())
            return 0
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print('\nExited; no automatic cancellations or request retries were sent.', file=sys.stderr)
        return 130
    except (DeskError, ValueError, OSError, WebSocketException) as exc:
        print('Error: ' + format_asset_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
