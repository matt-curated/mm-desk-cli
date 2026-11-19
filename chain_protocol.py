"""Cura leaf/transaction encoding adapted from the supplied transfer.py demo.

Secret-key operations use libsecp256k1 via coincurve, not the demo's Python curve math.
"""
import hashlib
from coincurve import PrivateKey
from account_keys import keccak256, EIP712_SIGNER

CHAIN_ID = 111111111111
GAS_LIMIT = 99_999_999
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

def hex_bytes(text, length=None, what="value"):
    text = text.strip()
    text = text[2:] if text[:2].lower() == "0x" else text
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ValueError(f"{what}: not hex") from None
    if length is not None and len(raw) != length:
        raise ValueError(f"{what}: expected {length} bytes, got {len(raw)}")
    return raw


def read_key_file(path, what):
    """A key file: one line of 64 hex characters, with or without 0x."""
    with open(path) as f:
        return hex_bytes(f.read(), 32, f"{what} in {path}")


def to_hex(raw):
    return "0x" + raw.hex()

TXHASH_TYPEHASH = bytes.fromhex("9cf5c9fd0f740f59a1f7b0fac559e927170ae0631bb93ef9f8ded0dfda8eb395")


def domain_separator(chain_id=CHAIN_ID, version=b"1"):
    """keccak256 of EIP712Domain(string version,uint256 chainId): no name, no contract."""
    typehash = keccak256(b"EIP712Domain(string version,uint256 chainId)")
    return keccak256(typehash + keccak256(version) + chain_id.to_bytes(32, "big"))


DOMAIN_SEPARATOR = domain_separator()


def eip712_digest(txhash, domain=DOMAIN_SEPARATOR):
    return keccak256(b"\x19\x01" + domain + keccak256(TXHASH_TYPEHASH + txhash))


EIP712_SIGNER = bytes.fromhex("ff01000000000000000000000000000000000001")


def unlock_script_address(code_leaf, args=b""):
    return sha3_256(code_leaf + args)[:20]


def sha3_256(data):
    return hashlib.sha3_256(data).digest()


def evm_address(private_key):
    return keccak256(PrivateKey(private_key).public_key.format(compressed=False)[1:])[-20:]


def sign_prehash(private_key, digest):
    signature = PrivateKey(private_key).sign_recoverable(digest, hasher=None)
    return signature[:64] + bytes([signature[64] + 27])


def account_address(evm):
    """The Cura Chain account an EVM key controls: what owns its asset tokens."""
    return unlock_script_address(EIP712_SIGNER, evm)


LEAF_TYPES = {"data": 0, "wasm": 1, "evm": 2}


class Leaf:
    """A leaf. `index` is six (value: 32 bytes, unique: bool) pairs."""

    def __init__(self, nonce, owner, index, operator, data, ty="data"):
        self.nonce = nonce
        self.owner = owner
        self.index = [(v, u) for v, u in index]
        self.ty = ty
        self.operator = operator
        self.data = data

    def hash(self):
        h = hashlib.sha3_256()
        h.update(self.nonce.to_bytes(8, "big"))
        h.update(self.owner)
        for value, unique in self.index:
            h.update(value)
            h.update(b"\x01" if unique else b"\x00")
        h.update(LEAF_TYPES[self.ty].to_bytes(2, "big"))
        h.update(self.operator if self.operator is not None else b"\x00" * 20)
        h.update(self.data)
        return h.digest()

    def address(self):
        return self.hash()[:20]

    def postcard(self):
        out = varint(self.nonce) + pc_bytes(self.owner)
        for value, unique in self.index:
            out += pc_bytes(value) + (b"\x01" if unique else b"\x00")
        out += varint(LEAF_TYPES[self.ty])
        out += b"\x00" if self.operator is None else b"\x01" + pc_bytes(self.operator)
        return out + pc_bytes(self.data)

    def to_json(self):
        out = {
            "nonce": self.nonce,
            "owner": to_hex(self.owner),
            "index": [{"unique": u, "value": to_hex(v)} for v, u in self.index],
            "ty": self.ty,
            "data": to_hex(self.data),
        }
        if self.operator is not None:
            out["operator"] = to_hex(self.operator)
        return out

    @classmethod
    def from_json(cls, j):
        op = j.get("operator")
        return cls(
            j["nonce"],
            hex_bytes(j["owner"], 20),
            [(hex_bytes(s["value"], 32), bool(s["unique"])) for s in j["index"]],
            hex_bytes(op, 20) if op else None,
            hex_bytes(j["data"]) if j["data"] not in ("0x", "") else b"",
            j.get("ty", "data"),
        )


def varint(value):
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def pc_bytes(raw):
    return varint(len(raw)) + raw


class Transaction:
    def __init__(self, inputs, input_ro, outputs, gas_limit, unlockers=None):
        self.inputs = inputs
        self.input_ro = input_ro
        self.outputs = outputs
        self.gas_limit = gas_limit
        self.unlockers = unlockers or []

    def hash(self):
        """SHA3-256 over gas_limit, inputs, input_ro and the outputs' leaf addresses."""
        h = hashlib.sha3_256()
        h.update(self.gas_limit.to_bytes(8, "big"))
        for a in self.inputs:
            h.update(a)
        for a in self.input_ro:
            h.update(a)
        for leaf in self.outputs:
            h.update(leaf.address())
        return h.digest()

    def postcard(self):
        """The bytes mempool_sendTransaction takes."""
        out = varint(self.gas_limit)
        out += varint(len(self.inputs)) + b"".join(pc_bytes(a) for a in self.inputs)
        out += varint(len(self.input_ro)) + b"".join(pc_bytes(a) for a in self.input_ro)
        out += varint(len(self.outputs)) + b"".join(leaf.postcard() for leaf in self.outputs)
        out += varint(len(self.unlockers)) + b"".join(pc_bytes(u) for u in self.unlockers)
        return out

    def to_json(self):
        """The object transaction_debugTransaction takes."""
        return {
            "unsigned": {
                "gas_limit": self.gas_limit,
                "inputs": [to_hex(a) for a in self.inputs],
                "input_ro": [to_hex(a) for a in self.input_ro],
                "outputs": [leaf.to_json() for leaf in self.outputs],
            },
            "unlockers": [to_hex(u) for u in self.unlockers],
        }

    @classmethod
    def from_json(cls, j):
        u = j["unsigned"]
        return cls(
            [hex_bytes(a, 20) for a in u["inputs"]],
            [hex_bytes(a, 20) for a in u["input_ro"]],
            [Leaf.from_json(o) for o in u["outputs"]],
            u["gas_limit"],
            [hex_bytes(x) for x in j.get("unlockers", [])],
        )


def eip712_unlocker(private_key, txhash, chain_id=CHAIN_ID):
    """The unlocker for an input the key's account owns: script, then the 65-byte signature."""
    signature = sign_prehash(private_key, eip712_digest(txhash, domain_separator(chain_id)))
    return pc_bytes(EIP712_SIGNER) + pc_bytes(evm_address(private_key)) + signature


ASSET_KIND = (3).to_bytes(32, "big")


ASSET_FAMILY = b"\xff" * 32


ZERO32 = b"\x00" * 32


def left_pad(address):
    return b"\x00" * 12 + address


def asset_data(amount):
    if type(amount) is not int or not 0 <= amount < 2 ** 128:
        raise ValueError('Asset amount must fit uint128')
    return (1).to_bytes(32, "big") + amount.to_bytes(32, "big")


def asset_amount(data):
    """An asset token's amount: the second 32-byte word of its data."""
    if len(data) != 64 or int.from_bytes(data[:32], "big") != 1:
        raise ValueError("not asset token data")
    amount = int.from_bytes(data[32:], "big")
    if amount >= 2 ** 128:
        raise ValueError('Asset amount exceeds uint128')
    return amount


def is_free_asset_token(leaf, token):
    """An unlocked asset token of `token`: index[1] marks the asset kind, index[2] is zero."""
    return (leaf.operator == token and leaf.index[1][0] == ASSET_KIND
            and leaf.index[2][0] == ZERO32 and leaf.index[5][0] == ASSET_FAMILY)



def asset_token(owner, token, asset_id, amount, nonce, lock_to=None, original_owner=None):
    index = [
        (asset_id, False),
        (ASSET_KIND, False),
        (left_pad(lock_to) if lock_to else ZERO32, False),
        (left_pad(original_owner) if original_owner else ZERO32, False),
        (ZERO32, False),
        (ASSET_FAMILY, False),
    ]
    return Leaf(nonce, owner, index, token, asset_data(amount))



def build_transfer(chain_key, token, asset_id, pay, amount, receiver, receiver_nonce, sender_nonce, chain_id=CHAIN_ID):
    """A signed transfer. `pay` is a list of (leaf address, amount) of your free asset tokens of `token`."""
    sender = account_address(evm_address(chain_key))
    total = sum(a for _, a in pay)
    if not pay or total < amount:
        raise ValueError(f"the inputs hold {total} raw, the transfer is {amount}")
    sent = asset_token(receiver, token, asset_id, amount, receiver_nonce)
    outputs = [sent]
    if total > amount:
        change_nonce = receiver_nonce + 1 if receiver == sender else sender_nonce
        outputs.append(asset_token(sender, token, asset_id, total - amount, change_nonce))
    tx = Transaction([a for a, _ in pay], [], outputs, GAS_LIMIT)
    txhash = tx.hash()
    tx.unlockers = [eip712_unlocker(chain_key, txhash, chain_id)] * len(tx.inputs)
    return tx, {
        "sender": to_hex(sender),
        "receiver_leaf": to_hex(sent.address()),
        "change": total - amount,
        "txhash": to_hex(txhash),
    }



def build_deposit(chain_key, token, asset_id, pay, amount, spot, pool_owner,
                  sender_nonce, custody_nonce, chain_id=CHAIN_ID):
    """deposit_pool: the SDK's command leaf plus the pool's locked asset and change.

    Command layout: dex/src/leaves.zig and selectors.zig. Pool custody/index
    layout matches the live pool leaves; both operators verified by debug RPC.
    """
    sender = account_address(evm_address(chain_key))
    total = sum(value for _, value in pay)
    if not pay or not 0 < amount <= total:
        raise ValueError('Invalid deposit funding or amount')
    if len(spot) != 20 or len(pool_owner) != 20:
        raise ValueError('Spot contract and pool owner must be 20-byte addresses')
    marker = b'\x00\x01' + b'\x00' * 29 + b'\x04'
    command = Leaf(sender_nonce, sender, [(marker, False)] + [(ZERO32, False)] * 5,
                   spot, b'\x00' + keccak256(b'deposit_pool()')[:4] + b'\x00' * 27)
    custody = sha3_256(spot)[:20]
    locked = asset_token(custody, token, asset_id, amount, custody_nonce,
                         lock_to=spot, original_owner=pool_owner)
    outputs = [command, locked]
    if total > amount:
        outputs.append(asset_token(sender, token, asset_id, total - amount, sender_nonce + 1))
    tx = Transaction([command.address()] + [address for address, _ in pay], [], outputs, GAS_LIMIT)
    tx.unlockers = [eip712_unlocker(chain_key, tx.hash(), chain_id)] * len(tx.inputs)
    return tx, dict(operation='deposit_pool', sender=to_hex(sender), token=to_hex(token),
                    amount_raw=str(amount), pool_owner=to_hex(pool_owner), spot_contract=to_hex(spot),
                    custody=to_hex(custody), txhash=to_hex(tx.hash()),
                    receiver_leaf=to_hex(locked.address()), change_raw=str(total - amount),
                    inputs=[to_hex(address) for address, _ in pay])
