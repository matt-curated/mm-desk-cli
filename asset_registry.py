"""Load shared asset metadata and annotate addresses only at display boundaries."""
import json
from pathlib import Path
import re
import unicodedata

REGISTRY_PATH = Path(__file__).with_name('assets.json')
ADDRESS = re.compile(r'0[xX][0-9a-fA-F]{40}\Z')
ADDRESS_IN_TEXT = re.compile(r'(?<![A-Za-z0-9_])0[xX][0-9a-fA-F]{40}(?![A-Za-z0-9_])')
ASSET_FIELDS = frozenset({'token', 'operator', 'asset', 'token_address', 'asset_address', 'base_asset', 'quote_asset'})


def load_assets(path=REGISTRY_PATH):
    """Return normalized address -> (name, decimals) metadata from the one data file."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate key in asset registry: {key}')
            result[key] = value
        return result

    data = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise ValueError('Asset registry must be a JSON object keyed by asset address')
    assets, names = {}, set()
    for address, info in data.items():
        if not ADDRESS.fullmatch(address):
            raise ValueError(f'Invalid asset address in registry: {address}')
        normalized = address.lower()
        if normalized in assets:
            raise ValueError(f'Duplicate asset address in registry: {address}')
        if not isinstance(info, dict):
            raise ValueError(f'Asset metadata must be an object: {address}')
        name, decimals = info.get('name'), info.get('decimals')
        if not isinstance(name, str) or not name.strip() or name != name.strip() or any(
                unicodedata.category(c).startswith('C') for c in name):
            raise ValueError(f'Asset name must be nonempty text without control characters: {address}')
        if name.upper() in names:
            raise ValueError(f'Duplicate asset name in registry: {name}')
        if decimals is not None and (type(decimals) is not int or not 0 <= decimals <= 255):
            raise ValueError(f'Asset decimals must be an integer from 0 to 255 or null: {address}')
        assets[normalized] = (name, decimals)
        names.add(name.upper())
    return assets


ASSETS = load_assets()
TOKENS = {name.upper(): address for address, (name, _) in ASSETS.items()}


def asset_label(address):
    """Preserve the original address casing and annotate only known assets."""
    entry = ASSETS.get(address.lower())
    return f'{entry[0]}（{address}）' if entry else address


def format_asset_text(text):
    """Annotate known 20-byte addresses, excluding longer hashes and identifiers."""
    def replace(match):
        address = match.group()
        entry = ASSETS.get(address.lower())
        if entry is None:
            return address
        prefix = entry[0] + '（'
        if text[:match.start()].endswith(prefix) and text[match.end():].startswith('）'):
            return address
        return asset_label(address)
    return ADDRESS_IN_TEXT.sub(replace, text)


def format_asset_values(value):
    """Create a display copy. Never change API inputs, responses, or stored errors."""
    if isinstance(value, str):
        return format_asset_text(value)
    if isinstance(value, list):
        return [format_asset_values(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            display_key = format_asset_text(key) if isinstance(key, str) else key
            if key in ASSET_FIELDS and isinstance(item, str) and item.upper() in TOKENS:
                result[display_key] = asset_label(TOKENS[item.upper()])
            else:
                result[display_key] = format_asset_values(item)
        return result
    return value


def display_width(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)
