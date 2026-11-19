#!/usr/bin/env python3
"""Standalone Unix-style CLI for Cura chain balances and transfers."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import shutil
import sys

try:
    from asset_registry import format_asset_text, format_asset_values
    from chain_balances import DEFAULT_RPC, print_balances
    from chain_ops import (CHAIN_ID, ChainError, Rpc, amount_raw, balance_query, execute_prepared,
                           load_chain_key, owner_of, prepare_transfer, receipt, token_address, wait_final)
    from chain_protocol import evm_address, to_hex
except ImportError as exc:
    sys.exit('Missing dependencies. Install requirements.txt with the selected Python interpreter: ' + str(exc))

VERSION = '1.0.0'


def positive_seconds(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('must be a positive finite number')
    return value


def common_options(parser):
    parser.add_argument('--rpc-url', default=DEFAULT_RPC, help='Chain HTTP JSON-RPC endpoint')
    parser.add_argument('--timeout', type=positive_seconds, default=15, help='HTTP request timeout in seconds (default: 15)')
    parser.add_argument('--json', action='store_true', help='Output one JSON result')


def key_options(parser, required=True):
    keys = parser.add_mutually_exclusive_group(required=required)
    keys.add_argument('--key-file', help='secp256k1 chain private-key file')
    keys.add_argument('--master-file', help='mm-keys.py master seed; derive the chain key locally')


def amount_options(parser):
    parser.add_argument('--token', required=True, help='Asset name or address')
    amounts = parser.add_mutually_exclusive_group(required=True)
    amounts.add_argument('--amount', help='Amount in token units; exact decimal text')
    amounts.add_argument('--raw-amount', help='Amount in smallest units; exact integer text')


def send_options(parser, simulation=True):
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--yes', action='store_true', help='Submit after a successful simulation (default: simulate only)' if simulation else 'Submit the withdrawal request (default: preview only)')
    mode.add_argument('--dry-run', action='store_true', help='Simulate only; never submit (default)' if simulation else 'Preview the request locally; never submit (default)')
    parser.add_argument('--no-wait', action='store_true', help='Return after submission without waiting for finality')
    parser.add_argument('--wait-timeout', type=positive_seconds, default=120, help='Finality deadline in seconds (default: 120)')


def parser():
    p = argparse.ArgumentParser(prog='chain-operator', description='Query chain balances and transfer assets on Cura Chain.')
    p.add_argument('--version', action='version', version=VERSION)
    p.add_argument('--manual', action='store_true', help='Open chain-operator(1) with the system man viewer')
    commands = p.add_subparsers(dest='command', metavar='COMMAND')
    balance = commands.add_parser('balance', aliases=['balances', 'check-balance'], help='Query any chain owner address')
    balance.add_argument('address', help='Cura account address')
    balance.add_argument('--limit', type=int, default=1000, help='Leaves per page (1-1000); follows cursors')
    common_options(balance)
    transfer = commands.add_parser('transfer', help='Transfer assets between chain accounts')
    transfer.add_argument('--to', required=True, help='Destination Cura account address')
    key_options(transfer)
    amount_options(transfer)
    transfer.add_argument('--leaf', action='append', default=[], help='Specific input leaf; may be repeated')
    transfer.add_argument('--chain-id', type=int, default=CHAIN_ID, help='EIP-712 chain ID')
    send_options(transfer)
    common_options(transfer)
    address = commands.add_parser('address', help='Show public addresses for a chain key')
    key_options(address)
    address.add_argument('--json', action='store_true')
    status = commands.add_parser('receipt', help='Query an existing transaction; never resubmit it')
    status.add_argument('txhash')
    status.add_argument('--owner', help='Account address for the x-app-key routing header')
    status.add_argument('--wait', action='store_true', help='Wait until finalized or the deadline')
    status.add_argument('--wait-timeout', type=positive_seconds, default=120)
    common_options(status)
    return p


def display(value):
    print(json.dumps(format_asset_values(value), ensure_ascii=False, indent=2))


def run(args):
    if args.command in ('balance', 'balances', 'check-balance'):
        result = balance_query(args.address, args.rpc_url, args.limit, args.timeout)
        if args.json:
            display(result)
        else:
            print_balances(result)
        return
    if args.command == 'receipt':
        rpc = Rpc(args.rpc_url, args.owner, args.timeout)
        result = wait_final(rpc, args.txhash, args.wait_timeout) if args.wait else receipt(rpc, args.txhash)
        display({'txhash': args.txhash, 'receipt': result})
        return
    key = load_chain_key(args.key_file, args.master_file)
    try:
        if args.command == 'address':
            display({'account_address': owner_of(key), 'evm_address': to_hex(evm_address(key))})
            return
        if not 0 < args.chain_id < 2 ** 256:
            raise ChainError('chain-id must be a positive uint256')
        token = token_address(args.token)
        raw = amount_raw(token, args.amount, args.raw_amount)
        rpc = Rpc(args.rpc_url, owner_of(key), args.timeout)
        tx, info = prepare_transfer(key, token, raw, args.to, rpc, args.leaf, args.chain_id)
    finally:
        del key
    result = execute_prepared(rpc, tx, info, args.yes, not args.no_wait, args.wait_timeout,
                              progress=lambda text: print(text, file=sys.stderr))
    display(result)


async def manual():
    viewer = shutil.which('man')
    if not viewer:
        raise ChainError('man(1) is not installed; use --help for command usage')
    process = await asyncio.create_subprocess_exec(viewer, str(Path(__file__).with_name('chain-operator.1').resolve()))
    code = await process.wait()
    if code:
        raise ChainError(f'man(1) exited with status {code}')


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.manual:
            return asyncio.run(manual()) or 0
        if args.command is None:
            p.error('a command is required')
        run(args)
        return 0
    except KeyboardInterrupt:
        print('Interrupted. If submission began, query the transaction receipt before resending.', file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError, TypeError) as exc:
        message = format_asset_text(str(exc))
        if getattr(args, 'json', False):
            print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
        else:
            print('Error: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
