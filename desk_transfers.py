"""Transfers between the authenticated desk account and its chain account."""
import asyncio
import json
from pathlib import Path
import sys
import time

from account_keys import endpoint_key, validate_owner
from chain_operator import amount_options, common_options, key_options, send_options
from chain_ops import (ChainError, Rpc, RpcError, amount_raw, execute_prepared, load_chain_key,
                       owner_of, prepare_deposit, receipt, token_address)

NETWORKS = Path(__file__).with_name('desk_networks.json')


def transfer_parser(parser_class):
    parser = parser_class(prog='transfer', description='Move assets between your chain account and the logged-in desk. Default: preview only.')
    directions = parser.add_subparsers(dest='direction', required=True, metavar='DIRECTION', parser_class=parser_class)
    deposit = directions.add_parser('to-desk', help='Deposit chain assets into the desk pool')
    key_options(deposit)
    deposit.add_argument('--leaf', action='append', default=[], help='Input asset leaf; may be repeated')
    withdraw = directions.add_parser('from-desk', help='Request a desk withdrawal to your chain account')
    withdraw.add_argument('--to', help='Own Cura account address; defaults to the owner linked to this login')
    for child in (deposit, withdraw):
        amount_options(child)
        send_options(child, simulation=child is deposit)
        common_options(child)
        child.set_defaults(rpc_url=None)
    return parser


def network_for(url):
    data = json.loads(NETWORKS.read_text(encoding='utf-8'))
    for endpoint, values in data.items():
        if endpoint_key(endpoint) == endpoint_key(url):
            result = dict(values)
            result['pool_owner'] = validate_owner(result['pool_owner'])
            result['spot_contract'] = validate_owner(result['spot_contract'])
            if type(result['chain_id']) is not int or not 0 < result['chain_id'] < 2 ** 256:
                raise ChainError('Invalid chain_id in desk_networks.json')
            return result
    raise ChainError('No network configuration for this desk endpoint in desk_networks.json')


def progress(message):
    print(message, file=sys.stderr)


async def wait_deposit(client, txhash, deadline):
    while True:
        rows = await client.call('mm_deposits')
        if not isinstance(rows, list):
            raise ChainError('Invalid mm_deposits response')
        for row in rows:
            if str(row.get('txhash', '')).lower() == txhash.lower():
                return row
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChainError(f'Deposit {txhash} is final but desk credit is not yet observed. Check deposits and balances; do not deposit again.')
        await asyncio.sleep(min(3, remaining))


async def wait_withdrawal(client, rpc, request_id, deadline):
    while True:
        rows = await client.call('mm_withdrawals')
        if not isinstance(rows, list):
            raise ChainError('Invalid mm_withdrawals response')
        for row in rows:
            if str(row.get('pool_request_id')) != str(request_id):
                continue
            payout = row.get('payout') or {}
            if str(row.get('state', '')).lower() in ('failed', 'rejected', 'cancelled') or str(payout.get('state', '')).lower() in ('failed', 'rejected'):
                raise ChainError(f'Withdrawal {request_id} failed: {json.dumps(row)}')
            txhash = payout.get('txhash')
            if txhash:
                try:
                    result = await asyncio.to_thread(receipt, rpc, txhash)
                except RpcError as exc:
                    if exc.code != -32011:
                        raise
                    result = None
                state = result.get('tx_status') if isinstance(result, dict) else None
                if state == 'Finalized':
                    return dict(withdrawal=row, receipt=result)
                if state in ('Failed', 'Conflicted'):
                    raise ChainError(f'Withdrawal {request_id} payout {txhash} {state}')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChainError(f'Withdrawal {request_id} was submitted but its payout is not final. Check withdrawals; do not resubmit.')
        await asyncio.sleep(min(3, remaining))


async def transfer(client, opts):
    if not client.connected or client.session is None:
        raise ChainError('Connect and log in before transferring to or from the desk')
    network = network_for(client.url)
    owner = client.chain_owner
    if owner is None:
        raise ChainError('The current login has no linked chain owner; configure accounts.json or log in with --owner')
    token = token_address(opts.token)
    raw = amount_raw(token, opts.amount, opts.raw_amount)
    rpc = Rpc(opts.rpc_url or network['rpc_url'], owner, opts.timeout)
    if opts.direction == 'to-desk':
        key = load_chain_key(opts.key_file, opts.master_file)
        try:
            if owner_of(key) != owner:
                raise ChainError('The chain key does not match the chain owner linked to this desk login')
            tx, info = await asyncio.to_thread(prepare_deposit, key, token, raw, rpc, network, opts.leaf)
        finally:
            del key
        result = await asyncio.to_thread(execute_prepared, rpc, tx, info, opts.yes,
                                         not opts.no_wait, opts.wait_timeout, progress)
        if opts.yes and not opts.no_wait:
            progress(f'Waiting for desk credit: {result["txhash"]}')
            try:
                result['deposit'] = await wait_deposit(client, result['txhash'], time.monotonic() + opts.wait_timeout)
            except Exception as exc:
                raise ChainError(f'Chain deposit {result["txhash"]} finalized; desk credit check failed. Do not resend. {exc}') from exc
            result['status'] = 'credited'
        return result
    to = validate_owner(opts.to) if opts.to else owner
    if to != owner:
        raise ChainError('Desk withdrawals must return to the chain owner linked to this login')
    params = {'token': token, 'amount': str(raw), 'to': to}
    if not opts.yes:
        return dict(status='preview', submitted=False, operation='mm_withdraw', params=params,
                    required_scope='withdraw', has_scope='withdraw' in client.session.get('scopes', []),
                    note='This is a local request preview; the desk API has no withdrawal simulation method.')
    result = await client.call('mm_withdraw', params)
    if not isinstance(result, dict) or result.get('status') != 'accepted':
        return result
    if opts.no_wait:
        return result
    request_id = result.get('pool_request_id')
    if request_id is None:
        raise ChainError('Withdrawal accepted without a request ID; check withdrawals before resubmitting')
    progress(f'Withdrawal accepted: {request_id}. Waiting for payout finality.')
    try:
        completed = await wait_withdrawal(client, rpc, request_id, time.monotonic() + opts.wait_timeout)
    except Exception as exc:
        raise ChainError(f'Withdrawal {request_id} was accepted; status check failed. Do not resubmit. {exc}') from exc
    return dict(status='finalized', submission=result, **completed)
