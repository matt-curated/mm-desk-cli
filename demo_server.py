"""Local protocol simulator. No exchange matching, settlement or real deadman timer."""
import asyncio
import json
import secrets
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from websockets.asyncio.server import serve

DEMO_SEED = bytes(range(32))  # Public test fixture, NEVER for a real account.
DOMAIN = b'mm_pool_desk/login:'


class DemoError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


class DemoDesk:
    def __init__(self, seed=DEMO_SEED, scopes=None, noise=False):
        self.public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        self.scopes = ['read', 'trade', 'withdraw'] if scopes is None else scopes
        self.noise = noise
        self.calls = []
        self.orders = {}
        self.requests = {}
        self.algos = {}
        self.withdrawals = []
        self.deadman = 0
        self.counter = 0

    def next_id(self):
        self.counter += 1
        return self.counter

    @staticmethod
    def stamp():
        return time.time_ns() // 1_000_000

    def request(self, kind, order_id=None, book=None):
        rid = self.next_id()
        self.requests[rid] = dict(pool_request_id=rid, kind=kind, state='accepted',
                                  reject_code=None, reject_detail=None, order_id=order_id,
                                  book=book, sent=True, created_at=self.stamp(), answered_at=self.stamp())
        return rid

    def lookup(self, collection, key):
        if key not in collection:
            raise DemoError(-32011, 'not_found')
        return collection[key]

    def history(self, rows, params):
        rows = [dict(x) for x in rows if
                ('book' not in params or x.get('book') == params['book']) and
                ('state' not in params or x.get('state') == params['state']) and
                ('from' not in params or x['created_at'] >= params['from']) and
                ('to' not in params or x['created_at'] < params['to'])]
        offset = params.get('offset', 0)
        return rows[offset:offset + params.get('limit', len(rows))]

    def place(self, p):
        oid = self.next_id()
        rid = self.request('place', oid, p['book'])
        self.orders[oid] = dict(order_id=oid, book=p['book'], side=p['side'], price=p['price'],
                               quantity=p['quantity'], filled='0', remaining=p['quantity'], tif=p['tif'],
                               post_only=p.get('post_only', False), client_nonce=None, version=1,
                               state='open' if p['tif'] == 'gtc' else 'closed', close_reason=None,
                               fill_in_flight=False, change_in_flight=False, parent_id=None,
                               created_at=self.stamp(), updated_at=self.stamp(), fills=[])
        return dict(status='accepted', order_id=oid, pool_request_id=rid)

    def cancel(self, oid):
        order = self.lookup(self.orders, oid)
        if order['state'] == 'closed':
            return dict(status='rejected', code='already_closed', detail='The simulated order is already closed')
        order.update(state='closed', close_reason='cancelled', updated_at=self.stamp())
        return dict(status='accepted', order_id=oid,
                    pool_request_id=self.request('cancel', oid, order['book']), absorbed=False)

    async def dispatch(self, name, p, session):
        if name == 'mm_challenge':
            session['challenge'] = secrets.token_bytes(32)
            return {'challenge': session['challenge'].hex()}
        if name == 'mm_login':
            challenge = session.pop('challenge', None)
            try:
                if challenge is None or p['key_id'] != self.public:
                    raise ValueError('wrong key or no challenge')
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(p['key_id'])).verify(
                    bytes.fromhex(p['signature']), DOMAIN + challenge)
            except Exception:
                raise DemoError(-32001, 'invalid_signature')
            session['logged_in'] = True
            return {'mm_id': 1, 'scopes': list(self.scopes)}
        if not session.get('logged_in'):
            raise DemoError(-32001, 'not_logged_in')
        if name in ('mm_placeOrder', 'mm_modifyOrder', 'mm_cancelOrder', 'mm_cancelAll',
                    'mm_withdraw', 'mm_placeAlgo', 'mm_cancelAlgo', 'mm_pauseAlgo', 'mm_resumeAlgo'):
            if type(p.get('ts')) is not int or abs(p['ts'] - self.stamp()) > 5000:
                raise DemoError(-32002, 'stale_timestamp')
        if name == 'mm_placeOrders':
            if not isinstance(p.get('orders'), list) or not 1 <= len(p['orders']) <= 50:
                raise DemoError(-32602, 'invalid_batch')
            for order in p['orders']:
                if type(order.get('ts')) is not int or abs(order['ts'] - self.stamp()) > 5000:
                    raise DemoError(-32002, 'stale_timestamp')
        for value in ([p] if name != 'mm_placeOrders' else p['orders']):
            for field in ('price', 'quantity', 'remaining', 'amount', 'limit_price'):
                if field in value and (not isinstance(value[field], str) or not value[field].isdigit()):
                    raise DemoError(-32602, 'amount_must_be_integer_string')
        if name == 'mm_setDeadman':
            self.deadman = p['timeout_ms']
            return dict(armed=bool(self.deadman), timeout_ms=self.deadman)
        if name == 'mm_gatewayToken':
            return dict(token='DEMO_NOT_A_VALID_JWT', channels=['mm:btc_usdt:spot'],
                        expires_at=int(time.time()) + 3600)
        if name == 'mm_placeOrder':
            return self.place(p)
        if name == 'mm_placeOrders':
            return [self.place(order) for order in p['orders']]
        if name == 'mm_modifyOrder':
            order = self.lookup(self.orders, p['order_id'])
            if order['state'] == 'closed':
                return dict(status='rejected', code='already_closed', detail='The simulated order is already closed')
            if 'quantity' in p:
                order['quantity'] = p['quantity']
                order['remaining'] = str(int(p['quantity']) - int(order['filled']))
            if 'remaining' in p:
                order['remaining'] = p['remaining']
                order['quantity'] = str(int(p['remaining']) + int(order['filled']))
            if 'price' in p:
                order['price'] = p['price']
            order['version'] += 1
            order['updated_at'] = self.stamp()
            return dict(status='accepted', order_id=p['order_id'],
                        pool_request_id=self.request('modify', p['order_id'], order['book']))
        if name == 'mm_cancelOrder':
            return self.cancel(p['order_id'])
        if name == 'mm_cancelAll':
            return [self.cancel(order['order_id']) for order in self.orders.values()
                    if order['state'] in ('open', 'pending', 'partially_filled', 'suspended') and
                    ('book' not in p or order['book'] == p['book'])]
        if name == 'mm_openOrders':
            return [dict(x) for x in self.orders.values() if x['state'] in ('open', 'pending', 'partially_filled', 'suspended')
                    and ('book' not in p or x['book'] == p['book'])]
        if name == 'mm_getOrder':
            return dict(self.lookup(self.orders, p['order_id']))
        if name == 'mm_listOrders':
            return self.history(self.orders.values(), p)
        if name == 'mm_getRequest':
            return dict(self.lookup(self.requests, p['pool_request_id']))
        if name == 'mm_listRequests':
            return self.history(self.requests.values(), p)
        if name == 'mm_balances':
            return [dict(token='usdt', available='1000000000000', in_flight='0', settled='1000000000000',
                         reserved='0', pending_out='0', pending_in='0', frozen='0', deposits_in_progress='0')]
        if name == 'mm_deposits':
            return []
        if name == 'mm_withdraw':
            rid = self.request('withdraw')
            self.withdrawals.append(dict(pool_request_id=rid, token=p['token'], amount=p['amount'],
                                         to=p['to'], state='pending', reason=None, release_txhash=None,
                                         payout=None, created_at=self.stamp()))
            return dict(status='accepted', pool_request_id=rid)
        if name == 'mm_withdrawals':
            return list(self.withdrawals)
        if name == 'mm_placeAlgo':
            parent_id = self.next_id()
            self.algos[parent_id] = dict(p, parent_id=parent_id, state='running', filled='0', in_slices='0',
                                         reserved=p['quantity'], end_reason=None, slices=[])
            return dict(parent_id=parent_id)
        if name == 'mm_getAlgo':
            return dict(self.lookup(self.algos, p['parent_id']))
        if name in ('mm_pauseAlgo', 'mm_resumeAlgo', 'mm_cancelAlgo'):
            algo = self.lookup(self.algos, p['parent_id'])
            algo['state'] = {'mm_pauseAlgo': 'paused', 'mm_resumeAlgo': 'running', 'mm_cancelAlgo': 'cancelled'}[name]
            return dict(parent_id=p['parent_id'], state=algo['state'])
        raise DemoError(-32601, 'method_not_found')

    async def handler(self, ws):
        session = {}
        async for message in ws:
            request = json.loads(message)
            name, p, rid = request['method'], request['params'], request['id']
            self.calls.append((name, p))
            if self.noise:
                await ws.send(json.dumps({'jsonrpc': '2.0', 'id': -999, 'result': 'unrelated'}))
                await ws.send(json.dumps({'jsonrpc': '2.0', 'method': 'notice', 'params': {}}))
            try:
                result = await self.dispatch(name, p, session)
                response = {'jsonrpc': '2.0', 'id': rid, 'result': result}
            except DemoError as exc:
                response = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': exc.code, 'message': exc.message}}
            await ws.send(json.dumps(response))


async def start_demo(desk=None):
    desk = desk or DemoDesk()
    server = await serve(desk.handler, '127.0.0.1', 0, ping_interval=15)
    port = server.sockets[0].getsockname()[1]
    return server, f'ws://127.0.0.1:{port}', desk
