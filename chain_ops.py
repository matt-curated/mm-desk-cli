"""Shared chain queries and transfer operations. Never retry transaction submission."""
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from account_keys import derive_key, read_seed, validate_owner
from asset_registry import ASSETS, TOKENS
from chain_balances import DEFAULT_RPC, summarize
from chain_protocol import (CHAIN_ID, N, Leaf, account_address, asset_amount,
                            build_transfer, evm_address, hex_bytes, is_free_asset_token, to_hex)

MAX_AMOUNT = (1 << 128) - 1  # Asset ABI uses 32-byte slots but requires uint128 values.


class ChainError(ValueError):
    pass


class RpcError(ChainError):
    def __init__(self, error):
        self.code = error.get('code') if isinstance(error, dict) else None
        super().__init__('Chain RPC error: ' + json.dumps(error, ensure_ascii=False))


class Rpc:
    def __init__(self, url=DEFAULT_RPC, owner=None, timeout=15):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.fragment:
            raise ChainError('rpc-url must be an http:// or https:// URL')
        if timeout <= 0:
            raise ChainError('timeout must be positive')
        self.url, self.owner = url, validate_owner(owner) if owner is not None else None
        self.timeout, self.sequence = timeout, 0

    def call(self, method, params):
        self.sequence += 1
        rid = self.sequence
        data = json.dumps(dict(jsonrpc='2.0', id=rid, method=method, params=params)).encode()
        headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        if self.owner:
            headers['x-app-key'] = self.owner
        request = urllib.request.Request(self.url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            reply = json.load(response)
        if (not isinstance(reply, dict) or reply.get('jsonrpc') != '2.0' or
                type(reply.get('id')) is not int or reply['id'] != rid or
                (('error' in reply) == ('result' in reply))):
            raise ChainError('Invalid chain JSON-RPC response')
        if 'error' in reply:
            raise RpcError(reply['error'])
        return reply['result']

    def leaves(self, owner, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ChainError('limit must be between 1 and 1000')
        owner = validate_owner(owner)
        cursor, seen = None, set()
        for _ in range(10000):
            params = {'owner': owner, 'limit': limit}
            if cursor is not None:
                params['cursor'] = cursor
            result = self.call('leaf_getLeafByOwner', params)
            if not isinstance(result, dict) or not isinstance(result.get('leaves'), list):
                raise ChainError('Chain result.leaves is not an array')
            page = result['leaves']
            for item in page:
                address = item.get('address') if isinstance(item, dict) else None
                if not isinstance(address, str):
                    raise ChainError('Leaf record is missing its address')
                address = validate_owner(address)
                if address in seen:
                    raise ChainError('Repeated leaf in pagination; refusing an incomplete or duplicated balance')
                seen.add(address)
                yield item
            if len(page) < limit:
                return
            cursor = page[-1]['address']
        raise ChainError('Pagination limit exceeded; refusing an incomplete balance')

    def next_nonce(self, owner):
        result = self.call('leaf_getLeafByOwner', {'owner': owner, 'limit': 1})
        if not isinstance(result, dict) or not isinstance(result.get('leaves'), list):
            raise ChainError('Invalid nonce query response')
        if not result['leaves']:
            return 0
        nonce = result['leaves'][0]['leaf']['nonce']
        if type(nonce) is not int or not 0 <= nonce < (1 << 64) - 1:
            raise ChainError('Invalid or exhausted account nonce')
        return nonce + 1


def load_chain_key(key_file=None, master_file=None):
    if bool(key_file) == bool(master_file):
        raise ChainError('Provide exactly one of --key-file or --master-file')
    if key_file:
        key = read_seed(key_file)
    else:
        master = read_seed(master_file)
        for n in range(256):
            key = derive_key(master, b'chain/secp256k1/%d' % n)
            if 0 < int.from_bytes(key, 'big') < N:
                break
        else:
            raise ChainError('Master did not derive a valid chain key')
    if not 0 < int.from_bytes(key, 'big') < N:
        raise ChainError('Invalid secp256k1 chain key')
    return key


def owner_of(key):
    return to_hex(account_address(evm_address(key)))


def token_address(value):
    try:
        return validate_owner(TOKENS.get(value.upper(), value))
    except ValueError as exc:
        raise ChainError('token must be a configured asset name or a 20-byte address') from exc


def amount_raw(token, amount=None, raw_amount=None):
    if (amount is None) == (raw_amount is None):
        raise ChainError('Provide exactly one of --amount or --raw-amount')
    if raw_amount is not None:
        if not re.fullmatch(r'[0-9]+', str(raw_amount)):
            raise ChainError('raw-amount must be a positive integer')
        raw = int(raw_amount)
    else:
        decimals = ASSETS.get(token, (None, None))[1]
        if decimals is None:
            raise ChainError('Token decimals are unknown; configure assets.json or use --raw-amount')
        if not isinstance(amount, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', amount):
            raise ChainError('amount must be a positive decimal number without an exponent')
        whole, _, fraction = amount.partition('.')
        if len(fraction) > decimals:
            raise ChainError(f'This asset accepts at most {decimals} decimal places')
        raw = int(whole) * 10 ** decimals + int(fraction.ljust(decimals, '0') or '0')
    if not 0 < raw <= MAX_AMOUNT:
        raise ChainError('Amount must be between 1 and 2^128 - 1 raw units')
    return raw


def balance_query(owner, rpc_url=DEFAULT_RPC, limit=1000, timeout=15):
    owner = validate_owner(owner)
    rpc = Rpc(rpc_url, owner, timeout)
    leaves = list(rpc.leaves(owner, limit))
    result = summarize({'leaves': leaves}, owner, rpc_url, len(leaves) + 1)
    result['limit'] = limit
    result['pagination_complete'] = True
    return result


def select_funding(rpc, owner, token, amount, requested=()):
    found = {}
    token_bytes = hex_bytes(token, 20)
    for item in rpc.leaves(owner):
        info = item.get('info')
        if not isinstance(info, dict) or 'deleted_txhash' not in info:
            raise ChainError('Leaf deletion state is missing')
        if info['deleted_txhash'] is not None:
            continue
        if info.get('pending') is True:
            continue
        leaf = Leaf.from_json(item['leaf'])
        if leaf.owner != hex_bytes(owner, 20):
            raise ChainError('Owner query returned a leaf belonging to another account')
        if not is_free_asset_token(leaf, token_bytes):
            continue
        address = hex_bytes(item['address'], 20)
        if leaf.address() != address:
            raise ChainError('Leaf content does not match its address')
        found[address] = (address, asset_amount(leaf.data), leaf.index[0][0])
    if requested:
        wanted = [hex_bytes(a, 20, 'leaf') for a in requested]
        if len(set(wanted)) != len(wanted):
            raise ChainError('Duplicate --leaf inputs are not allowed')
        if any(a not in found for a in wanted):
            raise ChainError('A requested leaf is not an unspent, unlocked asset owned by this account')
        chosen = [found[a] for a in wanted]
    else:
        # Never combine distinct asset IDs even if their operator is the same.
        groups = {}
        for item in found.values():
            groups.setdefault(item[2], []).append(item)
        chosen = []
        for group in sorted(groups.values(), key=lambda xs: -sum(x[1] for x in xs)):
            if sum(x[1] for x in group) < amount:
                continue
            total = 0
            for item in sorted(group, key=lambda x: (-x[1], x[0])):
                chosen.append(item)
                total += item[1]
                if total >= amount:
                    break
            break
    if not chosen or sum(x[1] for x in chosen) < amount:
        raise ChainError(f'Insufficient unlocked balance of {token} for {amount} raw units')
    if len({x[2] for x in chosen}) != 1:
        raise ChainError('Selected leaves have different asset IDs')
    return chosen


def prepare_transfer(key, token, raw, to, rpc, leaves=(), chain_id=CHAIN_ID):
    owner, to = owner_of(key), validate_owner(to)
    chosen = select_funding(rpc, owner, token, raw, leaves)
    receiver_nonce = rpc.next_nonce(to)
    sender_nonce = receiver_nonce if owner == to else rpc.next_nonce(owner)
    tx, info = build_transfer(key, hex_bytes(token, 20), chosen[0][2],
                              [(x[0], x[1]) for x in chosen], raw, hex_bytes(to, 20),
                              receiver_nonce, sender_nonce, chain_id)
    return tx, dict({k: v for k, v in info.items() if k != 'change'}, operation='transfer', token=token, amount_raw=str(raw), to=to,
                    inputs=[to_hex(x[0]) for x in chosen], change_raw=str(info['change']))


def simulate(rpc, tx):
    result = rpc.call('transaction_debugTransaction', {'transaction': tx.to_json()})
    body = result.get('result', result) if isinstance(result, dict) else None
    if not isinstance(body, dict):
        raise ChainError('Invalid transaction simulation response')
    codes = []
    for name in ('unlockers', 'operators'):
        entries = body.get(name, []) or []
        if not isinstance(entries, list):
            raise ChainError('Invalid simulation code list')
        for entry in entries:
            if not isinstance(entry, dict) or type(entry.get('code')) is not int:
                raise ChainError('Simulation entry is missing its execution code')
            codes.append(entry['code'])
    codes.extend(body.get('unlocker_codes', []) or [])
    operators = body.get('operator_codes', {}) or {}
    if not isinstance(operators, dict):
        raise ChainError('Invalid simulation operator codes')
    codes.extend(operators.values())
    unlocks = body.get('unlockers', body.get('unlocker_codes', [])) or []
    reported = {str(x.get('operator', '')).lower() for x in body.get('operators', []) or []}
    reported.update(str(k).lower() for k in operators)
    required = {to_hex(x.operator) for x in tx.outputs if x.operator is not None}
    if (body.get('status') not in (None, 'Success') or len(unlocks) != len(tx.inputs) or
            not required.issubset(reported) or not codes or
            any(type(c) is not int or c != 0 for c in codes)):
        raise ChainError('Transaction simulation failed: ' + json.dumps(result))
    return result


def receipt(rpc, txhash):
    txhash = to_hex(hex_bytes(txhash, 32, 'transaction hash'))
    result = rpc.call('transaction_getReceiptByTxHash', {'hash': txhash})
    if result is None:
        return None
    if not isinstance(result, dict):
        raise ChainError('Invalid transaction receipt')
    return result.get('receipt', result)


def wait_final(rpc, txhash, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = receipt(rpc, txhash)
        except RpcError as exc:
            if exc.code != -32011:
                raise
            result = None
        state = result.get('tx_status') if isinstance(result, dict) else None
        if state == 'Finalized':
            return result
        if state in ('Failed', 'Conflicted'):
            raise ChainError(f'Transaction {txhash} {state}')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChainError(f'Transaction {txhash} is not final; last status {state}. Do not resend; query its receipt.')
        time.sleep(min(1, remaining))


def execute_prepared(rpc, tx, info, send=False, wait=True, wait_timeout=120, progress=None):
    info = dict(info)
    info['simulation'] = simulate(rpc, tx)
    if not send:
        return dict(info, status='dry_run', submitted=False)
    txhash = to_hex(tx.hash())
    if progress:
        progress(f'Submitting transaction {txhash}')
    try:
        result = rpc.call('mempool_sendTransaction', {'transaction': to_hex(tx.postcard())})
    except RpcError:
        raise
    except (OSError, ValueError, TimeoutError) as exc:
        raise ChainError(f'Submission status unknown for {txhash}; do not resend. Query its receipt. {exc}') from exc
    if not isinstance(result, dict) or str(result.get('hash', '')).lower() != txhash:
        raise ChainError(f'Submission hash mismatch for {txhash}; do not resend. Node response: {result}')
    info.update(status='submitted', submitted=True)
    if wait:
        if progress:
            progress(f'Waiting for finality: {txhash}')
        try:
            info['receipt'] = wait_final(rpc, txhash, wait_timeout)
        except (ValueError, OSError) as exc:
            raise ChainError(f'Transaction {txhash} was submitted; finality check failed. Do not resend. {exc}') from exc
        info['status'] = 'finalized'
    return info


def prepare_deposit(key, token, raw, rpc, network, leaves=()):
    from chain_protocol import build_deposit, sha3_256
    owner = owner_of(key)
    spot = hex_bytes(network['spot_contract'], 20)
    pool = hex_bytes(network['pool_owner'], 20)
    pay = select_funding(rpc, owner, token, raw, leaves)
    return build_deposit(key, hex_bytes(token, 20), pay[0][2], [(x[0], x[1]) for x in pay],
                         raw, spot, pool, rpc.next_nonce(owner),
                         rpc.next_nonce(to_hex(sha3_256(spot)[:20])), network['chain_id'])
