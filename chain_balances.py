"""Read-only leaf balance query, following the user's supplied RPC/ABI mapping."""
import json
import re
import urllib.request
from urllib.parse import urlsplit
from asset_registry import ASSETS, asset_label, display_width, format_asset_values

DEFAULT_RPC = 'https://rpc.testnet.curated.finance'


def decimal_string(raw, decimals):
    # Exact even above Decimal's default precision; never use a float.
    if decimals == 0:
        return str(raw)
    whole, fraction = divmod(raw, 10 ** decimals)
    tail = str(fraction).zfill(decimals).rstrip('0')
    return str(whole) + ('.' + tail if tail else '')


def summarize(result, owner, rpc_url, limit):
    if not isinstance(result, dict) or not isinstance(result.get('leaves'), list):
        raise ValueError('The chain RPC result.leaves value is not an array')
    leaves = result['leaves']
    totals = {}
    malformed = 0
    matched = 0
    for item in leaves:
        try:
            if item['info']['deleted_txhash'] is not None:
                continue
            leaf = item['leaf']
            if int(leaf['index'][1]['value'], 16) != 3:
                continue
            data = leaf['data']
            if not isinstance(data, str) or not data.startswith('0x') or len(data) < 130:
                raise ValueError('short leaf data')
            word = data[2:][64:128]
            if not re.fullmatch(r'[0-9a-fA-F]{64}', word):
                raise ValueError('invalid amount word')
            operator = leaf['operator'].lower()
            if not re.fullmatch(r'0x[0-9a-f]{40}', operator):
                raise ValueError('invalid operator')
            totals[operator] = totals.get(operator, 0) + int(word, 16)
            matched += 1
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            malformed += 1
    rows = []
    for token, raw in totals.items():
        symbol, decimals = ASSETS.get(token, (token, None))
        rows.append(dict(symbol=symbol, token=token, decimals=decimals, raw=str(raw),
                         balance=decimal_string(raw, decimals) if decimals is not None else None))
    rows.sort(key=lambda x: x['symbol'])
    notes = []
    pagination = {k: v for k, v in result.items() if k != 'leaves'}
    if len(leaves) >= limit or pagination.get('has_more') or pagination.get('next_cursor'):
        notes.append('More pages may be available. The pagination protocol is unspecified, so no additional pages were fetched.')
    if malformed:
        notes.append(f'{malformed} malformed leaf record(s) were excluded from balances.')
    if any(row['decimals'] is None for row in rows):
        notes.append('Assets without configured decimals show raw amounts only; precision is not inferred.')
    return dict(source='leaf_getLeafByOwner', rpc_url=rpc_url, owner=owner,
                leaf_count=len(leaves), balance_leaf_count=matched, malformed_leaf_count=malformed,
                limit=limit, possibly_partial=bool(notes and (len(leaves) >= limit or malformed or
                    pagination.get('has_more') or pagination.get('next_cursor'))),
                balances=rows, notes=notes)


def fetch_balances(owner, rpc_url=DEFAULT_RPC, limit=1000, timeout=15):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', owner):
        raise ValueError('owner must be a 20-byte address prefixed with 0x')
    parsed = urlsplit(rpc_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.fragment:
        raise ValueError('rpc-url must be a valid http:// or https:// URL')
    if type(limit) is not int or limit < 1:
        raise ValueError('limit must be a positive integer')
    payload = dict(jsonrpc='2.0', id=1, method='leaf_getLeafByOwner', params={'owner': owner, 'limit': limit})
    request = urllib.request.Request(rpc_url, data=json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    if not isinstance(data, dict) or data.get('jsonrpc') != '2.0' or type(data.get('id')) is not int or data['id'] != 1:
        raise ValueError('The chain RPC returned an invalid JSON-RPC response')
    if 'error' in data:
        raise ValueError('Chain RPC error: ' + json.dumps(format_asset_values(data['error']), ensure_ascii=False))
    return summarize(data.get('result'), owner, rpc_url, limit)


def print_balances(result):
    print('Chain balances | leaf_getLeafByOwner')
    print('Owner: ' + result['owner'])
    print('RPC:   ' + result['rpc_url'])
    labels = [asset_label(row['token']) for row in result['balances']]
    width = max([len('Asset')] + [display_width(label) for label in labels])
    print(f"\n{'Asset':<{width}}  {'Balance':>28}  Raw amount")
    for row, label in zip(result['balances'], labels):
        value = row['balance'] if row['balance'] is not None else 'Unknown precision'
        if row['balance'] is not None:
            whole, dot, fraction = value.partition('.')
            value = format(int(whole), ',') + (dot + fraction if dot else '')
        padding = ' ' * (width - display_width(label))
        print(f"{label}{padding}  {value:>28}  {row['raw']}")
    if not result['balances']:
        print('No matching balance leaves were found in this query.')
    print(f"\nRead {result['leaf_count']} leaf record(s); included {result['balance_leaf_count']} balance leaf record(s).")
    for note in result['notes']:
        print('Note: ' + note)
