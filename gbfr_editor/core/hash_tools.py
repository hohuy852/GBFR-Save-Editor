from __future__ import annotations

import struct

_MASK = 0xFFFFFFFF
_PRIME32_1 = 0x9E3779B1
_PRIME32_2 = 0x85EBCA77
_PRIME32_3 = 0xC2B2AE3D
_PRIME32_4 = 0x27D4EB2F
_PRIME32_5 = 0x165667B1


def _rotl32(x: int, r: int) -> int:
    x &= _MASK
    return ((x << r) & _MASK) | (x >> (32 - r))


def _round(seed: int, value: int) -> int:
    return (_rotl32((seed + ((value * _PRIME32_2) & _MASK)) & _MASK, 13) * _PRIME32_1) & _MASK


def gbfr_hash32(text: str) -> int:
    """GBFR's custom XXHash32 variant used by GBFRDataTools for IDs."""
    data = text.encode("ascii")
    pos = 0
    h32 = 0x178A54A4
    if len(data) >= 16:
        v1 = 0x2557311B
        v2 = 0x871FB76A
        v3 = 0x0133ECF3
        v4 = 0x62FC7342
        while True:
            v1 = _round(v1, struct.unpack_from("<I", data, pos)[0])
            v2 = _round(v2, struct.unpack_from("<I", data, pos + 4)[0])
            v3 = _round(v3, struct.unpack_from("<I", data, pos + 8)[0])
            v4 = _round(v4, struct.unpack_from("<I", data, pos + 12)[0])
            pos += 16
            if len(data) - pos <= 16:
                break
        h32 = (_rotl32(v1, 1) + _rotl32(v2, 7) + _rotl32(v3, 12) + _rotl32(v4, 18)) & _MASK
    h32 = (h32 + len(data)) & _MASK
    while len(data) - pos >= 4:
        value = struct.unpack_from("<I", data, pos)[0]
        h32 = (_rotl32((h32 + ((value * _PRIME32_3) & _MASK)) & _MASK, 17) * _PRIME32_4) & _MASK
        pos += 4
    while len(data) - pos > 0:
        h32 = (_rotl32((h32 + data[pos] * _PRIME32_5) & _MASK, 11) * _PRIME32_1) & _MASK
        pos += 1
    h32 ^= h32 >> 15
    h32 = (h32 * _PRIME32_2) & _MASK
    h32 ^= h32 >> 13
    h32 = (h32 * _PRIME32_3) & _MASK
    h32 ^= h32 >> 16
    return h32 & _MASK
