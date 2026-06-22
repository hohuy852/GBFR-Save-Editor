from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import json
import os
import shutil
import struct
import tempfile
import time

MASK64 = 0xFFFFFFFFFFFFFFFF
XXH_PRIME64_1 = 11400714785074694791
XXH_PRIME64_2 = 14029467366897019727
XXH_PRIME64_3 = 1609587929392839161
XXH_PRIME64_4 = 9650029242287828579
XXH_PRIME64_5 = 2870177450012600261
XXHASH64_SAVE_SEED = 0x2F1A43EBCD

# Matches GBFRDataTools.SaveFile SaveGameFile.HashSectionInfos.
HASH_SECTION_INFOS: List[Tuple[int, int]] = [
    (0x58, 0x80),
    (0x30, 0xA0),
    (0x28, 0x30),
    (0x38, 0xC0),
    (0x40, 0xB0),
    (0x68, 0x50),
    (0x48, 0x60),
    (0x70, 0x90),
    (0x50, 0x40),
    (0x60, 0x70),
]


def _rol64(value: int, bits: int) -> int:
    return ((value << bits) & MASK64) | (value >> (64 - bits))


def _xxh64_round(acc: int, value: int) -> int:
    acc = (acc + value * XXH_PRIME64_2) & MASK64
    acc = _rol64(acc, 31)
    acc = (acc * XXH_PRIME64_1) & MASK64
    return acc


def _xxh64_merge(acc: int, value: int) -> int:
    value = _xxh64_round(0, value)
    acc ^= value
    acc = (acc * XXH_PRIME64_1 + XXH_PRIME64_4) & MASK64
    return acc


def _xxh64_avalanche(value: int) -> int:
    value ^= value >> 33
    value = (value * XXH_PRIME64_2) & MASK64
    value ^= value >> 29
    value = (value * XXH_PRIME64_3) & MASK64
    value ^= value >> 32
    return value & MASK64


def xxh64(data: bytes | bytearray | memoryview, seed: int = 0) -> int:
    """Pure-Python xxHash64, used so the editor has no binary dependency."""
    view = memoryview(data)
    length = len(view)
    pos = 0

    if length >= 32:
        v1 = (seed + XXH_PRIME64_1 + XXH_PRIME64_2) & MASK64
        v2 = (seed + XXH_PRIME64_2) & MASK64
        v3 = seed & MASK64
        v4 = (seed - XXH_PRIME64_1) & MASK64
        limit = length - 32
        while pos <= limit:
            v1 = _xxh64_round(v1, struct.unpack_from("<Q", view, pos)[0]); pos += 8
            v2 = _xxh64_round(v2, struct.unpack_from("<Q", view, pos)[0]); pos += 8
            v3 = _xxh64_round(v3, struct.unpack_from("<Q", view, pos)[0]); pos += 8
            v4 = _xxh64_round(v4, struct.unpack_from("<Q", view, pos)[0]); pos += 8

        h64 = (_rol64(v1, 1) + _rol64(v2, 7) + _rol64(v3, 12) + _rol64(v4, 18)) & MASK64
        h64 = _xxh64_merge(h64, v1)
        h64 = _xxh64_merge(h64, v2)
        h64 = _xxh64_merge(h64, v3)
        h64 = _xxh64_merge(h64, v4)
    else:
        h64 = (seed + XXH_PRIME64_5) & MASK64

    h64 = (h64 + length) & MASK64

    while pos + 8 <= length:
        k1 = _xxh64_round(0, struct.unpack_from("<Q", view, pos)[0])
        h64 ^= k1
        h64 = (_rol64(h64, 27) * XXH_PRIME64_1 + XXH_PRIME64_4) & MASK64
        pos += 8

    if pos + 4 <= length:
        h64 ^= (struct.unpack_from("<I", view, pos)[0] * XXH_PRIME64_1) & MASK64
        h64 = (_rol64(h64, 23) * XXH_PRIME64_2 + XXH_PRIME64_3) & MASK64
        pos += 4

    while pos < length:
        h64 ^= (view[pos] * XXH_PRIME64_5) & MASK64
        h64 = (_rol64(h64, 11) * XXH_PRIME64_1) & MASK64
        pos += 1

    return _xxh64_avalanche(h64)


def _read_u16(buf: bytes | bytearray | memoryview, offset: int) -> int:
    return struct.unpack_from("<H", buf, offset)[0]


def _read_u32(buf: bytes | bytearray | memoryview, offset: int) -> int:
    return struct.unpack_from("<I", buf, offset)[0]


def _read_i32(buf: bytes | bytearray | memoryview, offset: int) -> int:
    return struct.unpack_from("<i", buf, offset)[0]


def _write_u64(buf: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<Q", buf, offset, value & MASK64)


def _looks_like_flatbuffer_root(data: bytes | bytearray, start: int = 0, size: Optional[int] = None) -> bool:
    if size is None:
        size = len(data) - start
    if size < 16 or start < 0 or start + size > len(data):
        return False
    try:
        root_rel = _read_u32(data, start)
        table = start + root_rel
        if not (start + 4 <= table < start + size - 4):
            return False
        vt_rel = _read_i32(data, table)
        vtable = table - vt_rel
        if not (start <= vtable < table):
            return False
        vt_len = _read_u16(data, vtable)
        obj_len = _read_u16(data, vtable + 2)
        if not (4 <= vt_len <= 128 and 4 <= obj_len <= 256):
            return False
        if vtable + vt_len > start + size:
            return False
        return True
    except Exception:
        return False


@dataclass(frozen=True)
class ScalarSpec:
    table_field: int
    label: str
    element_size: int
    struct_format: str
    is_float: bool = False
    is_bool: bool = False


SCALAR_SPECS: Dict[str, ScalarSpec] = {
    "bool": ScalarSpec(1, "Bool", 1, "<?", is_bool=True),
    "byte": ScalarSpec(2, "Byte", 1, "<b"),
    "ubyte": ScalarSpec(3, "UByte", 1, "<B"),
    "short": ScalarSpec(4, "Short", 2, "<h"),
    "ushort": ScalarSpec(5, "UShort", 2, "<H"),
    "int": ScalarSpec(6, "Int", 4, "<i"),
    "uint": ScalarSpec(7, "UInt", 4, "<I"),
    "long": ScalarSpec(8, "Long", 8, "<q"),
    "ulong": ScalarSpec(9, "ULong", 8, "<Q"),
    "float": ScalarSpec(10, "Float", 4, "<f", is_float=True),
}
SPEC_BY_FIELD = {spec.table_field: key for key, spec in SCALAR_SPECS.items()}


@dataclass
class UnitRecord:
    kind: str
    index: int
    table_offset: int
    id_type: int
    unit_id: int
    value_vector_offset: int
    value_count: int
    value_data_offset: int

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.index}"


@dataclass
class SaveContainerInfo:
    path: Path
    mode: str
    payload_offset: int
    payload_size: int
    header: Dict[str, int]


class FlatBufferError(RuntimeError):
    pass


class GBFRSaveData:
    """In-place editor for GBFR SaveDataBinary payloads.

    The editor intentionally does not rebuild FlatBuffers yet. It only changes
    existing scalar vector values, which keeps offsets and file size stable.
    """

    def __init__(self, container: SaveContainerInfo, file_bytes: bytearray):
        self.container = container
        self._file_bytes = file_bytes
        if (
            int(container.payload_offset) < 0
            or int(container.payload_size) <= 0
            or int(container.payload_offset) + int(container.payload_size) > len(file_bytes)
        ):
            raise FlatBufferError("Save payload points outside the file. The file may be encrypted, incomplete, or not a supported decrypted GBFR save.")
        self._payload = memoryview(self._file_bytes)[container.payload_offset:container.payload_offset + container.payload_size]
        self.root_table = self._read_u32(0)
        if not self._payload_has(self.root_table, 4):
            raise FlatBufferError("Save root table points outside the file. The file may be encrypted or not a decrypted GBFR save.")
        self.records: List[UnitRecord] = []
        self.records_by_key: Dict[str, UnitRecord] = {}
        self.records_by_id: Dict[Tuple[str, int, int], List[UnitRecord]] = {}
        self.version_maybe: Optional[int] = None
        self._parse()

    @classmethod
    def open(cls, path: str | Path) -> "GBFRSaveData":
        p = Path(path)
        data = bytearray(p.read_bytes())
        try:
            container = cls._detect_container(p, data)
            return cls(container, data)
        except struct.error as exc:
            raise FlatBufferError(
                "This file looked like a possible GBFR save but ended early while reading it. "
                "It may be encrypted, compressed, incomplete, or not the actual GameData/SaveData payload."
            ) from exc

    @staticmethod
    def _detect_container(path: Path, data: bytearray) -> SaveContainerInfo:
        if _looks_like_flatbuffer_root(data, 0, len(data)):
            return SaveContainerInfo(path=path, mode="raw_savedatabinary", payload_offset=0, payload_size=len(data), header={})

        # Full SaveGameFile wrapper seen in GBFRDataTools.SaveFile.FromFile.
        if len(data) >= 52:
            try:
                main_version = struct.unpack_from("<i", data, 0)[0]
                steam_id = struct.unpack_from("<Q", data, 4)[0]
                unknown = struct.unpack_from("<i", data, 12)[0]
                sub_version = struct.unpack_from("<i", data, 16)[0]
                offset1 = struct.unpack_from("<q", data, 20)[0]
                slot_offset = struct.unpack_from("<q", data, 28)[0]
                size1 = struct.unpack_from("<q", data, 36)[0]
                slot_size = struct.unpack_from("<q", data, 44)[0]
                if (
                    0 <= offset1 < len(data) and 0 < size1 <= len(data) - offset1
                    and 0 <= slot_offset < len(data) and 0 < slot_size <= len(data) - slot_offset
                    and _looks_like_flatbuffer_root(data, slot_offset, slot_size)
                ):
                    return SaveContainerInfo(
                        path=path,
                        mode="wrapped_savegamefile_slotdata",
                        payload_offset=int(slot_offset),
                        payload_size=int(slot_size),
                        header={
                            "main_version": int(main_version),
                            "steam_id": int(steam_id),
                            "unknown": int(unknown),
                            "sub_version": int(sub_version),
                            "binary1_offset": int(offset1),
                            "slot_offset": int(slot_offset),
                            "binary1_size": int(size1),
                            "slot_size": int(slot_size),
                        },
                    )
            except Exception:
                pass

        raise FlatBufferError("This file does not look like a GBFR SaveDataBinary or supported SaveGameFile wrapper.")

    def _abs(self, payload_offset: int) -> int:
        return self.container.payload_offset + payload_offset

    def _payload_has(self, offset: int, size: int) -> bool:
        try:
            offset = int(offset)
            size = int(size)
        except Exception:
            return False
        return (
            offset >= 0
            and size >= 0
            and offset + size <= int(self.container.payload_size)
            and self._abs(offset) + size <= len(self._file_bytes)
        )

    def _require_payload(self, offset: int, size: int, label: str = "save data") -> None:
        if not self._payload_has(offset, size):
            raise FlatBufferError(
                f"{label} is outside the readable save buffer. "
                "The file may be encrypted, incomplete, or not a supported decrypted GBFR save."
            )

    def _read_u16(self, offset: int) -> int:
        self._require_payload(offset, 2, "u16 field")
        return _read_u16(self._file_bytes, self._abs(offset))

    def _read_u32(self, offset: int) -> int:
        self._require_payload(offset, 4, "u32 field")
        return _read_u32(self._file_bytes, self._abs(offset))

    def _read_i32(self, offset: int) -> int:
        self._require_payload(offset, 4, "i32 field")
        return _read_i32(self._file_bytes, self._abs(offset))

    def _table_field_pos(self, table_offset: int, field_index: int) -> Optional[int]:
        try:
            if not self._payload_has(table_offset, 4):
                return None
            vtable = table_offset - self._read_i32(table_offset)
            if not self._payload_has(vtable, 4):
                return None
            vtable_size = self._read_u16(vtable)
            if vtable_size < 4 or vtable_size > 512 or not self._payload_has(vtable, vtable_size):
                return None
            field_entry = 4 + field_index * 2
            if field_entry + 2 > vtable_size:
                return None
            rel = self._read_u16(vtable + field_entry)
            if rel == 0:
                return None
            pos = table_offset + rel
            if not self._payload_has(pos, 1):
                return None
            return pos
        except (FlatBufferError, struct.error, ValueError):
            return None

    def _vector_from_field(self, table_offset: int, field_index: int) -> Optional[Tuple[int, int, int]]:
        field_pos = self._table_field_pos(table_offset, field_index)
        if field_pos is None:
            return None
        try:
            if not self._payload_has(field_pos, 4):
                return None
            vector_offset = field_pos + self._read_u32(field_pos)
            if not self._payload_has(vector_offset, 4):
                return None
            count = self._read_u32(vector_offset)
            data_offset = vector_offset + 4
            if count < 0 or count > 5_000_000 or not self._payload_has(data_offset, 0):
                return None
            return vector_offset, count, data_offset
        except (FlatBufferError, struct.error, ValueError):
            return None

    def _parse(self) -> None:
        version_pos = self._table_field_pos(self.root_table, 0)
        try:
            self.version_maybe = self._read_u32(version_pos) if version_pos is not None else None
        except (FlatBufferError, struct.error):
            self.version_maybe = None

        for kind, spec in SCALAR_SPECS.items():
            vector = self._vector_from_field(self.root_table, spec.table_field)
            if vector is None:
                continue
            _, count, data_offset = vector
            if not self._payload_has(data_offset, int(count) * 4):
                continue
            for index in range(count):
                try:
                    slot = data_offset + index * 4
                    if not self._payload_has(slot, 4):
                        continue
                    table_offset = slot + self._read_u32(slot)
                    if not self._payload_has(table_offset, 4):
                        continue
                    id_pos = self._table_field_pos(table_offset, 0)
                    unit_pos = self._table_field_pos(table_offset, 1)
                    val_vector = self._vector_from_field(table_offset, 2)
                    if id_pos is None or val_vector is None:
                        continue
                    value_vector_offset, value_count, value_data_offset = val_vector
                    if value_count < 0 or not self._payload_has(value_data_offset, int(value_count) * int(spec.element_size)):
                        continue
                    id_type = self._read_u32(id_pos)
                    unit_id = self._read_u32(unit_pos) if unit_pos is not None else 0
                    rec = UnitRecord(
                        kind=kind,
                        index=index,
                        table_offset=table_offset,
                        id_type=id_type,
                        unit_id=unit_id,
                        value_vector_offset=value_vector_offset,
                        value_count=value_count,
                        value_data_offset=value_data_offset,
                    )
                    self.records.append(rec)
                    self.records_by_key[rec.key] = rec
                    self.records_by_id.setdefault((kind, id_type, unit_id), []).append(rec)
                except (FlatBufferError, struct.error, ValueError, OverflowError):
                    continue

        if not self.records:
            raise FlatBufferError(
                "No editable GBFR value records were found. The selected file may be encrypted, compressed, incomplete, or not the decrypted GameData/SaveData payload."
            )

    def get_values(self, rec: UnitRecord, limit: Optional[int] = None) -> List[Any]:
        spec = SCALAR_SPECS[rec.kind]
        total = rec.value_count if limit is None else min(rec.value_count, limit)
        values: List[Any] = []
        for i in range(total):
            rel = rec.value_data_offset + i * spec.element_size
            if not self._payload_has(rel, spec.element_size):
                break
            pos = self._abs(rel)
            if spec.is_bool:
                values.append(bool(struct.unpack_from(spec.struct_format, self._file_bytes, pos)[0]))
            elif spec.is_float:
                values.append(float(struct.unpack_from(spec.struct_format, self._file_bytes, pos)[0]))
            else:
                values.append(struct.unpack_from(spec.struct_format, self._file_bytes, pos)[0])
        return values

    def get_first_value(self, rec: UnitRecord, default: Any = None) -> Any:
        """Read only the first scalar value from a record.

        Many editor actions only need value[0].  Reading the entire vector can
        be expensive or unsafe if a newly mapped/experimental field has a large
        vector length.  Mastery Mods uses thousands of 1606/1607 scalar rows, so
        first-value edits must stay O(1).
        """
        if rec is None or rec.value_count < 1:
            return default
        spec = SCALAR_SPECS[rec.kind]
        if not self._payload_has(rec.value_data_offset, spec.element_size):
            return default
        pos = self._abs(rec.value_data_offset)
        if spec.is_bool:
            return bool(struct.unpack_from(spec.struct_format, self._file_bytes, pos)[0])
        if spec.is_float:
            return float(struct.unpack_from(spec.struct_format, self._file_bytes, pos)[0])
        return struct.unpack_from(spec.struct_format, self._file_bytes, pos)[0]

    def set_first_value(self, rec: UnitRecord, raw: Any) -> None:
        """Patch only value[0] for a scalar vector without rebuilding the vector."""
        if rec is None or rec.value_count < 1:
            raise ValueError("record has no scalar values to edit")
        spec = SCALAR_SPECS[rec.kind]
        if not self._payload_has(rec.value_data_offset, spec.element_size):
            raise FlatBufferError("Cannot write this row because its value offset is outside the save buffer.")
        pos = self._abs(rec.value_data_offset)
        value = self._coerce_scalar_for_pack(rec.kind, raw)
        struct.pack_into(spec.struct_format, self._file_bytes, pos, value)

    def _coerce_scalar_for_pack(self, kind: str, raw: Any) -> Any:
        spec = SCALAR_SPECS[kind]
        if spec.is_bool:
            return bool(raw)
        if spec.is_float:
            return float(raw)
        value = int(raw)
        # Save Wizard codes often write unsigned-looking hex values even when the
        # FlatBuffer field is signed. Convert those 32/64-bit bit patterns into
        # Python signed integers before struct.pack_into so a value like
        # 0xFFFFFFFF becomes -1 instead of crashing the writer.
        ranges = {
            "byte": (-0x80, 0x7F, 8),
            "short": (-0x8000, 0x7FFF, 16),
            "int": (-0x80000000, 0x7FFFFFFF, 32),
            "long": (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF, 64),
            "ubyte": (0, 0xFF, 8),
            "ushort": (0, 0xFFFF, 16),
            "uint": (0, 0xFFFFFFFF, 32),
            "ulong": (0, 0xFFFFFFFFFFFFFFFF, 64),
        }
        if kind not in ranges:
            return value
        lo, hi, bits = ranges[kind]
        mask = (1 << bits) - 1
        if lo < 0 and value > hi and value <= mask:
            value -= (1 << bits)
        if lo == 0 and value < 0:
            value &= mask
        if not (lo <= value <= hi):
            raise ValueError(f"Value {raw!r} is outside {kind} range {lo}..{hi}.")
        return value

    def set_values(self, rec: UnitRecord, values: Sequence[Any]) -> None:
        if len(values) != rec.value_count:
            raise ValueError(f"Value count must stay {rec.value_count}; got {len(values)}.")
        spec = SCALAR_SPECS[rec.kind]
        needed = int(rec.value_count) * int(spec.element_size)
        if not self._payload_has(rec.value_data_offset, needed):
            raise FlatBufferError("Cannot write this row because its value vector is outside the save buffer.")
        for i, raw in enumerate(values):
            pos = self._abs(rec.value_data_offset + i * spec.element_size)
            value = self._coerce_scalar_for_pack(rec.kind, raw)
            struct.pack_into(spec.struct_format, self._file_bytes, pos, value)

    def find(self, kind: Optional[str] = None, id_type: Optional[int] = None, unit_id: Optional[int] = None) -> List[UnitRecord]:
        out: List[UnitRecord] = []
        for rec in self.records:
            if kind is not None and rec.kind != kind:
                continue
            if id_type is not None and rec.id_type != id_type:
                continue
            if unit_id is not None and rec.unit_id != unit_id:
                continue
            out.append(rec)
        return out

    def find_first(self, kind: str, id_type: int, unit_id: int = 0) -> Optional[UnitRecord]:
        rows = self.records_by_id.get((kind, id_type, unit_id))
        return rows[0] if rows else None

    def parse_user_values(self, rec: UnitRecord, text: str) -> List[Any]:
        parts = [p.strip() for p in text.replace("\n", ",").split(",") if p.strip() != ""]
        spec = SCALAR_SPECS[rec.kind]
        if len(parts) != rec.value_count:
            raise ValueError(f"Expected {rec.value_count} values, got {len(parts)}.")
        out: List[Any] = []
        for p in parts:
            if spec.is_bool:
                out.append(p.lower() in {"1", "true", "yes", "on"})
            elif spec.is_float:
                out.append(float(p))
            else:
                out.append(int(p, 0))
        return out

    def hash_seed_record(self) -> Optional[UnitRecord]:
        return self.find_first("uint", 1003, 0)

    def hash_seed(self) -> Optional[int]:
        rec = self.hash_seed_record()
        if rec is None or rec.value_count < 1:
            return None
        return int(self.get_values(rec, 1)[0])

    def hashes_offset(self) -> Optional[int]:
        if self.container.payload_size < 0x14:
            return None
        off = self._read_u32(self.container.payload_size - 0x14)
        if off + 8 * len(HASH_SECTION_INFOS) <= self.container.payload_size:
            return off
        return None

    def stored_hashes(self) -> Optional[List[int]]:
        off = self.hashes_offset()
        if off is None:
            return None
        return [struct.unpack_from("<Q", self._file_bytes, self._abs(off + i * 8))[0] for i in range(len(HASH_SECTION_INFOS))]

    def compute_hash(self, index: int) -> int:
        hashes_offset = self.hashes_offset()
        if hashes_offset is None:
            raise FlatBufferError("Could not locate GBFR hash table.")
        start, sub_size = HASH_SECTION_INFOS[index]
        end = hashes_offset - sub_size
        if start < 0 or end <= start or end > self.container.payload_size:
            raise FlatBufferError(f"Invalid hash section bounds for index {index}.")
        abs_start = self._abs(start)
        abs_end = self._abs(end)
        return xxh64(memoryview(self._file_bytes)[abs_start:abs_end], XXHASH64_SAVE_SEED)

    def active_hash_index(self) -> Optional[int]:
        seed = self.hash_seed()
        return None if seed is None else int(seed % len(HASH_SECTION_INFOS))

    def check_active_hash(self) -> Optional[bool]:
        idx = self.active_hash_index()
        stored = self.stored_hashes()
        if idx is None or stored is None:
            return None
        return self.compute_hash(idx) == stored[idx]

    def update_active_hash(self) -> Optional[int]:
        """Refresh only the hash section selected by save field uint:1003.

        Real PC raw GameData and PS4 SaveData1.dat samples show that only the
        seed-selected slot normally matches. The remaining nine stored values
        are not expected to validate against the current payload. Updating all
        slots makes the file look less like a game-written save, so normal save
        operations intentionally update only this active slot.
        """
        idx = self.active_hash_index()
        off = self.hashes_offset()
        if idx is None or off is None:
            return None
        new_hash = self.compute_hash(idx)
        _write_u64(self._file_bytes, self._abs(off + idx * 8), new_hash)
        return idx

    def update_all_hashes(self) -> List[int]:
        """Debug/recovery helper: refresh every known hash-table entry.

        Do not use this for normal writes. Samples from the game keep only one
        active hash valid, selected by uint:1003. This method remains available
        for research/export tools, but save_as() defaults to active-only.
        """
        off = self.hashes_offset()
        if off is None:
            return []
        updated: List[int] = []
        for idx in range(len(HASH_SECTION_INFOS)):
            try:
                new_hash = self.compute_hash(idx)
            except Exception:
                continue
            _write_u64(self._file_bytes, self._abs(off + idx * 8), new_hash)
            updated.append(idx)
        return updated

    def hash_status(self) -> Dict[str, Any]:
        stored = self.stored_hashes()
        status: Dict[str, Any] = {
            "hashes_offset": self.hashes_offset(),
            "active_hash_index": self.active_hash_index(),
            "active_hash_ok": self.check_active_hash(),
            "sections": [],
        }
        if stored is None:
            return status
        for idx, old in enumerate(stored):
            try:
                computed = self.compute_hash(idx)
                ok = computed == old
            except Exception as exc:
                computed = None
                ok = False
                status["sections"].append({"index": idx, "stored": old, "computed": computed, "ok": ok, "error": str(exc)})
                continue
            status["sections"].append({"index": idx, "stored": old, "computed": computed, "ok": ok})
        return status

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {k: 0 for k in SCALAR_SPECS}
        for rec in self.records:
            counts[rec.kind] += 1
        return {
            "path": str(self.container.path),
            "mode": self.container.mode,
            "size": len(self._file_bytes),
            "payload_offset": self.container.payload_offset,
            "payload_size": self.container.payload_size,
            "version_maybe": self.version_maybe,
            "record_counts": counts,
            "hash_seed": self.hash_seed(),
            "active_hash_index": self.active_hash_index(),
            "active_hash_ok": self.check_active_hash(),
            "header": self.container.header,
        }

    def to_jsonable_records(self, limit_values: int = 16) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for rec in self.records:
            vals = self.get_values(rec, limit_values)
            rows.append({
                "key": rec.key,
                "kind": rec.kind,
                "index": rec.index,
                "id_type": rec.id_type,
                "unit_id": rec.unit_id,
                "value_count": rec.value_count,
                "values_preview": vals,
                "truncated": rec.value_count > limit_values,
            })
        return rows

    def export_report(self, path: str | Path, limit_values: int = 16) -> None:
        payload = {"summary": self.summary(), "records": self.to_jsonable_records(limit_values)}
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _validate_serialized_bytes(self, path: Path) -> None:
        """Re-open a just-written save and fail fast if its structure/hash is bad."""
        reopened = GBFRSaveData.open(path)
        if reopened.container.payload_size != self.container.payload_size:
            raise FlatBufferError("Saved file reopened, but payload size changed unexpectedly.")
        if reopened.hashes_offset() is not None and reopened.check_active_hash() is False:
            raise FlatBufferError("Saved file reopened, but active GBFR hash validation failed.")

    def save_as(
        self,
        path: str | Path,
        update_hash: bool = True,
        update_all_hashes: bool = False,
        validate: bool = True,
        fsync: bool = True,
    ) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if update_hash:
            # Normal game-written saves have exactly one currently valid hash
            # section, selected by uint:1003. Keep that behavior for PC raw
            # GameData and wrapped PS4 SaveData1.dat files.
            if update_all_hashes:
                updated = self.update_all_hashes()
                if not updated:
                    self.update_active_hash()
            else:
                self.update_active_hash()

        # Atomic same-folder write: prevents half-written/corrupt saves if the
        # editor crashes or Windows blocks a write halfway through. The GUI uses
        # validate=False/fsync=False for interactive saves because Windows can
        # mark the app Not Responding while Defender/OneDrive/USB storage waits
        # on fsync or while the file is reopened and hash-checked a second time.
        # CLI/research callers keep the safer defaults unless they opt out.
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(self._file_bytes)
                fh.flush()
                if fsync:
                    os.fsync(fh.fileno())
            if validate:
                self._validate_serialized_bytes(tmp_path)
            os.replace(tmp_path, target)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def save_over_original(
        self,
        update_hash: bool = True,
        backup: bool = True,
        validate: bool = True,
        fsync: bool = True,
    ) -> Path:
        target = self.container.path
        if backup:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = target.with_name(f"{target.name}.bak_{stamp}")
            shutil.copy2(target, backup_path)
        self.save_as(target, update_hash=update_hash, update_all_hashes=False, validate=validate, fsync=fsync)
        return target

    def group_by_unit(self, ids: Iterable[int]) -> Dict[int, Dict[int, UnitRecord]]:
        wanted = set(ids)
        grouped: Dict[int, Dict[int, UnitRecord]] = {}
        for rec in self.records:
            if rec.id_type in wanted:
                grouped.setdefault(rec.unit_id, {})[rec.id_type] = rec
        return grouped
