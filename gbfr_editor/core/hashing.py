from __future__ import annotations

PRIME32_1 = 0x9E3779B1
PRIME32_2 = 0x85EBCA77
PRIME32_3 = 0xC2B2AE3D
PRIME32_4 = 0x27D4EB2F
PRIME32_5 = 0x165667B1


def _u32(x: int) -> int:
    return x & 0xFFFFFFFF


def _rotl32(x: int, r: int) -> int:
    x &= 0xFFFFFFFF
    return ((x << r) | (x >> (32 - r))) & 0xFFFFFFFF


def _round(seed: int, input_value: int) -> int:
    return _u32(_rotl32(seed + _u32(input_value * PRIME32_2), 13) * PRIME32_1)


def gbfr_hash_bytes(data: bytes) -> int:
    """GBFR's custom XXHash32 variant used for many string IDs.

    Ported from GBFRDataTools.Hashing.XXHash32Custom. The seed and the
    four long-input accumulators are custom compared with normal xxhash32.
    """
    p = memoryview(data)
    h32 = 0x178A54A4
    offset = 0
    length = len(data)
    if length >= 16:
        v1 = 0x2557311B
        v2 = 0x871FB76A
        v3 = 0x0133ECF3
        v4 = 0x62FC7342
        # Match GBFRDataTools' loop condition: process while remaining > 16.
        # This preserves compatibility with the known custom implementation.
        while length - offset > 16:
            v1 = _round(v1, int.from_bytes(p[offset:offset+4], "little")); offset += 4
            v2 = _round(v2, int.from_bytes(p[offset:offset+4], "little")); offset += 4
            v3 = _round(v3, int.from_bytes(p[offset:offset+4], "little")); offset += 4
            v4 = _round(v4, int.from_bytes(p[offset:offset+4], "little")); offset += 4
        h32 = _u32(_rotl32(v1, 1) + _rotl32(v2, 7) + _rotl32(v3, 12) + _rotl32(v4, 18))
    h32 = _u32(h32 + length)
    while length - offset >= 4:
        val = int.from_bytes(p[offset:offset+4], "little")
        h32 = _u32(_rotl32(h32 + _u32(val * PRIME32_3), 17) * PRIME32_4)
        offset += 4
    while length - offset > 0:
        h32 = _u32(_rotl32(h32 + p[offset] * PRIME32_5, 11) * PRIME32_1)
        offset += 1
    h32 ^= h32 >> 15
    h32 = _u32(h32 * PRIME32_2)
    h32 ^= h32 >> 13
    h32 = _u32(h32 * PRIME32_3)
    h32 ^= h32 >> 16
    return h32 & 0xFFFFFFFF


def gbfr_hash(text: str, encoding: str = "ascii") -> int:
    return gbfr_hash_bytes(str(text).encode(encoding, errors="ignore"))


def gbfr_hash_hex(text: str) -> str:
    return f"{gbfr_hash(text):08X}"
