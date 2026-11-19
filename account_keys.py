"""Local account derivation matching the supplied mm-keys.py.

Keccak implementation copied from that reference; no OpenSSL command is needed.
Only public owner addresses survive login. Secrets are never sent to the chain RPC.
"""
import hashlib
import hmac
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
EIP712_SIGNER = bytes.fromhex('ff01000000000000000000000000000000000001')
ACCOUNTS_PATH = Path(__file__).with_name('accounts.json')

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(v, n):
    return ((v << n) | (v >> (64 - n))) & _M if n else v


def _keccak_f(a):
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data):
    """Ethereum's Keccak-256 (0x01 padding), which hashlib.sha3_256 is not."""
    rate = 136
    q = rate - len(data) % rate
    msg = bytearray(data) + (b"\x81" if q == 1 else b"\x01" + b"\x00" * (q - 2) + b"\x80")
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        a = _keccak_f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))



def derive_key(master, label):
    prk = hmac.new(b'bbm-mm-keys/v1', master, hashlib.sha256).digest()
    return hmac.new(prk, label + b'\x01', hashlib.sha256).digest()


def owner_from_chain_key(chain_key):
    key = ec.derive_private_key(int.from_bytes(chain_key, 'big'), ec.SECP256K1())
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)[1:]
    evm = keccak256(public)[-20:]
    return '0x' + hashlib.sha3_256(EIP712_SIGNER + evm).digest()[:20].hex()


def owner_from_master(master):
    for n in range(256):
        key = derive_key(master, b'chain/secp256k1/%d' % n)
        if 0 < int.from_bytes(key, 'big') < N:
            return owner_from_chain_key(key)
    raise ValueError('Master seed gave no valid secp256k1 scalar')


def read_seed(filename):
    with Path(filename).expanduser().open(encoding='utf-8') as stream:
        text = stream.read(4097).strip()
    if text.startswith(('0x', '0X')):
        text = text[2:]
    if not re.fullmatch('[0-9a-fA-F]{64}', text):
        raise ValueError('seed file must be 32 bytes (64 hexadecimal characters)')
    return bytes.fromhex(text)


def validate_owner(owner):
    owner = owner.strip()
    if not re.fullmatch('0[xX][0-9a-fA-F]{40}', owner):
        raise ValueError('owner must be a 20-byte address prefixed with 0x')
    return owner.lower()


def endpoint_key(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('ws', 'wss') or not parsed.hostname or parsed.fragment:
        raise ValueError('Account endpoint must be a valid ws:// or wss:// URL without a fragment')
    # Keep path and query significant. Treat an omitted path as the root path.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', parsed.query, ''))


def configured_owner(url, key_id, path=None):
    """Read public associations scoped to an endpoint and authenticated API key."""
    path = ACCOUNTS_PATH if path is None else Path(path)
    if not path.exists():
        return None, None
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate key in accounts.json')
            result[key] = value
        return result
    data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise ValueError('accounts.json must map WebSocket endpoints to public-key/owner objects')
    accounts = {}
    for endpoint, entries in data.items():
        scope = endpoint_key(endpoint)
        if urlsplit(endpoint).username is not None:
            raise ValueError('accounts.json endpoints must not contain credentials')
        if scope in accounts or not isinstance(entries, dict):
            raise ValueError('Duplicate endpoint or invalid account entries in accounts.json')
        accounts[scope] = {}
        for public, owner in entries.items():
            public = public.lower().removeprefix('0x')
            if not re.fullmatch('[0-9a-f]{64}', public) or public in accounts[scope]:
                raise ValueError('Invalid or duplicate public API key in accounts.json')
            if not isinstance(owner, str):
                raise ValueError('Account owner must be an address string in accounts.json')
            accounts[scope][public] = validate_owner(owner)
    owner = accounts.get(endpoint_key(url), {}).get(key_id.lower().removeprefix('0x'))
    return (owner, 'accounts.json public-key mapping') if owner is not None else (None, None)


def resolve_owner(seed, seed_file=None, master_file=None, owner=None):
    """Associate an owner with this login only; never infer from a desk seed alone."""
    if owner is not None:
        return validate_owner(owner), 'explicit login owner'
    if master_file:
        path = Path(master_file).expanduser()
    elif seed_file:
        path = Path(seed_file).expanduser()
        if not path.name.endswith('.desk.seed'):
            return None, None
        path = path.with_name(path.name[:-len('.desk.seed')] + '.master.seed')
        if not path.exists():
            return None, None
    else:
        return None, None
    master = read_seed(path)
    if not hmac.compare_digest(derive_key(master, b'desk/ed25519'), seed):
        raise ValueError('The master seed does not match this desk seed. Use the matching master or specify --owner for this login.')
    return owner_from_master(master), 'matching master seed'
