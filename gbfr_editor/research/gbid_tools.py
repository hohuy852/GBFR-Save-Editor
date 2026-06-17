from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from gbfr_save import GBFRSaveData, UnitRecord
from item_db import ItemDatabase
from unit_meta import unit_name

HASHISH_ID_TYPES = {1301, 1901, 2002, 2102, 2703, 2706, 2803, 2816, 3903}
EMPTY_HASHES = {0, 0x887AE0B0}


@dataclass(frozen=True)
class CandidateRecord:
    category: str
    confidence: str
    kind: str
    id_type: int
    id_name: str
    unit_id: int
    value_count: int
    preview: str
    note: str


def _preview_values(save: GBFRSaveData, rec: UnitRecord, item_db: Optional[ItemDatabase] = None, limit: int = 8) -> str:
    values = save.get_values(rec, limit)
    shown: List[str] = []
    for value in values:
        if isinstance(value, bool):
            shown.append("true" if value else "false")
        elif rec.kind == "uint" and rec.id_type in HASHISH_ID_TYPES and item_db:
            shown.append(item_db.lookup_text(int(value)))
        elif isinstance(value, float):
            shown.append(f"{value:.6g}")
        else:
            shown.append(str(value))
    if rec.value_count > limit:
        shown.append("...")
    return ", ".join(shown)


def build_candidate_records(save: GBFRSaveData, item_db: Optional[ItemDatabase] = None, limit: int = 5000) -> List[CandidateRecord]:
    """Return research-oriented high-value records, without claiming unproven offsets are safe."""
    rows: List[CandidateRecord] = []
    known: Dict[int, tuple[str, str, str]] = {
        1003: ("Save integrity", "Known", "Hash seed. Do not edit unless researching hash slot behavior."),
        1301: ("Characters", "Known hash", "Character ID hashes."),
        2102: ("Items", "Likely", "Item/material/sigil hash candidate observed in uploaded saves."),
        2103: ("Items", "Research", "Looks like item serial/index, not a simple count."),
        2104: ("Items", "Research", "Item flag/state candidate."),
        2105: ("Items", "Research", "Quantity/type candidate; verify with before/after save before bulk editing."),
        2702: ("Sigils", "Likely", "Sigil slot ID."),
        2703: ("Sigils", "Likely hash", "Sigil/Gem ID hash."),
        2704: ("Sigils", "Likely", "Primary sigil skill level."),
        2706: ("Sigils", "Likely hash", "Equipped/worn-by character hash."),
        2707: ("Sigils", "Likely", "Sigil flags; bit 0 is likely lock state."),
        2803: ("Weapons", "Likely hash", "Weapon ID hash."),
        2804: ("Weapons", "Likely", "Weapon XP/progress candidate."),
        2815: ("Weapons", "Research", "Weapon flags candidate."),
        2816: ("Weapons", "Likely hash", "Weapon stone/item hash candidate."),
        4315: ("Options", "Known", "Difficulty option."),
        4316: ("Options", "Known", "Assist mode option."),
        4317: ("Options", "Known", "Auto-save bool."),
        4318: ("Options", "Known", "Auto-play dialogue bool."),
        4319: ("Options", "Known", "Screen shake bool."),
    }
    for rec in save.records:
        if rec.id_type in known:
            cat, conf, note = known[rec.id_type]
            rows.append(CandidateRecord(cat, conf, rec.kind, rec.id_type, unit_name(rec.id_type), rec.unit_id, rec.value_count, _preview_values(save, rec, item_db), note))
        elif rec.value_count == 1 and rec.kind in {"int", "uint", "long", "ulong", "float"}:
            vals = save.get_values(rec, 1)
            value = vals[0] if vals else None
            # Include single-value numeric records that look like currencies/progress counters.
            if isinstance(value, (int, float)) and value not in (0, 1) and -1_000_000_000 <= float(value) <= 1_000_000_000:
                if rec.id_type < 2000 or 3000 <= rec.id_type <= 4500 or rec.id_type >= 8900:
                    rows.append(CandidateRecord("Single-value counters", "Research", rec.kind, rec.id_type, unit_name(rec.id_type), rec.unit_id, rec.value_count, _preview_values(save, rec, item_db), "Single numeric value; useful for before/after value hunting."))
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: (r.category, r.id_type, r.unit_id, r.kind))
    return rows


def format_record_pointer(kind: str, id_type: int, unit_id: int) -> str:
    return f"{kind}:{id_type}:{unit_id}"


def parse_record_pointer(text: str) -> tuple[str, int, int]:
    parts = [p.strip() for p in text.replace(",", ":").split(":") if p.strip()]
    if len(parts) != 3:
        raise ValueError("Use kind:id_type:unit_id, for example uint:2704:123")
    return parts[0].lower(), int(parts[1], 0), int(parts[2], 0)
