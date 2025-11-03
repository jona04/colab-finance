from typing import Any
from hexbytes import HexBytes
from web3 import Web3

def to_json_safe(obj: Any) -> Any:
    """
    Recursively convert web3 / HexBytes-heavy structures into plain
    JSON-serializable primitives (dict, list, str, int, float, bool, None).

    - HexBytes -> "0x..." str
    - bytes    -> "0x..." str
    - dict     -> {k: to_json_safe(v)}
    - list/tuple -> [to_json_safe(v), ...]
    - everything else -> unchanged if natively serializable, else str(obj)
    """
    # HexBytes
    if isinstance(obj, HexBytes):
        return Web3.to_hex(obj)

    # bytes (raw bytes)
    if isinstance(obj, (bytes, bytearray)):
        # represent as 0x + hex
        return "0x" + obj.hex()

    # basic primitives
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj

    # dict
    if isinstance(obj, dict):
        return {str(k): to_json_safe(v) for (k, v) in obj.items()}

    # list / tuple / set etc.
    if isinstance(obj, (list, tuple, set)):
        return [to_json_safe(v) for v in obj]

    # fallback: stringify
    return str(obj)
